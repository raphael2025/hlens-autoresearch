"""Persisted-row provenance: one verifier for every committed Raw row a D3E consumer relies on.

Phase 1 D3E-R1 / D3E-R2 (ADR-0023 §4 / §7, ADR-0027 §2 / §8 / §9 / §11). A committed row is
never trusted because the contract accepts it: before the REST store adopts, compares or reports
a row, and before the channel reconciler compares two rows or writes an edge between them, the
row is rebuilt **by the writer's own builder** from its lineage and must reproduce column for
column. The REST store and the reconciler both call this module, so the rules cannot drift apart.

REST (``raw.binance_spot_rest_*``):

- a response revision is rebuilt from its own inputs — page query + origin, the published body
  (looked up through the ``StorageAdapter``), block base, request / retrieval / knowledge times
  and a validated first-delivery provenance; it must hold its arrival block alone and be the
  only, exact content of its one batch snapshot;
- an element revision must re-derive its key / payload / id from its native fields, name exactly
  one such lawful response revision that accepted a page of its data type and symbol with enough
  elements, and be rebuilt from its native fields plus that response's block base and times;
  its arrival number is held by it alone, and the element batches of that response —
  ``<response>.elements.<index>`` — must hold exactly the committed rows of that lineage, in
  element order, under one microbatch plan and with the committed fingerprint and row count.

Archive (``raw.binance_spot_archives`` + ``raw.binance_spot_agg_trades`` / ``..._klines_1m``):

- the archive revision named by ``archive_revision_id`` must be committed exactly once for the
  same data type and symbol; it is rebuilt by the D2 builder from its published object (looked
  up through the ``StorageAdapter``), the D2 path / checksum binding, the frozen D0 collector and
  source bindings, its block base and times, with the frozen availability policy and no edge;
  it holds its block alone and is the exact content of its one batch snapshot;
- an element row is rebuilt by the D2 builder from its parser-native fields and that archive
  revision's block base, times and declared unit (the D1 unit rule); its stored UTC times must
  be the parser's conversion of its native ticks inside the archive's coverage; its arrival
  number is held by it alone; and its row batch ``<archive>.rows.<index>`` — a contiguous
  prefix of one microbatch plan — must still hold exactly what that snapshot committed.

What is *not* proven: that a REST element first delivered by another page really sat at that
index of that page's body (re-decoding needs that page's chain context), and that an archive
row's natives are the ones the object's bytes hold (that is D2's re-parse on ingest). Both are
bound instead by the committed batch fingerprint of their lineage.

Every lookup is bounded (``IN`` chunks, one lineage at a time). Callers read the rows and run
the verification between two identical sets of table heads; a view whose heads moved is never
judged (see ``RestRevisionStore`` / ``ChannelReconciler``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import (
    And,
    BooleanExpression,
    EqualTo,
    GreaterThanOrEqual,
    In,
    LessThan,
)

from core.contracts.catalog import SnapshotInfo, TableNotFound
from core.contracts.collector import CollectedObject, SourceBinding
from core.contracts.revision import AvailabilityDecision, RevisionRecord
from core.contracts.storage import (
    IntegrityViolation,
    ObjectKeyViolation,
    StorageAdapter,
    StorageError,
)
from core.domain.base import FrozenMapping
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.collector import binance_archive as d0
from infrastructure.collector import binance_rest as d3d
from infrastructure.parser.binance_archive import (
    AGG_TRADES_ROW_SCHEMA,
    KLINES_1M_ROW_SCHEMA,
    TimeUnit,
    time_unit_for,
)
from infrastructure.parser.binance_rest import DECODER_BINDING, RestRejectionCode
from infrastructure.revision import identity as archive_identity
from infrastructure.revision import rest_identity
from infrastructure.revision.availability import (
    AVAILABILITY_BINDING,
    AvailabilitySubject,
    decide_availability,
)
from infrastructure.revision.rest_availability import (
    RestAvailabilitySubject,
    decide_rest_availability,
)
from infrastructure.revision.store import (
    ArchiveContext,
    RevisionCatalog,
    RevisionStoreError,
    _archive_batch,
    _archive_batch_id,
    _check_archive_arrival_seq,
    _row_batch,
    _row_batch_id,
    _row_records,
    _times_from_row,
    identify_archive,
)

__all__ = [
    "ACCEPTED",
    "ELEMENT_DEFINITIONS",
    "ELEMENT_NATIVE_COLUMNS",
    "MAX_ELEMENT_MICROBATCH_ROWS",
    "PROVENANCE_COLUMNS",
    "REJECTED",
    "PersistedRowVerifier",
    "batch",
    "batch_rows",
    "check_batch_snapshot",
    "check_block_base",
    "check_provenance_shape",
    "indexed_batches",
    "element_batch_id",
    "element_columns",
    "response_batch_id",
    "response_columns",
    "snapshots_of_batches",
]

ELEMENT_DEFINITIONS: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_REST_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_REST_KLINES_1M,
}
_ELEMENT_SUBJECTS: Final[Mapping[str, RestAvailabilitySubject]] = {
    "agg_trades": RestAvailabilitySubject.AGG_TRADE,
    "klines_1m": RestAvailabilitySubject.KLINE_1M,
}
_ARCHIVE_ROW_DEFINITIONS: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_KLINES_1M,
}
_ARCHIVE_ROW_SUBJECTS: Final[Mapping[str, AvailabilitySubject]] = {
    "agg_trades": AvailabilitySubject.AGG_TRADE,
    "klines_1m": AvailabilitySubject.KLINE_1M,
}
_PARSER_ROW_SCHEMAS: Final[Mapping[str, pa.Schema]] = {
    "agg_trades": AGG_TRADES_ROW_SCHEMA,
    "klines_1m": KLINES_1M_ROW_SCHEMA,
}


def _native_columns(definition: RegisteredTableDefinition) -> tuple[str, ...]:
    """The decoder's native element fields: every column after the decoder binding."""
    names = [field.name for field in definition.arrow_schema]
    return tuple(names[names.index("decoder_hash") + 1 :])


ELEMENT_NATIVE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    data_type: _native_columns(definition) for data_type, definition in ELEMENT_DEFINITIONS.items()
}
#: Rows per REST element microbatch are bounded by one page.
MAX_ELEMENT_MICROBATCH_ROWS: Final = rest_identity.PAGE_LIMIT

#: Response columns that record the *first delivery* (provenance), not the revision identity.
PROVENANCE_COLUMNS: Final[tuple[str, ...]] = (
    "collector_id",
    "collector_version",
    "collection_request_id",
    "page_index",
    "requested_at",
    "retrieved_at",
    "source_metadata",
    "decode_outcome",
    "decode_rejection_code",
    "element_count",
    "answered_start",
    "answered_end",
)
#: Every response revision is a complete 200 page (s1); other statuses never reach the store.
_HTTP_OK: Final = 200
ACCEPTED: Final = "accepted"
REJECTED: Final = "rejected"
#: Values per ``IN`` filter of a bounded lookup (lineage revisions, arrival holders).
_KEY_CHUNK: Final = 256
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MINUTE: Final = timedelta(minutes=1)
_MICROSECOND: Final = timedelta(microseconds=1)
_BATCH_INDEX_DIGITS: Final = 8


def _equals(column: str, value: object) -> BooleanExpression:
    """``column == value`` as a PyIceberg expression (never a string built from data)."""
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _member(column: str, values: Iterable[object]) -> BooleanExpression:
    """``column IN values`` as a PyIceberg expression."""
    return In(column, set(values))  # type: ignore[call-arg, arg-type]


def _at_least(column: str, value: object) -> BooleanExpression:
    return GreaterThanOrEqual(column, value)  # type: ignore[call-arg, arg-type]


def _below(column: str, value: object) -> BooleanExpression:
    return LessThan(column, value)  # type: ignore[call-arg, arg-type]


def _at_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


def _chunks(values: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for offset in range(0, len(values), _KEY_CHUNK):
        yield values[offset : offset + _KEY_CHUNK]


# =========================================================================================
# batch ids, batches and snapshots
# =========================================================================================


def response_batch_id(revision_id: str, base: int) -> str:
    """Stable id of the one-row response batch; the block base keeps a lost race distinct."""
    return f"{revision_id}.response.{base}"


def element_batch_id(response_revision_id: str, index: int) -> str:
    """Stable id of the ``index``-th element microbatch of one response revision."""
    return f"{response_revision_id}.elements.{index:0{_BATCH_INDEX_DIGITS}d}"


def batch(definition: RegisteredTableDefinition, rows: Sequence[Mapping[str, Any]]) -> pa.Table:
    """Rows → a batch in the registered Arrow schema; any column drift fails closed here."""
    names = [field.name for field in definition.arrow_schema]
    for row in rows:
        drift = sorted(set(names) ^ set(row))
        if drift:
            raise CatalogIntegrityError(f"row for {definition.table} drifts: {drift}")
    table = pa.Table.from_pylist([dict(row) for row in rows], schema=definition.arrow_schema)
    if table.num_rows != len(rows):  # pragma: no cover - from_pylist keeps the row count
        raise CatalogIntegrityError(f"batch for {definition.table} lost rows")
    return table


def batch_rows(table: pa.Table) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = table.to_pylist()
    return rows


def snapshots_of_batches(
    adapter: RevisionCatalog, table: str, batch_ids: Iterable[str]
) -> dict[str, list[SnapshotInfo]]:
    """Main-branch snapshots committing each of ``batch_ids``, in one walk of the history."""
    wanted: dict[str, list[SnapshotInfo]] = {batch_id: [] for batch_id in batch_ids}
    for snapshot in _history(adapter, table):
        if snapshot.batch_id in wanted:
            wanted[snapshot.batch_id].append(snapshot)
    return wanted


def _history(adapter: RevisionCatalog, table: str) -> Iterable[SnapshotInfo]:
    info = adapter.load_table(table)
    if info is None:
        raise TableNotFound(f"table {table} does not exist")
    snapshot = info.current_snapshot
    while snapshot is not None:
        yield snapshot
        parent = snapshot.parent_snapshot_id
        snapshot = None if parent is None else adapter.get_snapshot(table, parent)


def _indexed_batches(
    adapter: RevisionCatalog, table: str, prefixes: Iterable[str]
) -> dict[str, dict[int, list[SnapshotInfo]]]:
    """Per prefix: ``<prefix><8-digit index>`` batch snapshots by index, in one history walk."""
    found: dict[str, dict[int, list[SnapshotInfo]]] = {prefix: {} for prefix in prefixes}
    if not found:
        return found
    for snapshot in _history(adapter, table):
        batch_id = snapshot.batch_id
        if batch_id is None:
            continue
        head, separator, tail = batch_id.rpartition(".")
        prefix = f"{head}{separator}"
        if prefix not in found:
            continue
        if len(tail) != _BATCH_INDEX_DIGITS or not tail.isdigit() or not tail.isascii():
            raise CatalogIntegrityError(f"{table} has a malformed batch id {batch_id!r}")
        found[prefix].setdefault(int(tail), []).append(snapshot)
    return found


def indexed_batches(
    adapter: RevisionCatalog, table: str, prefixes: Iterable[str]
) -> dict[str, dict[int, list[SnapshotInfo]]]:
    """Public form of ``_indexed_batches`` (Canonical verification, Phase 1 F1)."""
    return _indexed_batches(adapter, table, prefixes)


def check_batch_snapshot(
    definition: RegisteredTableDefinition,
    batch_id: str,
    snapshot: SnapshotInfo,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """The snapshot committing ``batch_id`` must carry exactly these rows' fingerprint."""
    table = batch(definition, [dict(row) for row in rows])
    if snapshot.batch_fingerprint != definition.fingerprint_rule.fingerprint(
        table
    ) or snapshot.added_rows != len(rows):
        raise CatalogIntegrityError(
            f"batch {batch_id} of {definition.table} was committed with other content"
        )


def _one_snapshot(table: str, batch_id: str, found: Sequence[SnapshotInfo]) -> SnapshotInfo:
    if len(found) != 1:
        raise CatalogIntegrityError(
            f"{table} has rows of batch {batch_id} but {len(found)} snapshots committing it"
        )
    return found[0]


# =========================================================================================
# REST row builders (the single builders of s1 / s2)
# =========================================================================================


def check_block_base(value: object) -> None:
    """A committed response ``arrival_seq`` must be a REST block base (else fail closed)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogIntegrityError("a committed REST response arrival_seq is not an integer")
    try:
        rest_identity.check_rest_arrival_seq(value)
    except rest_identity.RestIdentityViolation as exc:
        raise CatalogIntegrityError(f"corrupt REST response arrival_seq: {exc}") from None
    if (value - rest_identity.REST_ARRIVAL_SEQ_BASE) % rest_identity.REST_ARRIVAL_SEQ_STRIDE:
        raise CatalogIntegrityError(f"REST response arrival_seq {value} is not a block base")


def check_provenance_shape(provenance: Mapping[str, Any]) -> None:
    """A foreign first delivery's decode columns must at least be self-consistent."""
    outcome = provenance["decode_outcome"]
    code = provenance["decode_rejection_code"]
    count = provenance["element_count"]
    start, end = provenance["answered_start"], provenance["answered_end"]
    if outcome == ACCEPTED:
        if code is not None or not isinstance(count, int) or count < 0:
            raise ValueError("an accepted page has a count and no rejection code")
    elif outcome == REJECTED:
        if code is None or count is not None or start is not None or end is not None:
            raise ValueError("a rejected page has a code and no count or answered interval")
    else:
        raise ValueError(f"unknown decode outcome {outcome!r}")
    if (start is None) != (end is None) or (start is not None and not start < end):
        raise ValueError("the answered interval is malformed")
    if provenance["collector_id"] != d3d.REST_COLLECTOR_ID or (
        provenance["collector_version"] != d3d.REST_COLLECTOR_VERSION
    ):
        raise ValueError("the first delivery names another collector")


def _check_provenance_values(provenance: Mapping[str, Any]) -> None:
    """The first delivery's recorded values must be ones the D3D / D3C pipeline can produce."""
    request_id = provenance["collection_request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("the first delivery has no collection request id")
    page_index = provenance["page_index"]
    if not isinstance(page_index, int) or isinstance(page_index, bool) or page_index < 0:
        raise ValueError("the first delivery's page_index is not a non-negative int")
    names: list[str] = []
    for item in provenance["source_metadata"]:
        if set(item) != {"name", "value"} or not isinstance(item["value"], str):
            raise ValueError("source_metadata items are (name, value) text pairs")
        names.append(item["name"])
    if names != sorted(set(names)) or not set(names) <= d3d.ALLOWED_RESPONSE_HEADERS:
        raise ValueError("source_metadata is not the sorted header allowlist projection")
    code = provenance["decode_rejection_code"]
    if code is not None and code not in {item.value for item in RestRejectionCode}:
        raise ValueError(f"unknown decoder rejection code {code!r}")
    count = provenance["element_count"]
    if count is not None and count > rest_identity.PAGE_LIMIT:
        raise ValueError("an accepted page holds more elements than one page can")
    for label in ("answered_start", "answered_end"):
        value = provenance[label]
        if value is not None and (value - _EPOCH) % timedelta(milliseconds=1):
            raise ValueError(f"{label} is not a whole millisecond")


def _query_pairs(text: object) -> list[tuple[str, str]]:
    """``name=value&…`` back into pairs; the query rule then demands the canonical encoding."""
    if not isinstance(text, str) or not text:
        raise ValueError("request_query is empty")
    pairs: list[tuple[str, str]] = []
    for part in text.split("&"):
        name, separator, value = part.partition("=")
        if not separator or not name or "=" in value:
            raise ValueError("request_query is not name=value pairs")
        pairs.append((name, value))
    return pairs


def response_columns(
    *,
    data_type: str,
    source_binding: tuple[str, str],
    query: rest_identity.RestPageQuery,
    origin: str,
    page_identity: str,
    source_uri: str,
    http_status: int,
    body: tuple[str, str, str, int],
    base: int,
    knowledge_time: datetime,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Every column of one response revision from its inputs (the single builder, s1)."""
    body_key, body_uri, body_sha256, body_size = body
    observation_key = rest_identity.response_observation_key(page_identity)
    source = rest_identity.rest_source_identity()
    requested_at = provenance["requested_at"]
    retrieved_at = provenance["retrieved_at"]
    decision = decide_rest_availability(
        RestAvailabilitySubject.RESPONSE,
        event_time=requested_at,
        event_end_time=retrieved_at,
        ingest_time=retrieved_at,
        knowledge_time=knowledge_time,
    )
    record = RevisionRecord(
        observation_key=observation_key,
        revision_id=rest_identity.revision_id(observation_key, source, body_sha256),
        source_id=source,
        payload_hash=body_sha256,
        arrival_seq=base,
        availability=decision,
    )
    row = _revision_block(record)
    row.update(
        {
            "event_time": requested_at,
            "event_end_time": retrieved_at,
            "source_binding_id": source_binding[0],
            "source_binding_version": source_binding[1],
            "data_type": data_type,
            "symbol": query.symbol,
            "request_origin": origin,
            "request_path": query.path,
            "request_query": query.query_string(),
            "declared_time_unit": rest_identity.DECLARED_TIME_UNIT,
            "page_limit": query.limit,
            "page_identity_sha256": page_identity,
            "source_uri": source_uri,
            "http_status": http_status,
            "object_key": body_key,
            "object_uri": body_uri,
            "object_sha256": body_sha256,
            "object_size_bytes": body_size,
            "decoder_id": DECODER_BINDING.policy_id,
            "decoder_version": DECODER_BINDING.version,
            "decoder_hash": DECODER_BINDING.policy_hash,
            **provenance,
        }
    )
    return row


def _revision_block(record: RevisionRecord) -> dict[str, Any]:
    """The revision columns shared by the REST tables, from a validated contract record."""
    times = record.availability.times
    decision: AvailabilityDecision = record.availability
    return {
        "observation_key": record.observation_key,
        "revision_id": record.revision_id,
        "source_id": record.source_id,
        "payload_hash": record.payload_hash,
        "arrival_seq": record.arrival_seq,
        "supersedes": list(record.supersedes),
        "source_revision_id": record.source_revision_id,
        "source_revision_time": record.source_revision_time,
        "source_time": times.source_time,
        "available_time": times.available_time,
        "ingest_time": times.ingest_time,
        "knowledge_time": times.knowledge_time,
        "declared_latency_us": times.declared_latency // _MICROSECOND,
        "availability_policy_id": decision.policy.policy_id,
        "availability_policy_version": decision.policy.version,
        "availability_policy_hash": decision.policy.policy_hash,
        "availability_evidence": list(decision.evidence),
        "availability_evidence_gap": decision.evidence_gap,
        # REST 1.0.0 never produces an edge inside the channel (binance.spot.rest-revision).
        "precedence_evidence": [],
        "contract_schema_version": record.schema_version,
    }


def element_columns(
    definition: RegisteredTableDefinition,
    data_type: str,
    symbol: str,
    native: Mapping[str, Any],
    *,
    element_index: int,
    response_revision_id: str,
    base: int,
    ingest_time: datetime,
    knowledge_time: datetime,
) -> tuple[str, str, str, Mapping[str, Any]]:
    """``(key, revision id, payload hash, normalised row)`` of one element of a response.

    The single builder (s2): the element's times come from its native fields, its arrival
    number from the response's block base, its ``ingest_time`` / ``knowledge_time`` from the
    response revision; bindings, schema version and empty edges from the frozen rules.
    """
    subject = _ELEMENT_SUBJECTS[data_type]
    source = rest_identity.rest_source_identity()
    times: dict[str, Any]
    if data_type == "agg_trades":
        event_time = _at_ms(native["timestamp_raw"])
        times = {"event_time": event_time}
        key = rest_identity.agg_trade_observation_key(symbol, native["agg_trade_id"])
        payload = rest_identity.agg_trade_payload_hash(symbol, dict(native))
        decision = decide_rest_availability(
            subject, event_time=event_time, ingest_time=ingest_time, knowledge_time=knowledge_time
        )
    else:
        start = _at_ms(native["open_time_raw"])
        end = _at_ms(native["close_time_raw"] + 1)
        times = {"interval_start": start, "interval_end": end}
        key = rest_identity.kline_1m_observation_key(symbol, start)
        payload = rest_identity.kline_1m_payload_hash(symbol, dict(native))
        decision = decide_rest_availability(
            subject,
            event_time=start,
            event_end_time=end,
            ingest_time=ingest_time,
            knowledge_time=knowledge_time,
        )
    revision_id = rest_identity.revision_id(key, source, payload)
    record = RevisionRecord(
        observation_key=key,
        revision_id=revision_id,
        source_id=source,
        payload_hash=payload,
        arrival_seq=rest_identity.element_arrival_seq(base, element_index),
        availability=decision,
    )
    row = _revision_block(record)
    row.update(times)
    row.update(
        {
            "symbol": symbol,
            "response_revision_id": response_revision_id,
            "element_index": element_index,
            "decoder_id": DECODER_BINDING.policy_id,
            "decoder_version": DECODER_BINDING.version,
            "decoder_hash": DECODER_BINDING.policy_hash,
            **native,
        }
    )
    return key, revision_id, payload, batch_rows(batch(definition, [row]))[0]


def _verify_rest_element_identity(table: str, data_type: str, row: Mapping[str, Any]) -> None:
    """A committed REST element row must re-derive its key, payload hash and id."""
    try:
        symbol = row["symbol"]
        if data_type == "agg_trades":
            key = rest_identity.agg_trade_observation_key(symbol, row["agg_trade_id"])
            payload = rest_identity.agg_trade_payload_hash(symbol, dict(row))
            times_ok = row["event_time"] == _at_ms(row["timestamp_raw"])
        else:
            key = rest_identity.kline_1m_observation_key(symbol, row["interval_start"])
            payload = rest_identity.kline_1m_payload_hash(symbol, dict(row))
            times_ok = row["interval_start"] == _at_ms(row["open_time_raw"]) and row[
                "interval_end"
            ] == _at_ms(row["close_time_raw"] + 1)
        source = rest_identity.rest_source_identity()
        derived = rest_identity.revision_id(key, source, payload)
        rest_identity.check_rest_arrival_seq(row["arrival_seq"])
    except (rest_identity.RestIdentityViolation, KeyError, TypeError) as exc:
        raise CatalogIntegrityError(f"{table}: a committed row is not lawful ({exc})") from None
    for label, stored, expected in (
        ("observation_key", row["observation_key"], key),
        ("source_id", row["source_id"], source),
        ("payload_hash", row["payload_hash"], payload),
        ("revision_id", row["revision_id"], derived),
    ):
        if stored != expected:
            raise CatalogIntegrityError(
                f"{table}: committed {label} of {row['revision_id']} does not re-derive"
            )
    if not times_ok:
        raise CatalogIntegrityError(f"{table}: committed times of {row['revision_id']} drift")


def _mismatched(expected: Mapping[str, Any], stored: Mapping[str, Any]) -> list[str]:
    return sorted(
        name for name, value in expected.items() if name not in stored or stored[name] != value
    )


def _unique(table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """No revision id and no arrival number twice among the rows being judged."""
    seen_ids: set[str] = set()
    seen_seqs: set[int] = set()
    for row in rows:
        if row["revision_id"] in seen_ids:
            raise CatalogIntegrityError(
                f"{table}: revision {row['revision_id']} is committed twice"
            )
        seen_ids.add(row["revision_id"])
        if row["arrival_seq"] in seen_seqs:
            raise CatalogIntegrityError(f"{table}: arrival_seq {row['arrival_seq']} is not unique")
        seen_seqs.add(row["arrival_seq"])


# =========================================================================================
# archive element time rule (the D1 parser's conversion, checked, never re-parsed)
# =========================================================================================


def _archive_times_hold(
    data_type: str, unit: TimeUnit, archive: Mapping[str, Any], row: Mapping[str, Any]
) -> str | None:
    """``None`` if the row's UTC times are D1's conversion of its ticks inside the coverage."""
    per_tick = unit.micros_per_tick
    start_ticks = (archive["coverage_start"] - _EPOCH) // _MICROSECOND // per_tick
    end_ticks = (archive["coverage_end"] - _EPOCH) // _MICROSECOND // per_tick
    if data_type == "agg_trades":
        ticks = row["timestamp_raw"]
        if not start_ticks <= ticks < end_ticks:
            return "timestamp_raw is outside its archive's coverage"
        if row["event_time"] != _EPOCH + ticks * per_tick * _MICROSECOND:
            return "event_time is not timestamp_raw in its archive's declared unit"
        return None
    open_ticks, close_ticks = row["open_time_raw"], row["close_time_raw"]
    minute_ticks = 60 * unit.ticks_per_second
    if not start_ticks <= open_ticks < end_ticks or (open_ticks - start_ticks) % minute_ticks:
        return "open_time_raw is not a minute of its archive's coverage"
    if close_ticks != open_ticks + minute_ticks - 1:
        return "close_time_raw is not the last tick of its minute"
    start = _EPOCH + open_ticks * per_tick * _MICROSECOND
    if row["interval_start"] != start:
        return "interval_start is not open_time_raw in its archive's declared unit"
    if row["interval_end"] != start + _MINUTE:
        return "interval_end is not the exclusive end of its minute"
    return None


# =========================================================================================
# the verifier
# =========================================================================================


@dataclass(frozen=True, slots=True)
class VerifiedArchive:
    """A proven archive revision: what an element row may inherit from it."""

    revision_id: str
    time_unit: TimeUnit
    row: Mapping[str, Any]


class PersistedRowVerifier:
    """Proves committed Raw rows against their lineage; raises ``CatalogIntegrityError``.

    Stateless apart from the catalog and the object store it reads; ``StorageError`` (an
    unreadable warehouse) is re-raised through ``storage_error`` so each caller keeps its own
    operational error type.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        storage_error: Callable[[StorageError], Exception] | None = None,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._storage_error = storage_error or (lambda exc: exc)

    # ------------------------------------------------------------------ catalog helpers

    def _scan(
        self, definition: RegisteredTableDefinition, row_filter: BooleanExpression
    ) -> list[Mapping[str, Any]]:
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = self._adapter.scan_columns(
            definition.table, columns=columns, row_filter=row_filter
        ).to_pylist()
        return rows

    def _holders(self, table: str, seqs: Sequence[int]) -> dict[int, list[str]]:
        holders: dict[int, list[str]] = {}
        for chunk in _chunks(sorted(seqs)):
            found = self._adapter.scan_columns(
                table,
                columns=("revision_id", "arrival_seq"),
                row_filter=_member("arrival_seq", chunk),
            ).to_pylist()
            for item in found:
                holders.setdefault(item["arrival_seq"], []).append(item["revision_id"])
        return holders

    def _check_sole_holders(self, table: str, by_seq: Mapping[int, str], label: str) -> None:
        holders = self._holders(table, list(by_seq))
        for seq, revision in by_seq.items():
            if holders.get(seq) != [revision]:
                raise CatalogIntegrityError(
                    f"{table}: arrival_seq {seq} is not held by {label} {revision} alone"
                )

    def _lookup(self, key: str) -> Any:
        """The published object at ``key`` (or ``None``); an unreadable store is not integrity."""
        try:
            return self._storage.lookup(key)
        except (IntegrityViolation, ObjectKeyViolation) as exc:
            raise CatalogIntegrityError(f"committed object {key!r} does not hold: {exc}") from None
        except StorageError as exc:
            mapped = self._storage_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    # ------------------------------------------------------------------ REST responses

    def lawful_response_row(self, stored: Mapping[str, Any]) -> None:
        """Rebuild a committed response row from its own inputs; every column must reproduce.

        The inputs are the page query + origin (page identity, URI, key), the body digest (its
        published object), the block base, ``requested_at`` / ``retrieved_at`` /
        ``knowledge_time`` and the first delivery's validated provenance. Everything else — the
        availability decision, policy binding, source / collector / decoder bindings, schema
        version, empty edges and ``supersedes`` — is derived from the frozen rules and compared.
        """
        revision = stored.get("revision_id")
        try:
            check_block_base(stored["arrival_seq"])
            data_type = stored["data_type"]
            if data_type not in ELEMENT_DEFINITIONS:
                raise ValueError(f"unsupported data_type {data_type!r}")
            query = rest_identity.RestPageQuery.from_pairs(
                data_type, _query_pairs(stored["request_query"])
            )
            origin = stored["request_origin"]
            page_identity = rest_identity.page_identity_sha256(query, origin)
            body_key = rest_identity.response_object_key(
                data_type, query.symbol, stored["payload_hash"], page_identity
            )
            provenance = {name: stored[name] for name in PROVENANCE_COLUMNS}
            check_provenance_shape(provenance)
            _check_provenance_values(provenance)
            body = self._lookup(body_key)
            if body is None:
                raise ValueError(f"its response body {body_key!r} is not published")
            expected = response_columns(
                data_type=data_type,
                source_binding=(d3d.REST_SOURCE.source_id, d3d.REST_SOURCE.version),
                query=query,
                origin=origin,
                page_identity=page_identity,
                source_uri=rest_identity.page_source_uri(query, origin),
                http_status=_HTTP_OK,
                body=(body.key, body.uri, body.sha256, body.size),
                base=stored["arrival_seq"],
                knowledge_time=stored["knowledge_time"],
                provenance=provenance,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CatalogIntegrityError(
                f"committed response revision {revision} is not lawful: {exc}"
            ) from None
        normalised = batch_rows(batch(BINANCE_SPOT_REST_RESPONSES, [expected]))[0]
        mismatched = _mismatched(normalised, stored)
        if mismatched:
            raise CatalogIntegrityError(
                f"committed response revision {revision} does not reproduce from its own "
                f"inputs: {mismatched}"
            )

    def verify_responses(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Each row is lawful, holds its block alone and is its batch's only, exact content."""
        if not rows:
            return
        for row in rows:
            self.lawful_response_row(row)
        by_base: dict[int, str] = {}
        for row in rows:
            if by_base.setdefault(row["arrival_seq"], row["revision_id"]) != row["revision_id"]:
                raise CatalogIntegrityError(
                    f"REST arrival block {row['arrival_seq']} is held by two response revisions"
                )
        holders = self._holders(BINANCE_SPOT_REST_RESPONSES.table, list(by_base))
        for base, revision in by_base.items():
            if holders.get(base) != [revision]:
                raise CatalogIntegrityError(
                    f"REST arrival block {base} is not held by response revision {revision} alone"
                )
        table = BINANCE_SPOT_REST_RESPONSES.table
        batches = {response_batch_id(row["revision_id"], row["arrival_seq"]): row for row in rows}
        snapshots = snapshots_of_batches(self._adapter, table, batches)
        for batch_id, row in batches.items():
            snapshot = _one_snapshot(table, batch_id, snapshots[batch_id])
            check_batch_snapshot(BINANCE_SPOT_REST_RESPONSES, batch_id, snapshot, [row])

    # ------------------------------------------------------------------ REST elements

    def verify_rest_elements(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Every row re-derives, inherits a lawful lineage and sits in its exact batch."""
        table = definition.table
        for row in rows:
            _verify_rest_element_identity(table, data_type, row)
        _unique(table, rows)
        if not rows:
            return
        self._verify_rest_lineage(definition, data_type, rows)
        self._check_sole_holders(
            table, {row["arrival_seq"]: row["revision_id"] for row in rows}, "element"
        )
        self._verify_rest_element_batches(
            definition, sorted({row["response_revision_id"] for row in rows})
        )

    def _verify_rest_lineage(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Each element row is exactly what its lineage response revision must have released.

        ``response_revision_id`` must name exactly one committed, lawful (fully re-derived),
        accepted response revision of the same data type and symbol whose element count covers
        ``element_index``. The element row is then rebuilt from its native fields and that
        response's block base and times — arrival number, availability decision, decoder
        binding, schema version, empty edges and ``supersedes`` — and must match column for
        column.
        """
        table = definition.table
        lineage_ids = sorted({row["response_revision_id"] for row in rows})
        found: dict[str, list[Mapping[str, Any]]] = {}
        for chunk in _chunks(lineage_ids):
            for response in self._scan(BINANCE_SPOT_REST_RESPONSES, _member("revision_id", chunk)):
                found.setdefault(response["revision_id"], []).append(response)
        for lineage_id in lineage_ids:
            count = len(found.get(lineage_id, ()))
            if count != 1:
                raise CatalogIntegrityError(
                    f"{table}: lineage response revision {lineage_id} is committed {count} "
                    "time(s); an element must name exactly one committed response"
                )
        lineage = {lineage_id: found[lineage_id][0] for lineage_id in lineage_ids}
        self.verify_responses([lineage[lineage_id] for lineage_id in lineage_ids])
        for row in rows:
            response = lineage[row["response_revision_id"]]
            revision = row["revision_id"]
            if (
                response["data_type"] != data_type
                or response["symbol"] != row["symbol"]
                or response["decode_outcome"] != ACCEPTED
            ):
                raise CatalogIntegrityError(
                    f"{table}: element {revision} names response revision {response['revision_id']}"
                    f" that did not accept a {data_type} page of {row['symbol']}"
                )
            index = row["element_index"]
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < response["element_count"]
            ):
                raise CatalogIntegrityError(
                    f"{table}: element {revision} has element_index {index!r} outside the "
                    f"{response['element_count']} element(s) of its lineage response"
                )
            try:
                *_, expected = element_columns(
                    definition,
                    data_type,
                    row["symbol"],
                    {name: row[name] for name in ELEMENT_NATIVE_COLUMNS[data_type]},
                    element_index=index,
                    response_revision_id=response["revision_id"],
                    base=response["arrival_seq"],
                    ingest_time=response["ingest_time"],
                    knowledge_time=response["knowledge_time"],
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CatalogIntegrityError(
                    f"{table}: element {revision} cannot be what its lineage response "
                    f"released: {exc}"
                ) from None
            mismatched = _mismatched(expected, row)
            if mismatched:
                raise CatalogIntegrityError(
                    f"{table}: element {revision} does not inherit its lineage response "
                    f"{response['revision_id']}: {mismatched}"
                )

    def _verify_rest_element_batches(
        self, definition: RegisteredTableDefinition, lineage_ids: Sequence[str]
    ) -> None:
        """The committed rows of each lineage are exactly its element batches, in element order.

        The store commits a response's elements in microbatches ``<response>.elements.<i>`` of
        one plan size ``s`` (element ``e`` in batch ``e // s``), skipping elements another page
        delivered first. So the lineage's rows, in element order, must split into its batches by
        their committed row counts, each part must carry its batch's committed fingerprint, and
        one ``s`` must explain every index.
        """
        table = definition.table
        by_lineage: dict[str, list[Mapping[str, Any]]] = {lineage: [] for lineage in lineage_ids}
        for chunk in _chunks(list(lineage_ids)):
            for row in self._scan(definition, _member("response_revision_id", chunk)):
                by_lineage[row["response_revision_id"]].append(row)
        prefixes = {f"{lineage}.elements.": lineage for lineage in lineage_ids}
        found = _indexed_batches(self._adapter, table, prefixes)
        for prefix, lineage in prefixes.items():
            members = sorted(by_lineage[lineage], key=lambda row: row["element_index"])
            batches = [
                (index, _one_snapshot(table, element_batch_id(lineage, index), snapshots))
                for index, snapshots in sorted(found[prefix].items())
            ]
            committed = sum(snapshot.added_rows for _, snapshot in batches)
            if committed != len(members):
                raise CatalogIntegrityError(
                    f"{table}: response revision {lineage} has {len(members)} element row(s) but "
                    f"its element batches committed {committed}"
                )
            offset = 0
            low, high = 1, MAX_ELEMENT_MICROBATCH_ROWS
            for index, snapshot in batches:
                batch_id = element_batch_id(lineage, index)
                part = members[offset : offset + snapshot.added_rows]
                offset += snapshot.added_rows
                check_batch_snapshot(definition, batch_id, snapshot, part)
                for row in part:
                    element = row["element_index"]
                    low = max(low, element // (index + 1) + 1)
                    if index:
                        high = min(high, element // index)
            if low > high:
                raise CatalogIntegrityError(
                    f"{table}: the element batches of response revision {lineage} follow no "
                    "single microbatch plan"
                )

    # ------------------------------------------------------------------ archive rows

    def verify_archive_elements(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        symbol: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, VerifiedArchive]:
        """Prove archive element rows down to their archive revision; returns the lineages."""
        table = definition.table
        if _ARCHIVE_ROW_DEFINITIONS.get(data_type) is not definition:
            raise CatalogIntegrityError(f"{table} is not the archive table of {data_type}")
        _unique(table, rows)
        if not rows:
            return {}
        lineage = self._verified_archives(
            data_type, symbol, sorted({row["archive_revision_id"] for row in rows})
        )
        by_archive: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_archive.setdefault(row["archive_revision_id"], []).append(row)
        for archive_id, members in sorted(by_archive.items()):
            self._verify_archive_rows(definition, data_type, lineage[archive_id], members)
        self._check_sole_holders(
            table, {row["arrival_seq"]: row["revision_id"] for row in rows}, "row"
        )
        self._verify_archive_row_batches(definition, by_archive)
        return lineage

    def _verified_archives(
        self, data_type: str, symbol: str, archive_ids: Sequence[str]
    ) -> dict[str, VerifiedArchive]:
        """Each named archive revision: committed once, for this data type and symbol, lawful."""
        table = BINANCE_SPOT_ARCHIVES.table
        found: dict[str, list[Mapping[str, Any]]] = {}
        for chunk in _chunks(list(archive_ids)):
            for row in self._scan(BINANCE_SPOT_ARCHIVES, _member("revision_id", chunk)):
                found.setdefault(row["revision_id"], []).append(row)
        verified: dict[str, VerifiedArchive] = {}
        for archive_id in archive_ids:
            rows = found.get(archive_id, [])
            if len(rows) > 1:
                raise CatalogIntegrityError(
                    f"{table}: archive revision {archive_id} is committed {len(rows)} time(s)"
                )
            if not rows or rows[0]["data_type"] != data_type or rows[0]["symbol"] != symbol:
                raise CatalogIntegrityError(
                    f"an archive row names archive revision {archive_id} that is not committed "
                    "for this data type and symbol"
                )
            row = rows[0]
            self._lawful_archive_row(row)
            verified[archive_id] = VerifiedArchive(
                revision_id=archive_id,
                time_unit=time_unit_for(data_type, row["coverage_start"]),
                row=row,
            )
        by_base: dict[int, str] = {}
        for item in verified.values():
            base = item.row["arrival_seq"]
            if by_base.setdefault(base, item.revision_id) != item.revision_id:
                raise CatalogIntegrityError(f"archive arrival block {base} is held twice")
        self._check_sole_holders(table, by_base, "archive revision")
        batches = {
            _archive_batch_id(item.revision_id, item.row["arrival_seq"]): item.row
            for item in verified.values()
        }
        snapshots = snapshots_of_batches(self._adapter, table, batches)
        for batch_id, row in batches.items():
            snapshot = _one_snapshot(table, batch_id, snapshots[batch_id])
            check_batch_snapshot(BINANCE_SPOT_ARCHIVES, batch_id, snapshot, [row])
        return verified

    def _lawful_archive_row(self, stored: Mapping[str, Any]) -> None:
        """Rebuild an archive revision row with the D2 builder from its published object.

        Inputs: the published object (looked up by its key), the official path / checksum and
        its source metadata, the collection request, the block base and ``knowledge_time``.
        Derived and compared: identity (D2 binding), the frozen D0 collector and source
        bindings, the availability decision under the frozen policy, no precedence edge.
        """
        revision = stored.get("revision_id")
        try:
            _check_archive_arrival_seq(stored["arrival_seq"])
            if (stored["collector_id"], stored["collector_version"]) != (
                d0.COLLECTOR_ID,
                d0.COLLECTOR_VERSION,
            ):
                raise ValueError("it names another collector")
            ref = self._lookup(stored["object_key"])
            if ref is None:
                raise ValueError(f"its archive object {stored['object_key']!r} is not published")
            metadata: dict[str, str] = {}
            for item in stored["source_metadata"]:
                if set(item) != {"name", "value"} or item["name"] in metadata:
                    raise ValueError("source_metadata is not a (name, value) mapping")
                metadata[item["name"]] = item["value"]
            collected = CollectedObject(
                ref=ref,
                symbol=stored["symbol"],
                coverage_start=stored["coverage_start"],
                coverage_end=stored["coverage_end"],
                source_uri=stored["source_uri"],
                retrieved_at=stored["retrieved_at"],
                source_sha256=stored["source_sha256"],
                source_metadata=FrozenMapping(metadata),
            )
            context = ArchiveContext(
                request_id=stored["collection_request_id"],
                data_type=stored["data_type"],
                collector_id=stored["collector_id"],
                collector_version=stored["collector_version"],
                source=SourceBinding(
                    source_id=stored["source_binding_id"],
                    version=stored["source_binding_version"],
                ),
            )
            observation_key, revision_id = identify_archive(collected, context)
            decision = decide_availability(
                AvailabilitySubject.ARCHIVE,
                event_time=collected.coverage_start,
                event_end_time=collected.coverage_end,
                ingest_time=collected.retrieved_at,
                knowledge_time=stored["knowledge_time"],
                binding=AVAILABILITY_BINDING,
            )
            record = RevisionRecord(
                observation_key=observation_key,
                revision_id=revision_id,
                source_id=archive_identity.archive_source_identity(),
                payload_hash=collected.ref.sha256,
                arrival_seq=stored["arrival_seq"],
                availability=decision,
            )
            expected = batch_rows(
                _archive_batch(BINANCE_SPOT_ARCHIVES, record, (), collected, context)
            )
        except (RevisionStoreError, KeyError, TypeError, ValueError, OverflowError) as exc:
            # The D2 binding, the contracts and the frozen policy refuse what D2 never wrote.
            raise CatalogIntegrityError(
                f"committed archive revision {revision} is not lawful: {exc}"
            ) from None
        mismatched = _mismatched(expected[0], stored)
        if mismatched:
            raise CatalogIntegrityError(
                f"committed archive revision {revision} does not reproduce from its own "
                f"inputs: {mismatched}"
            )

    def _verify_archive_rows(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        archive: VerifiedArchive,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Rebuild each row with the D2 builder from its natives and the archive revision."""
        table = definition.table
        schema = _PARSER_ROW_SCHEMAS[data_type]
        ordered = sorted(rows, key=lambda row: (row["archive_line_number"], row["revision_id"]))
        for row in ordered:
            problem = _archive_times_hold(data_type, archive.time_unit, archive.row, row)
            if problem is not None:
                raise CatalogIntegrityError(f"{table}: row {row['revision_id']}: {problem}")
        try:
            chunk = pa.Table.from_pylist(
                [{name: row[name] for name in schema.names} for row in ordered], schema=schema
            )
            records = _row_records(
                chunk,
                data_type=data_type,
                symbol=archive.row["symbol"],
                time_unit=archive.time_unit.value,
                source_identity=archive_identity.row_source_identity(archive.revision_id),
                base=archive.row["arrival_seq"],
                times=_times_from_row(archive.row),
                subject=_ARCHIVE_ROW_SUBJECTS[data_type],
                binding=AVAILABILITY_BINDING,
            )
            expected = batch_rows(_row_batch(definition, records, chunk, data_type))
        except (RevisionStoreError, KeyError, TypeError, ValueError, OverflowError) as exc:
            # Identity, contract and Arrow refusals of a row D2 could not have written.
            raise CatalogIntegrityError(
                f"{table}: rows of archive revision {archive.revision_id} cannot be what it "
                f"released: {exc}"
            ) from None
        for row, rebuilt in zip(ordered, expected, strict=True):
            mismatched = _mismatched(rebuilt, row)
            if mismatched:
                raise CatalogIntegrityError(
                    f"{table}: row {row['revision_id']} does not inherit its archive revision "
                    f"{archive.revision_id}: {mismatched}"
                )

    def _verify_archive_row_batches(
        self,
        definition: RegisteredTableDefinition,
        rows: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        """Each touched row sits in a committed row batch that still holds exactly its content.

        D2 commits an archive's parsed rows in order, as batches ``<archive>.rows.<i>`` of one
        plan size ``s`` (lines ``i*s+1 … i*s+s``; only the last may be shorter), so the batches
        of an archive are a contiguous prefix of indices. A touched row's line names its batch;
        that batch's current rows are read (bounded by ``s``) and must carry its fingerprint.
        """
        table = definition.table
        prefixes = {f"{archive_id}.rows.": archive_id for archive_id in rows}
        found = _indexed_batches(self._adapter, table, prefixes)
        for prefix, archive_id in sorted(prefixes.items()):
            batches = sorted(found[prefix].items())
            snapshots = [_one_snapshot(table, _row_batch_id(archive_id, i), s) for i, s in batches]
            if [index for index, _ in batches] != list(range(len(batches))):
                raise CatalogIntegrityError(
                    f"{table}: the row batches of archive revision {archive_id} are not a "
                    "contiguous prefix"
                )
            if not snapshots:
                raise CatalogIntegrityError(
                    f"{table}: archive revision {archive_id} has rows but no committed row batch"
                )
            sizes = [snapshot.added_rows for snapshot in snapshots]
            size = sizes[0]
            if size < 1 or any(value != size for value in sizes[:-1]) or sizes[-1] > size:
                raise CatalogIntegrityError(
                    f"{table}: the row batches of archive revision {archive_id} follow no single "
                    "microbatch plan"
                )
            needed: set[int] = set()
            for row in rows[archive_id]:
                position = row["archive_line_number"] - 1
                index = position // size if position >= 0 else -1
                if not 0 <= index < len(sizes) or position >= index * size + sizes[index]:
                    raise CatalogIntegrityError(
                        f"{table}: row {row['revision_id']} (line {row['archive_line_number']}) "
                        f"is not committed by any batch of archive revision {archive_id}"
                    )
                needed.add(index)
            for index in sorted(needed):
                first = index * size + 1
                current = self._scan(
                    definition,
                    And(
                        _equals("archive_revision_id", archive_id),
                        And(
                            _at_least("archive_line_number", first),
                            _below("archive_line_number", first + sizes[index]),
                        ),
                    ),
                )
                current.sort(key=lambda row: (row["archive_line_number"], row["revision_id"]))
                check_batch_snapshot(
                    definition, _row_batch_id(archive_id, index), snapshots[index], current
                )
