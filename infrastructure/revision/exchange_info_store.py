"""exchangeInfo snapshot revision store (Phase 1 E2; ADR-0029 §1, ADR-0023 §4 / §7).

The store turns one **committed** snapshot attempt of ``binance.spot.public-exchange-info@1.0.0``
into one append-only Raw source revision of ``raw.binance_spot_exchange_info``:

- identity, keys and payload hash come from ``hlens.binance.spot.exchange-info-identity@1.0.0``;
  times from ``binance.spot.exchange-info-publication@1.0.0`` (``available_time = ingest_time`` +
  gap); ``arrival_seq`` is the largest committed number + 1; ``knowledge_time`` is one reading of
  the injected UTC clock after every proof, never before ``ingest_time``;
- one revision per batch, batch id ``<revision_id>.snapshot.<arrival_seq>`` (the number keeps a
  lost race distinct), committed with the expected parent snapshot.

Trust boundary: the store never collects. It re-verifies the committed checkpoint with the
collector's own strict reader (a collector whose only transport refuses every request): request
fingerprint, request identity, content-addressed body re-hashed, HTTP 200, header allowlist,
decoder binding, a strict **re-decode** and a field-for-field reproduction. A missing checkpoint
fails; a committed decoder rejection is **never** a Raw revision (ADR-0029).

Idempotency and recovery (no journal, no sidecar): the same bytes for the same request are one
revision whoever delivers them; a second delivery (a replay or another ``request_id``) adopts the
committed row, its number, times and first-delivery provenance, and commits nothing. A crash after
the commit is repaired by calling ``ingest_snapshot`` again.

Every committed row the store adopts is proven first (``ExchangeInfoRowVerifier``, which the
listing derivation uses too): it is rebuilt column for column from **its own first delivery's**
verified checkpoint, it holds its revision id and ``arrival_seq`` alone, and its one-row batch
snapshot carries exactly its fingerprint. ``verify_table`` additionally proves the table's history
is nothing but such one-row appends (no delete, no foreign batch, no row without its batch).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Final, Self, cast

import httpx
from pyiceberg.expressions import BooleanExpression, EqualTo

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    SnapshotInfo,
    TableNotFound,
)
from core.contracts.collector import UnsupportedRequest
from core.contracts.revision import RevisionRecord
from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure import contract_version
from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_EXCHANGE_INFO
from infrastructure.collector.binance_exchange_info import (
    EXCHANGE_INFO_COLLECTOR_ID,
    EXCHANGE_INFO_COLLECTOR_VERSION,
    BinanceSpotExchangeInfoCollector,
    ExchangeInfoCheckpointInvalid,
    ExchangeInfoRequest,
    ExchangeInfoSnapshot,
    ExchangeInfoSnapshotRejected,
)
from infrastructure.parser.binance_exchange_info import EXCHANGE_INFO_DECODER_BINDING
from infrastructure.revision import exchange_info_identity as identity
from infrastructure.revision.exchange_info_availability import (
    ExchangeInfoAvailabilitySubject,
    ExchangeInfoAvailabilityViolation,
    decide_exchange_info_availability,
)
from infrastructure.revision.row_integrity import (
    batch,
    batch_rows,
    check_batch_snapshot,
    history_from,
)
from infrastructure.revision.store import BatchCommit, RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = [
    "EXCHANGE_INFO_TABLE",
    "ProvenSnapshotTable",
    "ExchangeInfoRowVerifier",
    "ExchangeInfoSnapshotStore",
    "ExchangeInfoStoreConflict",
    "ExchangeInfoStoreError",
    "ExchangeInfoStored",
    "snapshot_batch_id",
    "snapshot_columns",
    "snapshot_reader",
]

EXCHANGE_INFO_TABLE: Final = BINANCE_SPOT_EXCHANGE_INFO.table
_ATTEMPTS: Final = 8
_ZERO: Final = timedelta(0)
_MICROSECOND: Final = timedelta(microseconds=1)
_BATCH_RE: Final = re.compile(r"(rev1-[0-9a-f]{64})\.snapshot\.(0|[1-9][0-9]{0,18})")


class ExchangeInfoStoreError(Exception):
    """Base class of snapshot store failures that must not be papered over."""


class ExchangeInfoStoreConflict(ExchangeInfoStoreError):
    """The request cannot be honoured now (clock, contention); nothing more is written."""


def _equals(column: str, value: object) -> BooleanExpression:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _refuse_network(request: httpx.Request) -> httpx.Response:
    raise ExchangeInfoStoreError("a snapshot checkpoint reader never touches the network")


def snapshot_reader(storage: StorageAdapter, origin: str) -> BinanceSpotExchangeInfoCollector:
    """The collector's strict checkpoint reader; its only transport refuses every request."""
    return BinanceSpotExchangeInfoCollector(
        storage,
        market_data_base_url=origin,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=0,
        http_user_agent="hlens-e2-checkpoint-reader/1.0.0",
        min_request_interval_ms=50,
        max_retry_after_seconds=1,
        max_response_bytes=65_536,
        http_transport=httpx.MockTransport(_refuse_network),
    )


def snapshot_batch_id(revision_id: str, arrival_seq: int) -> str:
    """Stable id of the one-row snapshot batch; the number keeps a lost race distinct."""
    return f"{revision_id}.snapshot.{arrival_seq}"


# =========================================================================================
# the single row builder
# =========================================================================================


def snapshot_columns(
    snapshot: ExchangeInfoSnapshot,
    *,
    arrival_seq: int,
    knowledge_time: datetime,
    contract_schema_version: str | None = None,
) -> dict[str, Any]:
    """Every column of the snapshot revision of ``snapshot`` (the single builder).

    ``contract_schema_version``: the version a committed revision records when it is rebuilt,
    ``None`` for a new revision (the current version; ADR-0052 versioned replay, V1 / V2).

    Raises ``ExchangeInfoIdentityViolation`` / ``ExchangeInfoAvailabilityViolation`` /
    ``ValueError`` when the inputs cannot form a lawful revision.
    """
    observation_key = identity.snapshot_observation_key(snapshot.request_identity)
    source = identity.exchange_info_source_identity()
    payload = snapshot.body.sha256
    decision = decide_exchange_info_availability(
        ExchangeInfoAvailabilitySubject.SNAPSHOT,
        requested_at=snapshot.requested_at,
        ingest_time=snapshot.retrieved_at,
        knowledge_time=knowledge_time,
    )
    record = RevisionRecord(
        schema_version=(
            contract_version.new_group_version()
            if contract_schema_version is None
            else contract_version.replay_version(
                contract_schema_version, what="a rebuilt snapshot revision"
            )
        ),
        observation_key=observation_key,
        revision_id=identity.revision_id(observation_key, source, payload),
        source_id=source,
        payload_hash=payload,
        arrival_seq=identity.check_arrival_seq(arrival_seq),
        availability=decision,
    )
    times = decision.times
    decoded = snapshot.decoded
    request = snapshot.request
    row: dict[str, Any] = {
        "observation_key": record.observation_key,
        "revision_id": record.revision_id,
        "source_id": record.source_id,
        "payload_hash": record.payload_hash,
        "arrival_seq": record.arrival_seq,
        "supersedes": [],
        "source_revision_id": None,
        "source_revision_time": None,
        "event_time": times.event_time,
        "event_end_time": times.event_end_time,
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
        # Snapshots of one request are never ordered against each other: no edge, ever.
        "precedence_evidence": [],
        "contract_schema_version": record.schema_version,
        "source_binding_id": request.source.source_id,
        "source_binding_version": request.source.version,
        "collector_id": EXCHANGE_INFO_COLLECTOR_ID,
        "collector_version": EXCHANGE_INFO_COLLECTOR_VERSION,
        "collection_request_id": request.request_id,
        "request_origin": snapshot.origin,
        "request_path": snapshot.query.path,
        "request_query": snapshot.query.query_string(),
        "request_identity_sha256": snapshot.request_identity,
        "source_uri": snapshot.source_uri,
        "requested_at": snapshot.requested_at,
        "retrieved_at": snapshot.retrieved_at,
        "http_status": snapshot.http_status,
        "source_metadata": [
            {"name": name, "value": value} for name, value in snapshot.http_metadata
        ],
        "object_key": snapshot.body.key,
        "object_uri": snapshot.body.uri,
        "object_sha256": snapshot.body.sha256,
        "object_size_bytes": snapshot.body.size,
        "decoder_id": EXCHANGE_INFO_DECODER_BINDING.policy_id,
        "decoder_version": EXCHANGE_INFO_DECODER_BINDING.version,
        "decoder_hash": EXCHANGE_INFO_DECODER_BINDING.policy_hash,
        "server_time_raw": decoded.server_time_raw,
        "requested_symbols": list(snapshot.query.symbols),
        "symbols": [
            {
                "symbol": item.symbol,
                "status": item.status,
                "base_asset": item.base_asset,
                "quote_asset": item.quote_asset,
            }
            for item in decoded.symbols
        ],
    }
    return dict(batch_rows(batch(BINANCE_SPOT_EXCHANGE_INFO, [row]))[0])


def _row_key(row: Mapping[str, Any]) -> str:
    """A content key of a committed row (proof cache): every column, canonically encoded."""
    return canonical_json(
        {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in sorted(row.items())
        }
    )


# =========================================================================================
# the persisted-row verifier
# =========================================================================================


@dataclass(frozen=True, slots=True)
class ProvenSnapshotTable:
    """Every proven row of the snapshot table at ``head`` and the snapshot that committed it."""

    head: str | None
    rows: tuple[Mapping[str, Any], ...]
    #: revision id → the snapshot id of its one-row batch.
    snapshot_of: Mapping[str, str]
    #: ``head`` and its ancestors, newest first.
    history: tuple[str, ...]

    def rows_at(self, snapshot_id: str) -> tuple[Mapping[str, Any], ...]:
        """The rows committed at or before ``snapshot_id`` (an ancestor of ``head``)."""
        if snapshot_id not in self.history:
            raise CatalogIntegrityError(
                f"{EXCHANGE_INFO_TABLE} snapshot {snapshot_id} is not in the proven history"
            )
        older = set(self.history[self.history.index(snapshot_id) :])
        return tuple(row for row in self.rows if self.snapshot_of[row["revision_id"]] in older)


class ExchangeInfoRowVerifier:
    """Proves committed ``raw.binance_spot_exchange_info`` rows (store and listing derivation).

    ``catalog`` may be the real adapter or a ``PinnedCatalogView``; every read names the head it
    judges (``head``), so a verdict is always about one fixed snapshot. Proven rows are cached by
    their full content: a row that proved once proves again.
    """

    def __init__(self, catalog: RevisionCatalog, storage: StorageAdapter, origin: str) -> None:
        self._catalog = catalog
        self._storage = storage
        self._reader = snapshot_reader(storage, origin)
        self._proven: dict[str, Mapping[str, Any]] = {}

    @property
    def origin(self) -> str:
        return self._reader.origin

    def close(self) -> None:
        self._reader.close()

    def load_snapshot(self, request: ExchangeInfoRequest) -> ExchangeInfoSnapshot | None:
        """The verified committed snapshot of ``request`` (never fetched)."""
        return self._reader.replay(request)

    # ------------------------------------------------------------------ one row

    def expected_row(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        """The row rebuilt from its own first delivery's verified checkpoint, at the contract
        version the row was committed with (ADR-0052 versioned replay, V1)."""
        revision = row.get("revision_id")
        version = contract_version.replay_version(
            row.get("contract_schema_version"), what=f"snapshot revision {revision}"
        )
        request = ExchangeInfoRequest(request_id=row.get("collection_request_id"))  # type: ignore[arg-type]
        try:
            snapshot = self._reader.replay(request)
        except ExchangeInfoSnapshotRejected as exc:
            raise CatalogIntegrityError(
                f"snapshot revision {revision} names a rejected attempt: {exc}"
            ) from None
        except (ExchangeInfoCheckpointInvalid, ValueError) as exc:
            raise CatalogIntegrityError(
                f"snapshot revision {revision} names a checkpoint that does not reproduce: {exc}"
            ) from None
        except UnsupportedRequest as exc:
            raise CatalogIntegrityError(
                f"snapshot revision {revision} names no lawful request: {exc}"
            ) from None
        if snapshot is None:
            raise CatalogIntegrityError(
                f"snapshot revision {revision} has no committed checkpoint behind it"
            )
        try:
            return snapshot_columns(
                snapshot,
                arrival_seq=row["arrival_seq"],
                knowledge_time=row["knowledge_time"],
                contract_schema_version=version,
            )
        except (identity.ExchangeInfoIdentityViolation, ExchangeInfoAvailabilityViolation) as exc:
            raise CatalogIntegrityError(
                f"snapshot revision {revision} is not lawful: {exc}"
            ) from None
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogIntegrityError(
                f"snapshot revision {revision} is not lawful: {exc}"
            ) from None

    def verify_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        head: str | None,
        *,
        whole_table: bool = False,
    ) -> list[Mapping[str, Any]]:
        """Each row rebuilt from its checkpoint, unique by id and number, in its own batch.

        Only the checkpoint rebuild is cached (by the row's full content); uniqueness and the
        batch snapshot are judged again at every ``head``. With ``whole_table`` the rows are the
        table's complete content at ``head`` and uniqueness is checked among them directly.
        """
        definition = BINANCE_SPOT_EXCHANGE_INFO
        checked: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for row in rows:
            key = _row_key(row)
            expected = self._proven.get(key)
            if expected is None:
                expected = self.expected_row(row)
                mismatched = sorted(
                    name for name, value in expected.items() if row.get(name) != value
                )
                if mismatched or set(row) != set(expected):
                    raise CatalogIntegrityError(
                        f"snapshot revision {row.get('revision_id')} disagrees with its verified "
                        f"checkpoint: {mismatched}"
                    )
                self._proven[key] = expected
            checked.append((row, expected))
        if not checked:
            return []
        if whole_table:
            for column in ("revision_id", "arrival_seq"):
                values = [row[column] for row, _ in checked]
                if len(set(values)) != len(values):
                    raise CatalogIntegrityError(
                        f"{EXCHANGE_INFO_TABLE}: a {column} is held by 2 rows or more"
                    )
        found: dict[str, list[SnapshotInfo]] = {
            snapshot_batch_id(row["revision_id"], row["arrival_seq"]): [] for row, _ in checked
        }
        for snapshot in _history(self._catalog, EXCHANGE_INFO_TABLE, head):
            if snapshot.batch_id in found:
                found[snapshot.batch_id].append(snapshot)
        for row, expected in checked:
            revision = row["revision_id"]
            if not whole_table:
                for column, value in (
                    ("revision_id", revision),
                    ("arrival_seq", row["arrival_seq"]),
                ):
                    holders = self._catalog.scan_columns(
                        EXCHANGE_INFO_TABLE,
                        columns=("revision_id",),
                        row_filter=_equals(column, value),
                        limit=2,
                        snapshot_id=head,
                    ).num_rows
                    if holders != 1:
                        raise CatalogIntegrityError(
                            f"snapshot revision {revision}: {column} is held by {holders} rows"
                        )
            batch_id = snapshot_batch_id(revision, row["arrival_seq"])
            snapshots = found[batch_id]
            if len(snapshots) != 1:
                raise CatalogIntegrityError(
                    f"{EXCHANGE_INFO_TABLE} has rows of batch {batch_id} but {len(snapshots)} "
                    "snapshots committing it"
                )
            check_batch_snapshot(definition, batch_id, snapshots[0], [expected])
        return list(rows)

    # ------------------------------------------------------------------ the whole table

    def verify_table(self, head: str | None) -> ProvenSnapshotTable:
        """Every committed row at ``head``, proven; the history holds only one-row appends."""
        if head is None:
            return ProvenSnapshotTable(head=None, rows=(), snapshot_of={}, history=())
        rows = self._scan(head)
        history = _history(self._catalog, EXCHANGE_INFO_TABLE, head)
        batches: dict[tuple[str, int], str] = {}
        for snapshot in history:
            match = None if snapshot.batch_id is None else _BATCH_RE.fullmatch(snapshot.batch_id)
            if match is None or snapshot.added_rows != 1:
                raise CatalogIntegrityError(
                    f"{EXCHANGE_INFO_TABLE} snapshot {snapshot.snapshot_id} is not a one-row "
                    "snapshot append"
                )
            entry = (match.group(1), int(match.group(2)))
            if entry in batches:
                raise CatalogIntegrityError(f"{EXCHANGE_INFO_TABLE} commits batch {entry} twice")
            batches[entry] = snapshot.snapshot_id
        if history[0].total_rows != len(history) or len(rows) != len(history):
            raise CatalogIntegrityError(
                f"{EXCHANGE_INFO_TABLE} holds {len(rows)} rows after {len(history)} one-row "
                "appends: rows were removed or added outside a snapshot batch"
            )
        if {(row["revision_id"], row["arrival_seq"]) for row in rows} != set(batches):
            raise CatalogIntegrityError(
                f"{EXCHANGE_INFO_TABLE}: the committed rows are not exactly its batches' rows"
            )
        self.verify_rows(rows, head, whole_table=True)
        return ProvenSnapshotTable(
            head=head,
            rows=tuple(rows),
            snapshot_of={
                row["revision_id"]: batches[(row["revision_id"], row["arrival_seq"])]
                for row in rows
            },
            history=tuple(snapshot.snapshot_id for snapshot in history),
        )

    def verify_table_bounded(
        self,
        head: str | None,
        *,
        bounded_metadata: BoundedIcebergMetadata | None = None,
        scratch_storage: StorageAdapter,
        capacity: int,
        merge_fanout: int,
        limits: RunLimits,
        max_record_bytes: int,
    ) -> RunRef | None:
        """Prove the Raw table at ``head`` without retaining rows or a proof cache.

        The returned RunRef is sorted by ``(revision_id, arrival_seq)`` and contains exact rows in
        ``{"row": ..., "sort_key": [...], "snapshot_id": ..., "snapshot_ordinal": ...}``
        envelopes. The snapshot identity and ordinal are derived from the actual verified catalog
        history row paired with its data row, never inferred from arrival order alone. Consume it
        using ``iter_run`` and close that context. Scratch objects may orphan on failure; callers
        must provide a namespace distinct from report/evidence storage. The cap is explicit and
        checked before spooling.

        This reduces Python row/result retention. Catalog snapshot metadata and an individual
        backend record batch remain implementation-dependent, so this is not an E1-CAP-1 claim.
        """
        if bounded_metadata is not None:
            if bounded_metadata.name != EXCHANGE_INFO_TABLE:
                raise CatalogIntegrityError("bounded Raw metadata is pinned to another table")
            pinned_head_id = bounded_metadata.selected_snapshot_id
            if head != pinned_head_id:
                raise CatalogIntegrityError("Raw head differs from its selected bounded snapshot")
        if head is None:
            return None
        if not isinstance(limits, RunLimits):
            raise ValueError("limits must be RunLimits")
        for name, value, minimum in (
            ("capacity", capacity, 1),
            ("merge_fanout", merge_fanout, 2),
            ("max_record_bytes", max_record_bytes, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if scratch_storage is self._storage:
            raise ValueError("scratch_storage must be a distinct adapter and storage namespace")
        for attribute in ("warehouse_uri", "staging_uri"):
            source_uri = getattr(self._storage, attribute, None)
            scratch_uri = getattr(scratch_storage, attribute, None)
            if isinstance(source_uri, str) and source_uri == scratch_uri:
                raise ValueError(
                    f"scratch_storage {attribute} must differ from evidence storage; "
                    "namespace isolation remains a caller precondition"
                )

        def pair_key(row: Mapping[str, Any]) -> tuple[str, int]:
            return (row["sort_key"][0], row["sort_key"][1])

        def make_run(key: Callable[[Mapping[str, Any]], Any]) -> RunSetBuilder:
            return RunSetBuilder(
                scratch_storage,
                key=key,
                capacity=capacity,
                merge_fanout=merge_fanout,
                limits=limits,
            )

        row_builder = make_run(pair_key)
        history_builder = make_run(pair_key)
        proved_builder = make_run(pair_key)
        revision_builder = make_run(lambda row: row["revision_id"])
        arrival_builder = make_run(lambda row: row["arrival_seq"])
        with row_builder, history_builder, proved_builder, revision_builder, arrival_builder:
            snapshot_info = getattr(self._catalog, "_snapshot_info", None)
            if bounded_metadata is not None:
                if not callable(snapshot_info):
                    raise CatalogIntegrityError(
                        "bounded Raw verifier requires the adapter SnapshotInfo converter"
                    )
                head_snapshot = bounded_metadata.require_snapshot(head)
                head_info = snapshot_info(EXCHANGE_INFO_TABLE, head_snapshot)
                history_snapshots: Iterator[Any] = iter(bounded_metadata.iter_history(head))
            else:
                head_info = self._catalog.get_snapshot(EXCHANGE_INFO_TABLE, head)
                history_snapshots = iter(history_from(self._catalog, EXCHANGE_INFO_TABLE, head))
            if head_info.snapshot_id != head:
                raise CatalogIntegrityError("Raw snapshot lookup returned a different head")

            def proven_history() -> Iterator[SnapshotInfo]:
                try:
                    for snapshot in history_snapshots:
                        if bounded_metadata is not None:
                            assert callable(snapshot_info)
                            yield snapshot_info(EXCHANGE_INFO_TABLE, snapshot)
                            continue
                        try:
                            actual_snapshot = self._catalog.get_snapshot(
                                EXCHANGE_INFO_TABLE, snapshot.snapshot_id
                            )
                        except Exception as exc:
                            raise CatalogIntegrityError(
                                f"{EXCHANGE_INFO_TABLE} history names an unreadable snapshot "
                                f"{snapshot.snapshot_id}"
                            ) from exc
                        if actual_snapshot != snapshot:
                            raise CatalogIntegrityError(
                                f"{EXCHANGE_INFO_TABLE} history metadata disagrees with snapshot "
                                f"{snapshot.snapshot_id}"
                            )
                        yield snapshot
                finally:
                    close_history = getattr(history_snapshots, "close", None)
                    if callable(close_history):
                        close_history()

            history_count = 0
            for snapshot_info_value in proven_history():
                expected_total_rows = head_info.total_rows - history_count
                if snapshot_info_value.total_rows != expected_total_rows:
                    raise CatalogIntegrityError(
                        f"{EXCHANGE_INFO_TABLE} snapshot history has inconsistent total_rows"
                    )
                match = (
                    None
                    if snapshot_info_value.batch_id is None
                    else _BATCH_RE.fullmatch(snapshot_info_value.batch_id)
                )
                if match is None or snapshot_info_value.added_rows != 1:
                    raise CatalogIntegrityError(
                        f"{EXCHANGE_INFO_TABLE} snapshot "
                        f"{snapshot_info_value.snapshot_id} is not a "
                        "one-row snapshot append"
                    )
                if int(match.group(2)) != snapshot_info_value.total_rows - 1:
                    raise CatalogIntegrityError(
                        f"{EXCHANGE_INFO_TABLE} snapshot "
                        f"{snapshot_info_value.snapshot_id} has a batch "
                        "arrival sequence inconsistent with its append ordinal"
                    )
                history_row = {
                    "sort_key": [match.group(1), int(match.group(2))],
                    "snapshot": snapshot_info_value.model_dump(mode="python"),
                }
                self._check_bounded_row(history_row, max_record_bytes)
                history_builder.add(history_row)
                history_count += 1
            if history_count != head_info.total_rows:
                raise CatalogIntegrityError(
                    f"{EXCHANGE_INFO_TABLE} head has {head_info.total_rows} rows but its "
                    f"one-row history contains {history_count} snapshots"
                )
            history_root = history_builder.finish()
            if history_root is None:
                raise CatalogIntegrityError("a non-empty Raw snapshot head has empty history")

            columns = tuple(field.name for field in BINANCE_SPOT_EXCHANGE_INFO.arrow_schema)
            if bounded_metadata is None:
                reader = self._catalog.scan_column_batches(
                    EXCHANGE_INFO_TABLE, columns=columns, snapshot_id=head
                )
            else:
                scan_pinned = getattr(self._catalog, "scan_pinned_batches", None)
                if not callable(scan_pinned):
                    raise CatalogIntegrityError(
                        "bounded Raw verifier requires pinned batch scan support"
                    )
                reader = scan_pinned(
                    bounded_metadata,
                    snapshot_id=head,
                    columns=columns,
                )
            row_count = 0
            try:
                for record_batch in reader:
                    for index in range(record_batch.num_rows):
                        [row] = record_batch.slice(index, 1).to_pylist()
                        self._check_bounded_row(row, max_record_bytes)
                        expected = self.expected_row(row)
                        if set(row) != set(expected) or any(
                            row.get(name) != value for name, value in expected.items()
                        ):
                            raise CatalogIntegrityError(
                                f"snapshot revision {row.get('revision_id')} disagrees with its "
                                "verified checkpoint"
                            )
                        revision_id = row.get("revision_id")
                        arrival_seq = row.get("arrival_seq")
                        if not isinstance(revision_id, str) or not revision_id:
                            raise CatalogIntegrityError("a Raw snapshot row has no revision_id")
                        if (
                            isinstance(arrival_seq, bool)
                            or not isinstance(arrival_seq, int)
                            or arrival_seq < 0
                        ):
                            raise CatalogIntegrityError(
                                "a Raw snapshot row has invalid arrival_seq"
                            )
                        sort_key = [revision_id, arrival_seq]
                        row_builder.add({"row": row, "sort_key": sort_key})
                        revision_builder.add({"revision_id": revision_id})
                        arrival_builder.add({"arrival_seq": arrival_seq})
                        row_count += 1
            finally:
                close = getattr(reader, "close", None)
                if callable(close):
                    close()
            if row_count != history_count:
                raise CatalogIntegrityError(
                    f"{EXCHANGE_INFO_TABLE} holds {row_count} rows after {history_count} "
                    "one-row appends"
                )
            row_root = row_builder.finish()
            revision_root = revision_builder.finish()
            arrival_root = arrival_builder.finish()
            if row_root is None or revision_root is None or arrival_root is None:
                raise CatalogIntegrityError("a non-empty Raw table produced an empty run")

            with (
                iter_run(scratch_storage, history_root) as expected_rows,
                iter_run(scratch_storage, row_root) as found_rows,
            ):
                sentinel = object()
                expected_item = next(expected_rows, sentinel)
                found_item = next(found_rows, sentinel)
                while expected_item is not sentinel and found_item is not sentinel:
                    expected = cast(Mapping[str, Any], expected_item)
                    found = cast(Mapping[str, Any], found_item)
                    if expected["sort_key"] != found["sort_key"]:
                        raise CatalogIntegrityError(
                            f"{EXCHANGE_INFO_TABLE}: rows do not match its one-row batch history"
                        )
                    snapshot = SnapshotInfo.model_validate(expected["snapshot"])
                    batch_id = snapshot_batch_id(
                        found["row"]["revision_id"], found["row"]["arrival_seq"]
                    )
                    check_batch_snapshot(
                        BINANCE_SPOT_EXCHANGE_INFO, batch_id, snapshot, [found["row"]]
                    )
                    proved = {
                        "row": found["row"],
                        "sort_key": found["sort_key"],
                        "snapshot_id": snapshot.snapshot_id,
                        "snapshot_ordinal": snapshot.total_rows - 1,
                    }
                    self._check_bounded_row(proved, max_record_bytes)
                    proved_builder.add(proved)
                    expected_item = next(expected_rows, sentinel)
                    found_item = next(found_rows, sentinel)
                if expected_item is not sentinel or found_item is not sentinel:
                    raise CatalogIntegrityError(
                        f"{EXCHANGE_INFO_TABLE}: rows do not match its one-row batch history"
                    )
            for root, field in ((revision_root, "revision_id"), (arrival_root, "arrival_seq")):
                with iter_run(scratch_storage, root) as values:
                    previous: Any = object()
                    for item in values:
                        current = item[field]
                        if current == previous:
                            raise CatalogIntegrityError(
                                f"{EXCHANGE_INFO_TABLE}: {field} {current} occurs more than once"
                            )
                        previous = current
            proved_root = proved_builder.finish()
            if proved_root is None:
                raise CatalogIntegrityError("a non-empty Raw table produced an empty proved run")
            return proved_root

    @staticmethod
    def _check_bounded_row(row: Mapping[str, Any], maximum: int) -> None:
        """Count canonical JSONL bytes exactly without encoding any whole string or row."""
        size = 0

        def add(amount: int) -> None:
            nonlocal size
            size += amount
            if size > maximum:
                raise CatalogIntegrityError(f"snapshot row exceeds max_record_bytes={maximum}")

        def text_body_size(value: str) -> None:
            for offset in range(0, len(value), 128):
                try:
                    escaped = json.dumps(value[offset : offset + 128], ensure_ascii=False)[1:-1]
                    add(len(escaped.encode("utf-8")))
                except UnicodeEncodeError as exc:
                    raise CatalogIntegrityError("snapshot row contains invalid UTF-8 text") from exc

        def visit(value: Any) -> None:
            if isinstance(value, str):
                add(2)
                text_body_size(value)
            elif isinstance(value, Mapping):
                if len(value) > maximum // 3:
                    raise CatalogIntegrityError(f"snapshot row exceeds max_record_bytes={maximum}")
                keys = list(value)
                if any(not isinstance(key, str) for key in keys):
                    raise CatalogIntegrityError("snapshot row has a non-text key")
                keys.sort()
                add(1)
                for index, key in enumerate(keys):
                    if index:
                        add(1)
                    add(2)
                    text_body_size(key)
                    add(1)
                    try:
                        item = value[key]
                    except Exception as exc:
                        raise CatalogIntegrityError(
                            "snapshot row mapping changed during sizing"
                        ) from exc
                    visit(item)
                add(1)
            elif isinstance(value, list | tuple):
                add(2)
                for index, item in enumerate(value):
                    if index:
                        add(1)
                    visit(item)
            elif value is None:
                add(4)
            elif isinstance(value, bool):
                add(4 if value else 5)
            elif isinstance(value, int):
                add(len(str(value)))
            elif isinstance(value, float):
                try:
                    add(len(json.dumps(value, allow_nan=False).encode("ascii")))
                except (TypeError, ValueError) as exc:
                    raise CatalogIntegrityError(
                        "snapshot row contains an invalid JSON number"
                    ) from exc
            elif isinstance(value, datetime):
                add(2)
                text_body_size(value.isoformat())
            else:
                raise CatalogIntegrityError(
                    f"snapshot row contains unsupported value {type(value).__name__}"
                )

        visit(row)
        add(1)  # JSONL LF

    def _scan(self, head: str | None) -> list[Mapping[str, Any]]:
        columns = tuple(field.name for field in BINANCE_SPOT_EXCHANGE_INFO.arrow_schema)
        rows: list[Mapping[str, Any]] = self._catalog.scan_columns(
            EXCHANGE_INFO_TABLE, columns=columns, snapshot_id=head
        ).to_pylist()
        return rows


def _history(catalog: RevisionCatalog, table: str, head: str | None) -> list[SnapshotInfo]:
    """``head`` and its ancestors, newest first; reject a cyclic parent chain."""
    found: list[SnapshotInfo] = []
    snapshot = None if head is None else catalog.get_snapshot(table, head)
    while snapshot is not None:
        if any(item.snapshot_id == snapshot.snapshot_id for item in found):
            raise CatalogIntegrityError(f"table {table} has a cycle in snapshot history")
        found.append(snapshot)
        parent = snapshot.parent_snapshot_id
        snapshot = None if parent is None else catalog.get_snapshot(table, parent)
    return found


# =========================================================================================
# the store
# =========================================================================================


@dataclass(frozen=True, slots=True)
class ExchangeInfoStored:
    """Result of persisting one committed snapshot attempt."""

    request_id: str
    observation_key: str
    revision_id: str
    arrival_seq: int
    ingest_time: datetime
    knowledge_time: datetime
    #: True when the committed row records *this* request as its first delivery.
    first_delivery: bool
    #: Requested symbols the snapshot did not contain (a fact; never inferred here).
    missing_symbols: tuple[str, ...]
    commit: BatchCommit

    @property
    def replayed(self) -> bool:
        return self.commit.replayed


class ExchangeInfoSnapshotStore:
    """Persists committed exchangeInfo snapshot attempts as Raw source revisions.

    One writer per table (ADR-0023 §7); concurrent writers are tolerated through expected-parent
    commits and bounded retries. No state between calls.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            self._verifier = ExchangeInfoRowVerifier(adapter, storage, market_data_base_url)
        except ValueError as exc:
            raise ExchangeInfoStoreError(f"invalid market-data origin: {exc}") from None

    def close(self) -> None:
        self._verifier.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def ingest_snapshot(self, request: ExchangeInfoRequest) -> ExchangeInfoStored:
        """Persist the committed attempt ``request`` as one snapshot revision (or adopt it)."""
        snapshot = self._load(request)
        observation_key = identity.snapshot_observation_key(snapshot.request_identity)
        revision = identity.revision_id(
            observation_key, identity.exchange_info_source_identity(), snapshot.body.sha256
        )
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            parent = self._head()
            ours = self._rows(_equals("revision_id", revision), parent)
            if len(ours) > 1:
                raise CatalogIntegrityError(f"snapshot revision {revision} is committed twice")
            if ours:
                return self._adopt(request, snapshot, ours[0], parent)
            largest = self._largest_arrival(parent)
            try:
                seq = identity.next_arrival_seq(largest)
            except identity.ExchangeInfoIdentityViolation as exc:
                raise CatalogIntegrityError(f"corrupt exchangeInfo arrival anchor: {exc}") from None
            knowledge_time = self._knowledge_time(snapshot.retrieved_at)
            row = snapshot_columns(snapshot, arrival_seq=seq, knowledge_time=knowledge_time)
            table = batch(BINANCE_SPOT_EXCHANGE_INFO, [row])
            commit = CommitRequest(
                table=EXCHANGE_INFO_TABLE,
                batch_id=snapshot_batch_id(revision, seq),
                batch_fingerprint=BINANCE_SPOT_EXCHANGE_INFO.fingerprint_rule.fingerprint(table),
                row_count=1,
                expected_parent_snapshot_id=parent,
            )
            try:
                result = self._adapter.commit_batch(commit, table)
            except (CommitConflict, BatchConflict) as exc:
                last_error = exc  # another writer moved the head: read everything again
                continue
            head = result.snapshot.snapshot_id
            stored = self._rows(_equals("revision_id", revision), head)
            if len(stored) != 1 or dict(stored[0]) != row:
                raise CatalogIntegrityError(
                    f"snapshot revision {revision} does not read back as committed"
                )
            self._verifier.verify_rows(stored, head)
            return ExchangeInfoStored(
                request_id=request.request_id,
                observation_key=observation_key,
                revision_id=revision,
                arrival_seq=seq,
                ingest_time=row["ingest_time"],
                knowledge_time=row["knowledge_time"],
                first_delivery=True,
                missing_symbols=snapshot.decoded.missing_symbols,
                commit=BatchCommit(
                    table=EXCHANGE_INFO_TABLE,
                    batch_id=commit.batch_id,
                    snapshot_id=head,
                    outcome=result.outcome,
                    row_count=1,
                ),
            )
        raise ExchangeInfoStoreConflict(
            f"snapshot revision {revision} lost {_ATTEMPTS} commit races"
        ) from last_error

    # ------------------------------------------------------------------ helpers

    def _load(self, request: ExchangeInfoRequest) -> ExchangeInfoSnapshot:
        try:
            snapshot = self._verifier.load_snapshot(request)
        except ExchangeInfoSnapshotRejected as exc:
            raise ExchangeInfoStoreError(
                f"request {request.request_id!r} was rejected by the decoder; a rejected "
                f"snapshot is never a Raw revision: {exc}"
            ) from None
        except ExchangeInfoCheckpointInvalid as exc:
            raise CatalogIntegrityError(str(exc)) from None
        except UnsupportedRequest as exc:
            raise ExchangeInfoStoreError(str(exc)) from None
        if snapshot is None:
            raise ExchangeInfoStoreError(
                f"request {request.request_id!r} has no committed snapshot checkpoint: the store "
                "only persists finished attempts and never collects"
            )
        return snapshot

    def _adopt(
        self,
        request: ExchangeInfoRequest,
        snapshot: ExchangeInfoSnapshot,
        stored: Mapping[str, Any],
        head: str | None,
    ) -> ExchangeInfoStored:
        """A committed revision of these bytes: prove it from its own delivery, then reuse it."""
        self._verifier.verify_rows([stored], head)
        batch_id = snapshot_batch_id(stored["revision_id"], stored["arrival_seq"])
        commit = next(
            item for item in _history(self._adapter, EXCHANGE_INFO_TABLE, head)
            if item.batch_id == batch_id
        )  # fmt: skip
        return ExchangeInfoStored(
            request_id=request.request_id,
            observation_key=stored["observation_key"],
            revision_id=stored["revision_id"],
            arrival_seq=stored["arrival_seq"],
            ingest_time=stored["ingest_time"],
            knowledge_time=stored["knowledge_time"],
            first_delivery=stored["collection_request_id"] == request.request_id,
            missing_symbols=snapshot.decoded.missing_symbols,
            commit=BatchCommit(
                table=EXCHANGE_INFO_TABLE,
                batch_id=batch_id,
                snapshot_id=commit.snapshot_id,
                outcome=CommitOutcome.ALREADY_COMMITTED,
                row_count=1,
            ),
        )

    def _rows(self, row_filter: BooleanExpression, head: str | None) -> list[Mapping[str, Any]]:
        if head is None:
            return []
        columns = tuple(field.name for field in BINANCE_SPOT_EXCHANGE_INFO.arrow_schema)
        rows: list[Mapping[str, Any]] = self._adapter.scan_columns(
            EXCHANGE_INFO_TABLE, columns=columns, row_filter=row_filter, snapshot_id=head
        ).to_pylist()
        return rows

    def _largest_arrival(self, head: str | None) -> int | None:
        if head is None:
            return None
        values = self._adapter.scan_columns(
            EXCHANGE_INFO_TABLE, columns=("arrival_seq",), snapshot_id=head
        ).column("arrival_seq")
        if values.null_count:
            raise CatalogIntegrityError(f"{EXCHANGE_INFO_TABLE}: an arrival_seq is null")
        numbers = values.to_pylist()
        return max(numbers) if numbers else None

    def _head(self) -> str | None:
        info = self._adapter.load_table(EXCHANGE_INFO_TABLE)
        if info is None:
            raise TableNotFound(
                f"table {EXCHANGE_INFO_TABLE} does not exist; create the Phase 1 tables first"
            )
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def _knowledge_time(self, ingest_time: datetime) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _ZERO:
            raise ExchangeInfoStoreError("the clock must return timezone-aware UTC")
        if now < ingest_time:
            raise ExchangeInfoStoreConflict(
                "knowledge_time would precede ingest_time: refusing to guess a local clock"
            )
        return now
