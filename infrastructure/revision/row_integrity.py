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

Bound to immutable sources (D3E-R3, after an independent review reproduced forged rows sitting
in brand-new batches with their own fingerprints):

- every REST response row is rebuilt from the **verified checkpoint page of its first delivery**
  (the committed D3D collection re-read and strictly re-decoded), provenance columns included;
- every REST element row must be the element the first delivery's body holds **at its index**;
- every archive row must be exactly the **strict D1 re-parse** of its archive object at its line,
  and no line beyond the object's rows exists.

Every lookup is bounded (``IN`` chunks, one lineage at a time). Callers read the rows and run
the verification between two identical sets of table heads; a view whose heads moved is never
judged (see ``RestRevisionStore`` / ``ChannelReconciler``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Container, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, runtime_checkable

import httpx
import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import (
    And,
    BooleanExpression,
    EqualTo,
    GreaterThanOrEqual,
    In,
    LessThan,
)

from core.contracts.catalog import BatchRejected, SnapshotInfo, TableNotFound
from core.contracts.collector import (
    CollectedObject,
    CollectionRequest,
    CollectionResult,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.revision import AvailabilityDecision, RevisionRecord
from core.contracts.storage import (
    IntegrityViolation,
    ObjectKeyViolation,
    StorageAdapter,
    StorageError,
)
from core.domain.base import FrozenMapping, canonical_json
from infrastructure import contract_version
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
from infrastructure.parser import ArchiveParseRequest
from infrastructure.parser.binance_archive import (
    AGG_TRADES_ROW_SCHEMA,
    KLINES_1M_ROW_SCHEMA,
    ArchiveRejection,
    SpooledArchive,
    TimeUnit,
    parse_archive_spooled,
    time_unit_for,
)
from infrastructure.parser.binance_rest import (
    DECODER_BINDING,
    RestPageDecoded,
    RestPageRejection,
    RestRejectionCode,
)
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
    "PageElement",
    "PersistedRowVerifier",
    "SnapshotHistory",
    "batch",
    "batch_rows",
    "check_batch_snapshot",
    "check_block_base",
    "check_provenance_shape",
    "history_from",
    "element_batch_id",
    "element_columns",
    "element_identity",
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
#: Verified first-delivery collections kept per verifier (checkpoints are immutable).
_COLLECTION_CACHE: Final = 16
#: History walks memoised per (table, head, prefixes): a head's history never changes.
_BATCH_INDEX_CACHE: Final = 8
#: Verified archives kept by a caching verifier (each holds its parsed object).
# One retained parsed archive limits cache cardinality. Its spool byte size still scales with one
# archive (and may be resident on tmpfs), so this is a structural bound, not an E1 capacity pass.
_ARCHIVE_CACHE: Final = 1
#: Widest ``arrival_seq`` range one holder scan covers (G3-S).
_HOLDER_SPAN: Final = 1 << 17


@contextmanager
def _scan_rows(
    catalog: Any,
    table: str,
    *,
    columns: Sequence[str],
    row_filter: BooleanExpression,
    snapshot_id: str | None = None,
) -> Iterator[Iterator[Mapping[str, Any]]]:
    """Yield row mappings from bounded catalog batches and always close the reader."""
    reader = catalog.scan_column_batches(
        table, columns=columns, row_filter=row_filter, snapshot_id=snapshot_id
    )

    def rows() -> Iterator[Mapping[str, Any]]:
        for record_batch in reader:
            yield from record_batch.to_pylist()

    try:
        yield rows()
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


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
    names = {field.name for field in definition.arrow_schema}
    for row in rows:
        if row.keys() != names:
            drift = sorted(names ^ set(row))
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
    """Main-branch snapshots committing each requested batch, newest first."""
    wanted: dict[str, list[SnapshotInfo]] = {batch_id: [] for batch_id in batch_ids}
    for snapshot in _history(adapter, table):
        if snapshot.batch_id in wanted:
            wanted[snapshot.batch_id].append(snapshot)
    return wanted


def _spooled_snapshots_of_batches(
    adapter: RevisionCatalog, table: str, batch_ids: Iterable[str]
) -> _BatchSnapshotLookup:
    """Bounded internal lookup for callers that need counts or streamed matches."""
    return _BatchSnapshotLookup(adapter, table, batch_ids)


class _BatchSnapshotLookup:
    """Disk-backed requested-ID index; retains only each ID's count and first snapshot."""

    __slots__ = ("_connection", "_path", "_closed")

    def __init__(self, adapter: RevisionCatalog, table: str, batch_ids: Iterable[str]) -> None:
        fd, self._path = tempfile.mkstemp(prefix="hlens-batch-snapshots-", suffix=".sqlite3")
        os.close(fd)
        self._closed = False
        try:
            self._connection = sqlite3.connect(self._path)
        except BaseException:
            self._closed = True
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass
            raise
        try:
            self._connection.execute("PRAGMA cache_size = -256")
            self._connection.execute("PRAGMA temp_store = FILE")
            self._connection.execute("PRAGMA mmap_size = 0")
            self._connection.execute("PRAGMA journal_mode = OFF")
            self._connection.execute("PRAGMA synchronous = OFF")
            self._connection.execute(
                "CREATE TABLE requested_batches ("
                "batch_id TEXT PRIMARY KEY, match_count INTEGER NOT NULL DEFAULT 0, "
                "first_snapshot TEXT) WITHOUT ROWID"
            )
            self._connection.executemany(
                "INSERT OR IGNORE INTO requested_batches (batch_id) VALUES (?)",
                ((batch_id,) for batch_id in batch_ids),
            )
            self._connection.commit()
            for snapshot in _history(adapter, table):
                if snapshot.batch_id is None:
                    continue
                cursor = self._connection.execute(
                    "UPDATE requested_batches SET match_count = match_count + 1 "
                    "WHERE batch_id = ?",
                    (snapshot.batch_id,),
                )
                if cursor.rowcount:
                    existing = self._connection.execute(
                        "SELECT first_snapshot FROM requested_batches WHERE batch_id = ?",
                        (snapshot.batch_id,),
                    ).fetchone()
                    if existing is not None and existing[0] is None:
                        payload = json.dumps(
                            snapshot.model_dump(mode="json"), separators=(",", ":")
                        )
                        self._connection.execute(
                            "UPDATE requested_batches SET first_snapshot = ? WHERE batch_id = ?",
                            (payload, snapshot.batch_id),
                        )
            self._connection.commit()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> _BatchSnapshotLookup:
        if self._closed:
            raise RuntimeError("snapshot lookup is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            try:
                os.unlink(self._path)
            except FileNotFoundError:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def one(self, batch_id: str) -> tuple[int, SnapshotInfo | None]:
        if self._closed:
            raise RuntimeError("snapshot lookup is closed")
        row = self._connection.execute(
            "SELECT match_count, first_snapshot FROM requested_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return 0, None
        count, payload = row
        first = None if payload is None else SnapshotInfo.model_validate(json.loads(payload))
        return count, first


def _history(adapter: RevisionCatalog, table: str) -> Iterable[SnapshotInfo]:
    info = adapter.load_table(table)
    if info is None:
        raise TableNotFound(f"table {table} does not exist")
    snapshot = info.current_snapshot
    if snapshot is None:
        return
    yield snapshot
    yield from history_from(adapter, table, snapshot.parent_snapshot_id)


@dataclass(slots=True)
class _BatchPrefixSummary:
    """Constant-size summary of one prefix in newest-first history order."""

    count: int = 0
    newest_index: int | None = None
    oldest_index: int | None = None
    previous_index: int | None = None
    added_rows: int = 0
    index_zero_rows: int | None = None
    newest_rows: int | None = None
    regular_rows: int | None = None


@dataclass(slots=True)
class _IndexedBatchHistory:
    """A pinned history head and per-prefix summaries; never retains SnapshotInfo objects."""

    head: str | None
    prefixes: tuple[str, ...]
    summaries: dict[str, _BatchPrefixSummary]


def _batch_prefix(batch_id: str, table: str, prefixes: Container[str]) -> tuple[str, int] | None:
    """Parse a batch id only when its parent lineage is one of the requested prefixes."""
    head, separator, tail = batch_id.rpartition(".")
    prefix = f"{head}{separator}"
    if prefix not in prefixes:
        return None
    if len(tail) != _BATCH_INDEX_DIGITS or not tail.isdigit() or not tail.isascii():
        raise CatalogIntegrityError(f"{table} has a malformed batch id {batch_id!r}")
    return prefix, int(tail)


def _indexed_batches(
    adapter: RevisionCatalog,
    table: str,
    prefixes: Iterable[str],
    snapshots: Iterable[SnapshotInfo] | None = None,
) -> dict[str, _BatchPrefixSummary]:
    """Summarise requested batch prefixes with constant space per prefix.

    ``snapshots`` is the history to walk (default: the table's current one).

    Histories are newest first. The writers append a lineage's microbatches in increasing index
    order, so its history indices are strictly decreasing. Checking that order detects duplicate
    submissions without keeping a set of every index. D2's stricter contiguous-prefix rule is
    checked by its consumer from the summary's count and endpoints; D3E may legitimately have
    gaps where another response first delivered an element.
    """
    found = {prefix: _BatchPrefixSummary() for prefix in prefixes}
    if not found:
        return found
    for snapshot in _history(adapter, table) if snapshots is None else snapshots:
        batch_id = snapshot.batch_id
        if batch_id is None:
            continue
        parsed = _batch_prefix(batch_id, table, found)
        if parsed is None:
            continue
        prefix, index = parsed
        summary = found[prefix]
        if summary.previous_index is not None and index >= summary.previous_index:
            raise CatalogIntegrityError(
                f"{table} has duplicate or out-of-order batch index {index} for {prefix!r}"
            )
        if summary.newest_index is None:
            summary.newest_index = index
            summary.newest_rows = snapshot.added_rows
        summary.oldest_index = index
        summary.previous_index = index
        summary.count += 1
        summary.added_rows += snapshot.added_rows
        if index == 0:
            summary.index_zero_rows = snapshot.added_rows
        # For D2, every batch before the final index has the index-zero row count. While
        # walking backwards, index n-2 supplies that count; retain only one value.
        if summary.newest_index != index and index != 0:
            if summary.regular_rows is None:
                summary.regular_rows = snapshot.added_rows
            elif summary.regular_rows != snapshot.added_rows:
                summary.regular_rows = -1
    return found


def _iter_indexed_batches(
    adapter: RevisionCatalog, table: str, history: _IndexedBatchHistory
) -> Iterator[tuple[str, int, SnapshotInfo]]:
    """Second pinned pass; yields matching SnapshotInfo one at a time, newest first."""
    if history.head is None:
        return
    prefixes = set(history.prefixes)
    for snapshot in history_from(adapter, table, history.head):
        batch_id = snapshot.batch_id
        if batch_id is None:
            continue
        parsed = _batch_prefix(batch_id, table, prefixes)
        if parsed is not None:
            yield parsed[0], parsed[1], snapshot


@runtime_checkable
class SnapshotHistory(Protocol):
    """Optional catalog capability: one snapshot's ancestry from one metadata load.

    ``PyIcebergCatalogAdapter`` (and a ``PinnedCatalogView`` over any catalog) provide it. It
    must yield exactly what the ``get_snapshot`` parent walk of ``history_from`` yields, newest
    first, with the same failures (a parent cycle, which that walk never leaves, may fail
    closed instead); it only avoids reloading the table metadata at every step.
    """

    def history(self, table: str, snapshot_id: str) -> Iterator[SnapshotInfo]: ...


def history_from(
    adapter: RevisionCatalog, table: str, snapshot_id: str | None
) -> Iterable[SnapshotInfo]:
    """``snapshot_id`` and its ancestors, newest first (a pinned head's history; none if None).

    A catalog with a ``SnapshotHistory.history`` walk is walked with it (one metadata load);
    any other ``RevisionCatalog`` keeps the ``get_snapshot`` parent walk, one snapshot per call.
    Its cycle detector uses Brent's algorithm with O(1) auxiliary memory.
    """
    if snapshot_id is None:
        return
    if isinstance(adapter, SnapshotHistory):
        yield from adapter.history(table, snapshot_id)
        return
    snapshot: SnapshotInfo | None = adapter.get_snapshot(table, snapshot_id)
    # Brent's cycle detector keeps one ancestry id and two counters instead of retaining
    # every visited id. ``power`` is the current checkpoint interval; ``distance`` counts
    # snapshots since that checkpoint. The parent walk remains a single catalog lookup per
    # link, in newest-first order.
    checkpoint_id: str | None = None
    power = 1
    distance = 0
    while snapshot is not None:
        if checkpoint_id is None:
            checkpoint_id = snapshot.snapshot_id
        else:
            distance += 1
        if snapshot.snapshot_id == checkpoint_id and distance > 0:
            raise CatalogIntegrityError(
                f"{table} has a cycle in snapshot ancestry at {snapshot.snapshot_id}"
            )
        yield snapshot
        if distance == power:
            checkpoint_id = snapshot.snapshot_id
            power *= 2
            distance = 0
        parent = snapshot.parent_snapshot_id
        snapshot = None if parent is None else adapter.get_snapshot(table, parent)


def check_batch_snapshot(
    definition: RegisteredTableDefinition,
    batch_id: str,
    snapshot: SnapshotInfo,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """The snapshot committing ``batch_id`` must carry exactly these rows' fingerprint."""
    table = batch(definition, rows)
    if snapshot.batch_fingerprint != definition.fingerprint_rule.fingerprint(
        table
    ) or snapshot.added_rows != len(rows):
        raise CatalogIntegrityError(
            f"batch {batch_id} of {definition.table} was committed with other content"
        )


def _one_snapshot(
    table: str, batch_id: str, found: tuple[int, SnapshotInfo | None]
) -> SnapshotInfo:
    count, snapshot = found
    if count != 1 or snapshot is None:
        raise CatalogIntegrityError(
            f"{table} has rows of batch {batch_id} but {count} snapshots committing it"
        )
    return snapshot


# =========================================================================================
# committed D3D collections (the one checkpoint path; D3E-R3)
# =========================================================================================


class CommittedCheckpointError(Exception):
    """A committed D3D collection cannot serve as provenance."""


class CheckpointMissing(CommittedCheckpointError):
    """No committed collection checkpoint (c3) for the request."""


class CheckpointConflict(CommittedCheckpointError):
    """The request id is committed with other request content."""


class CheckpointInvalid(CommittedCheckpointError):
    """The committed checkpoint or a body it references does not reproduce."""


@dataclass(frozen=True, slots=True)
class CommittedCollection:
    request: CollectionRequest
    outcome: str
    result: CollectionResult | None
    chains: tuple[tuple[str, tuple[Any, ...]], ...]


def _refuse_network(request: httpx.Request) -> httpx.Response:
    raise CheckpointInvalid("a checkpoint reader never touches the network")


def checkpoint_reader(storage: StorageAdapter, origin: str) -> Any:
    """The accepted D3D checkpoint reader; its only transport refuses every request."""
    return d3d.BinanceSpotRestCollector(
        storage,
        market_data_base_url=origin,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=0,
        http_user_agent="hlens-d3e-checkpoint-reader/1.0.0",
        max_pages_per_collect=d3d.MIN_PAGES_PER_COLLECT,
        min_request_interval_ms=d3d.MIN_REQUEST_INTERVAL_MS,
        max_retry_after_seconds=d3d.MIN_RETRY_AFTER_SECONDS,
        max_response_bytes=d3d.MAX_RESPONSE_BYTES,
        http_transport=httpx.MockTransport(_refuse_network),
    )


def _collection_key(request_id: str) -> str:
    root = f"{d3d.CHECKPOINT_PREFIX}/{d3d._sha256_hex(request_id.encode('utf-8'))}"
    return f"{root}/collection.json"


def committed_request(reader: Any, request_id: str) -> CollectionRequest:
    """The ``CollectionRequest`` a committed collection checkpoint is bound to."""
    try:
        payload = reader._read_object(_collection_key(request_id))
        if payload is None:
            raise CheckpointMissing(f"request {request_id!r} has no committed collection")
        document = d3d._parse_checkpoint(payload, d3d.COLLECTION_CHECKPOINT_KIND)
        fingerprint = d3d._mapping_field(document, "request")
        named = (fingerprint["source_id"], fingerprint["source_version"])
        # A published binding named by its id and version is the registered object itself,
        # whatever contract version is current (ADR-0052 versioned replay, V3); any other name
        # is built only to be refused by the reader.
        source = (
            d3d.REST_SOURCE
            if named == (d3d.REST_SOURCE.source_id, d3d.REST_SOURCE.version)
            else SourceBinding(source_id=named[0], version=named[1])
        )
        request = CollectionRequest(
            request_id=fingerprint["request_id"],
            source=source,
            data_type=fingerprint["data_type"],
            symbols=tuple(fingerprint["symbols"]),
            coverage_start=datetime.fromisoformat(fingerprint["coverage_start"]),
            coverage_end=datetime.fromisoformat(fingerprint["coverage_end"]),
        )
    except (d3d._CheckpointInvalid, KeyError, TypeError, ValueError) as exc:
        raise CheckpointInvalid(
            f"committed collection of {request_id!r} is invalid: {exc}"
        ) from None
    if request.request_id != request_id or d3d._request_fingerprint(request) != fingerprint:
        raise CheckpointInvalid(f"committed collection of {request_id!r} names another request")
    return request


def load_committed_collection(reader: Any, request: CollectionRequest) -> CommittedCollection:
    """The committed collection checkpoint of ``request``, re-verified field for field.

    Every page is re-read and strictly re-decoded by the accepted D3D reader; never fetched.
    ``StorageError`` other than an integrity violation propagates unchanged.
    """
    try:
        start_ms, end_ms = reader._validate_request(request)
    except UnsupportedRequest as exc:
        raise CheckpointInvalid(f"not a REST collection request: {exc}") from None
    fingerprint = d3d._request_fingerprint(request)
    key = _collection_key(request.request_id)
    try:
        payload = reader._read_object(key)
        if payload is None:
            raise CheckpointMissing(
                f"request {request.request_id!r} has no committed collection checkpoint "
                "(c3): the store only persists finished attempts and never collects"
            )
        document = d3d._parse_checkpoint(payload, d3d.COLLECTION_CHECKPOINT_KIND)
        if d3d._mapping_field(document, "request") != fingerprint:
            raise CheckpointConflict(
                f"request {request.request_id!r} is committed with different request content"
            )
        chains = reader._verify_chains(key, document, fingerprint, request, start_ms, end_ms)
        if not chains:
            raise d3d._CheckpointInvalid(f"{key!r} records no chain at all")
        outcome = d3d._str_field(document, "outcome")
        result: CollectionResult | None
        if outcome == "failed":
            failure = d3d._mapping_field(document, "failure")
            reader._check_failure_shape(failure, chains)
            result = None
            rebuilt = d3d._collection_document(
                fingerprint, chains, result=None, failure=dict(failure)
            )
        elif outcome == "succeeded":
            if [symbol for symbol, _ in chains] != list(request.symbols):
                raise d3d._CheckpointInvalid(f"{key!r} succeeds without every symbol")
            for symbol, records in chains:
                last = records[-1]
                if last.rejection is not None or last.summary.stop_reason is None:
                    raise d3d._CheckpointInvalid(f"{key!r} has an unterminated {symbol} chain")
            result = reader._assemble(request, start_ms, end_ms, chains)
            rebuilt = d3d._collection_document(fingerprint, chains, result=result, failure=None)
        else:
            raise d3d._CheckpointInvalid(f"{key!r} carries an unknown outcome")
        if canonical_json(rebuilt).encode("utf-8") != payload:
            raise d3d._CheckpointInvalid(f"{key!r} does not reproduce field for field")
    except d3d._CheckpointInvalid as exc:
        raise CheckpointInvalid(f"committed checkpoint does not reproduce: {exc}") from None
    except IntegrityViolation as exc:
        raise CheckpointInvalid(f"a committed object does not match its reference: {exc}") from None
    except StorageError:
        raise
    except ValueError as exc:
        raise CheckpointInvalid(f"committed checkpoint does not validate: {exc}") from None
    return CommittedCollection(
        request=request,
        outcome=outcome,
        result=result,
        chains=tuple((symbol, tuple(records)) for symbol, records in chains),
    )


def page_provenance(request: CollectionRequest, page: Any) -> dict[str, Any]:
    """The first-delivery provenance columns of a response row, from its verified page."""
    outcome = page.outcome
    if isinstance(outcome, RestPageRejection):
        decoded: dict[str, Any] = {
            "decode_outcome": REJECTED,
            "decode_rejection_code": outcome.code.value,
            "element_count": None,
            "answered_start": None,
            "answered_end": None,
        }
    elif isinstance(outcome, RestPageDecoded):
        summary = outcome.summary
        if summary.element_count != len(outcome.elements):
            raise CheckpointInvalid("page summary does not count its elements")
        answered = summary.answered
        decoded = {
            "decode_outcome": ACCEPTED,
            "decode_rejection_code": None,
            "element_count": summary.element_count,
            "answered_start": None if answered.is_empty else _at_ms(answered.start_ms),
            "answered_end": None if answered.is_empty else _at_ms(answered.end_ms),
        }
    else:  # pragma: no cover - the verified reader only yields these two outcomes
        raise CheckpointInvalid("unknown page decode outcome")
    return {
        "collector_id": d3d.REST_COLLECTOR_ID,
        "collector_version": d3d.REST_COLLECTOR_VERSION,
        "collection_request_id": request.request_id,
        "page_index": page.page_index,
        "requested_at": page.requested_at,
        "retrieved_at": page.retrieved_at,
        "source_metadata": [
            {"name": name, "value": page.http_metadata[name]} for name in sorted(page.http_metadata)
        ],
        **decoded,
    }


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
    contract_schema_version: str | None = None,
) -> dict[str, Any]:
    """Every column of one response revision from its inputs (the single builder, s1).

    ``contract_schema_version``: the version a committed revision records when it is rebuilt,
    ``None`` for a new revision (the current version; ADR-0052 versioned replay, V1 / V2).
    """
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
        schema_version=_group_version(contract_schema_version),
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


def _group_version(recorded: str | None) -> str:
    """A rebuilt revision's recorded version (checked), or a new revision's current one."""
    if recorded is None:
        return contract_version.new_group_version()
    return contract_version.replay_version(recorded, what="a rebuilt Raw revision")


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


def element_identity(
    data_type: str, symbol: str, native: Mapping[str, Any]
) -> tuple[str, str, str]:
    """``(key, payload hash, revision id)`` of one REST element from its normalised natives.

    The identity never depends on the response that delivered the element: that is what lets a
    later page find an element another page delivered first (``foreign``, never re-written).
    """
    if data_type == "agg_trades":
        key = rest_identity.agg_trade_observation_key(symbol, native["agg_trade_id"])
        payload = rest_identity.agg_trade_payload_hash(symbol, dict(native))
    else:
        key = rest_identity.kline_1m_observation_key(symbol, _at_ms(native["open_time_raw"]))
        payload = rest_identity.kline_1m_payload_hash(symbol, dict(native))
    return (
        key,
        payload,
        rest_identity.revision_id(key, rest_identity.rest_source_identity(), payload),
    )


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
    contract_schema_version: str | None = None,
) -> tuple[str, str, str, Mapping[str, Any]]:
    """``(key, revision id, payload hash, normalised row)`` of one element of a response.

    The single builder (s2): the element's times come from its native fields, its arrival
    number from the response's block base, its ``ingest_time`` / ``knowledge_time`` from the
    response revision; bindings, schema version and empty edges from the frozen rules.
    """
    subject = _ELEMENT_SUBJECTS[data_type]
    source = rest_identity.rest_source_identity()
    key, payload, revision_id = element_identity(data_type, symbol, native)
    times: dict[str, Any]
    if data_type == "agg_trades":
        event_time = _at_ms(native["timestamp_raw"])
        times = {"event_time": event_time}
        decision = decide_rest_availability(
            subject, event_time=event_time, ingest_time=ingest_time, knowledge_time=knowledge_time
        )
    else:
        start = _at_ms(native["open_time_raw"])
        end = _at_ms(native["close_time_raw"] + 1)
        times = {"interval_start": start, "interval_end": end}
        decision = decide_rest_availability(
            subject,
            event_time=start,
            event_end_time=end,
            ingest_time=ingest_time,
            knowledge_time=knowledge_time,
        )
    record = RevisionRecord(
        # An element is written, and rebuilt, at its response revision's version (one group).
        schema_version=_group_version(contract_schema_version),
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


def _normalised(
    definition: RegisteredTableDefinition, fields: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Native fields exactly as ``definition`` stores them (``decimal(38, 18)`` included)."""
    schema = pa.schema([definition.arrow_schema.field(name) for name in fields])
    rows: list[Mapping[str, Any]] = pa.Table.from_pylist([dict(fields)], schema=schema).to_pylist()
    return rows[0]


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


def _archive_rows_at(archive: SpooledArchive, lines: Sequence[int]) -> list[dict[str, Any]]:
    """Read requested 1-based archive lines from a validated spool with one batch live."""
    if not lines:
        return []
    wanted = tuple(line - 1 for line in lines)
    truths: list[dict[str, Any]] = []
    wanted_index = 0
    with archive.open_cursor() as cursor:
        while wanted_index < len(wanted):
            batch_index = wanted[wanted_index] // archive.batch_rows
            batch = cursor.read_batch(batch_index)
            while (
                wanted_index < len(wanted)
                and wanted[wanted_index] // archive.batch_rows == batch_index
            ):
                offset = wanted[wanted_index] % archive.batch_rows
                if offset >= batch.num_rows:
                    raise CatalogIntegrityError(
                        f"archive revision {archive.archive_revision_id} spool batch is short"
                    )
                truths.append(batch.slice(offset, 1).to_pylist()[0])
                wanted_index += 1
    if wanted_index != len(wanted):
        raise CatalogIntegrityError(
            f"archive revision {archive.archive_revision_id} spool ended before its declared rows"
        )
    return truths


# =========================================================================================
# the verifier
# =========================================================================================


@dataclass(frozen=True, slots=True)
class PageElement:
    """One element a proven, accepted response page holds (strict re-decode of its body)."""

    element_index: int
    observation_key: str
    revision_id: str


@dataclass(frozen=True, slots=True)
class VerifiedArchive:
    """Lightweight proven archive metadata: what an element row may inherit from it."""

    revision_id: str
    time_unit: TimeUnit
    row: Mapping[str, Any]
    row_count: int


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
        cache_archives: bool = False,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._storage_error = storage_error or (lambda exc: exc)
        #: Verified first-delivery collections, most recent last (immutable checkpoints).
        self._collections: dict[tuple[str, str], CommittedCollection] = {}
        self._batch_index: dict[
            tuple[str, str | None, tuple[str, ...]], _IndexedBatchHistory
        ] = {}
        #: Only for a verifier over a pinned, read-only view (one normalizer call, G3-S): archive
        #: identity metadata is memoized. Parsed archive spools are always call-local and closed.
        self._cache_archives = cache_archives
        self._archives: dict[tuple[str, str, str], VerifiedArchive] = {}

    def close(self) -> None:
        """Clear cached archive metadata; parsed-archive spools are never retained here."""
        self._archives.clear()

    def _cache_archive(self, key: tuple[str, str, str], item: VerifiedArchive) -> None:
        current = self._archives.get(key)
        if current is item:
            return
        if current is not None:
            del self._archives[key]
        while len(self._archives) >= _ARCHIVE_CACHE:
            oldest = next(iter(self._archives))
            self._archives.pop(oldest)
        self._archives[key] = item

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ first deliveries

    def _first_delivery_page(self, response: Mapping[str, Any]) -> tuple[CollectionRequest, Any]:
        """The verified checkpoint page that first delivered ``response`` (D3E-R3).

        The response row names its first delivery (collection request id + page index); that
        committed collection is re-read and strictly re-decoded by the accepted D3D reader, and
        the page must be exactly this response's page identity and body.
        """
        origin, request_id = response["request_origin"], response["collection_request_id"]
        key = (origin, request_id)
        collection = self._collections.get(key)
        if collection is None:
            try:
                reader = checkpoint_reader(self._storage, origin)
            except ValueError as exc:
                raise CatalogIntegrityError(
                    f"response origin {origin!r} is invalid: {exc}"
                ) from None
            try:
                collection = load_committed_collection(
                    reader, committed_request(reader, request_id)
                )
            except CommittedCheckpointError as exc:
                raise CatalogIntegrityError(
                    f"the first delivery {request_id!r} of response revision "
                    f"{response['revision_id']} does not reproduce: {exc}"
                ) from None
            except StorageError as exc:
                mapped = self._storage_error(exc)
                if mapped is exc:
                    raise
                raise mapped from exc
            finally:
                reader.close()
            if len(self._collections) >= _COLLECTION_CACHE:
                self._collections.pop(next(iter(self._collections)))
            self._collections[key] = collection
        pages = [
            page
            for symbol, records in collection.chains
            if symbol == response["symbol"]
            for page in records
            if page.page_index == response["page_index"]
        ]
        if len(pages) != 1:
            raise CatalogIntegrityError(
                f"the first delivery {request_id!r} has no page {response['page_index']} of "
                f"{response['symbol']} for response revision {response['revision_id']}"
            )
        page = pages[0]
        if (
            page.page_identity != response["page_identity_sha256"]
            or page.body.sha256 != response["payload_hash"]
        ):
            raise CatalogIntegrityError(
                f"the first delivery of response revision {response['revision_id']} is another "
                "page or body"
            )
        return collection.request, page

    # ------------------------------------------------------------------ catalog helpers

    def _scan(
        self,
        definition: RegisteredTableDefinition,
        row_filter: BooleanExpression,
        *,
        limit: int | None = None,
    ) -> list[Mapping[str, Any]]:
        """Materialize this bounded result without invoking the high-level scan planner."""
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise BatchRejected("scan limit must be a positive int or None")
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = []
        reader = self._adapter.scan_column_batches(
            definition.table,
            columns=columns,
            row_filter=row_filter,
        )
        try:
            for record_batch in reader:
                remaining = None if limit is None else limit - len(rows)
                bounded_batch = (
                    record_batch
                    if remaining is None or record_batch.num_rows <= remaining
                    else record_batch.slice(0, remaining)
                )
                rows.extend(bounded_batch.to_pylist())
                if limit is not None and len(rows) >= limit:
                    break
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
        return rows

    def _holders(self, table: str, seqs: Sequence[int]) -> dict[int, list[str]]:
        """Every row holding one of ``seqs``: range scans over runs of nearby numbers (G3-S)."""
        wanted = set(seqs)
        holders: dict[int, list[str]] = {}
        ordered = sorted(wanted)
        start = 0
        while start < len(ordered):
            first = ordered[start]
            end = start + 1
            while end < len(ordered) and ordered[end] - first < _HOLDER_SPAN:
                end += 1
            with _scan_rows(
                self._adapter,
                table,
                columns=("revision_id", "arrival_seq"),
                row_filter=And(
                    _at_least("arrival_seq", first), _below("arrival_seq", ordered[end - 1] + 1)
                ),
            ) as found:
                for item in found:
                    seq = item["arrival_seq"]
                    if seq not in wanted:
                        continue
                    revisions = holders.setdefault(seq, [])
                    # Two records prove a non-unique holder; retaining more corrupt duplicates
                    # cannot change the caller's fail-closed decision.
                    if len(revisions) < 2:
                        revisions.append(item["revision_id"])
            start = end
        return holders

    def _indexed(
        self, table: str, prefixes: Mapping[str, str]
    ) -> _IndexedBatchHistory:
        """Constant-space batch summaries, memoised per immutable head (G3-S)."""
        info = self._adapter.load_table(table)
        snapshot = None if info is None else info.current_snapshot
        head = None if snapshot is None else snapshot.snapshot_id
        key = (table, head, tuple(sorted(prefixes)))
        found = self._batch_index.get(key)
        if found is None:
            # Walk exactly the history of the head the memo is keyed by (a commit landing in
            # between must never be filed under the older head: review G3 cursor-3).
            walk = None if info is None else history_from(self._adapter, table, head)
            found = _IndexedBatchHistory(
                head=head,
                prefixes=tuple(sorted(prefixes)),
                summaries=_indexed_batches(self._adapter, table, prefixes, walk),
            )
            if len(self._batch_index) >= _BATCH_INDEX_CACHE:
                self._batch_index.pop(next(iter(self._batch_index)))
            self._batch_index[key] = found
        return found

    def _batch_snapshots(
        self, table: str, history: _IndexedBatchHistory
    ) -> Iterator[tuple[str, int, SnapshotInfo]]:
        """The second, streaming pass over exactly the summarized head."""
        return _iter_indexed_batches(self._adapter, table, history)

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
        # Rebuilt at the version the row was committed with (ADR-0052 versioned replay, V1).
        version = contract_version.replay_version(
            stored.get("contract_schema_version"),
            what=f"committed response revision {revision}",
        )
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
                contract_schema_version=version,
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
        # D3E-R3: the provenance columns are not free inputs — rebuild the whole row from the
        # verified checkpoint page of its first delivery.
        request, page = self._first_delivery_page(stored)
        try:
            delivered = response_columns(
                data_type=request.data_type,
                source_binding=(request.source.source_id, request.source.version),
                query=page.query,
                origin=stored["request_origin"],
                page_identity=page.page_identity,
                source_uri=page.source_uri,
                http_status=page.http_status,
                body=(page.body.key, page.body.uri, page.body.sha256, page.body.size),
                base=stored["arrival_seq"],
                knowledge_time=stored["knowledge_time"],
                provenance=page_provenance(request, page),
                contract_schema_version=version,
            )
        except (CommittedCheckpointError, KeyError, TypeError, ValueError) as exc:
            raise CatalogIntegrityError(
                f"committed response revision {revision} cannot be rebuilt from its first "
                f"delivery: {exc}"
            ) from None
        mismatched = _mismatched(
            batch_rows(batch(BINANCE_SPOT_REST_RESPONSES, [delivered]))[0], stored
        )
        if mismatched:
            raise CatalogIntegrityError(
                f"committed response revision {revision} does not reproduce from its first "
                f"delivery: {mismatched}"
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
        with _spooled_snapshots_of_batches(self._adapter, table, batches) as snapshots:
            for batch_id, row in batches.items():
                snapshot = _one_snapshot(table, batch_id, snapshots.one(batch_id))
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
                    contract_schema_version=response["contract_schema_version"],
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
            # D3E-R3: the natives must be the element the lineage response's body holds at
            # this index (strict re-decode of its first delivery), not merely self-consistent.
            _, page = self._first_delivery_page(response)
            decoded = page.outcome
            element = (
                None
                if not isinstance(decoded, RestPageDecoded)
                else next((e for e in decoded.elements if e.element_index == index), None)
            )
            if element is None:
                raise CatalogIntegrityError(
                    f"{table}: element {revision}: the body of its lineage response "
                    f"{response['revision_id']} holds no element {index}"
                )
            natives = _normalised(definition, element.native_fields())
            wrong = sorted(name for name, value in natives.items() if row[name] != value)
            if wrong:
                raise CatalogIntegrityError(
                    f"{table}: element {revision} is not element {index} of its lineage "
                    f"response's body: {wrong}"
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
        found = self._indexed(table, prefixes)
        members_by_prefix = {
            prefix: sorted(by_lineage[lineage], key=lambda row: row["element_index"])
            for prefix, lineage in prefixes.items()
        }
        later_rows = {prefix: 0 for prefix in prefixes}
        seen = {prefix: 0 for prefix in prefixes}
        bounds = {prefix: [1, MAX_ELEMENT_MICROBATCH_ROWS] for prefix in prefixes}
        for prefix, lineage in prefixes.items():
            members = members_by_prefix[prefix]
            summary = found.summaries[prefix]
            if summary.added_rows != len(members):
                raise CatalogIntegrityError(
                    f"{table}: response revision {lineage} has {len(members)} element row(s) but "
                    f"its element batches committed {summary.added_rows}"
                )
        for prefix, index, snapshot in self._batch_snapshots(table, found):
            lineage = prefixes[prefix]
            members = members_by_prefix[prefix]
            batch_id = element_batch_id(lineage, index)
            start = len(members) - later_rows[prefix] - snapshot.added_rows
            if start < 0:
                raise CatalogIntegrityError(
                    f"{table}: response revision {lineage} has inconsistent batch row counts"
                )
            part = members[start : start + snapshot.added_rows]
            if len(part) != snapshot.added_rows:
                raise CatalogIntegrityError(
                    f"{table}: response revision {lineage} has inconsistent batch row counts"
                )
            later_rows[prefix] += snapshot.added_rows
            seen[prefix] += 1
            check_batch_snapshot(definition, batch_id, snapshot, part)
            low, high = bounds[prefix]
            for row in part:
                element = row["element_index"]
                low = max(low, element // (index + 1) + 1)
                if index:
                    high = min(high, element // index)
            bounds[prefix] = [low, high]
        for prefix, lineage in prefixes.items():
            summary = found.summaries[prefix]
            if seen[prefix] != summary.count or later_rows[prefix] != len(
                members_by_prefix[prefix]
            ):
                raise CatalogIntegrityError(
                    f"{table}: response revision {lineage} batch history changed during "
                    "verification"
                )
            if bounds[prefix][0] > bounds[prefix][1]:
                raise CatalogIntegrityError(
                    f"{table}: the element batches of response revision {lineage} follow no "
                    "single microbatch plan"
                )

    def page_elements(
        self, data_type: str, response_revision_id: str
    ) -> tuple[Mapping[str, Any], tuple[PageElement, ...]]:
        """The one committed response revision and every element its body holds (G2-R1a).

        The response row is proven lawful first (``verify_responses``); an accepted page is then
        strictly re-decoded from its first delivery and must hold exactly ``element_count``
        elements. Each element's identity comes from ``element_identity`` — the store's own —
        so a reader can tell which of them the element table must hold, under this response or
        (first delivered by another page, never re-written: ADR-0027) under another one. A
        rejected page holds none.
        """
        definition = ELEMENT_DEFINITIONS.get(data_type)
        if definition is None:
            raise CatalogIntegrityError(f"unsupported REST data_type {data_type!r}")
        found = self._scan(
            BINANCE_SPOT_REST_RESPONSES, _equals("revision_id", response_revision_id)
        )
        if len(found) != 1:
            raise CatalogIntegrityError(
                f"{response_revision_id} is committed {len(found)} time(s) in "
                f"{BINANCE_SPOT_REST_RESPONSES.table}"
            )
        [response] = found
        self.verify_responses([response])
        if response["data_type"] != data_type:
            raise CatalogIntegrityError(
                f"response revision {response_revision_id} is not a {data_type} page"
            )
        if response["decode_outcome"] != ACCEPTED:
            return response, ()
        _, page = self._first_delivery_page(response)
        decoded = page.outcome
        if (
            not isinstance(decoded, RestPageDecoded)
            or len(decoded.elements) != (response["element_count"])
        ):
            raise CatalogIntegrityError(
                f"the body of response revision {response_revision_id} does not decode to its "
                f"{response['element_count']} element(s)"
            )
        elements: list[PageElement] = []
        for element in decoded.elements:
            natives = _normalised(definition, element.native_fields())
            try:
                key, _, revision_id = element_identity(data_type, response["symbol"], natives)
            except (rest_identity.RestIdentityViolation, KeyError, TypeError) as exc:
                raise CatalogIntegrityError(
                    f"element {element.element_index} of response revision "
                    f"{response_revision_id} has no lawful identity: {exc}"
                ) from None
            elements.append(PageElement(element.element_index, key, revision_id))
        return response, tuple(elements)

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
            archive = lineage[archive_id]
            collected = self._lawful_archive_row(archive.row)
            parsed = self._reparse(collected, data_type, archive_id)
            try:
                if parsed.row_count != archive.row_count:
                    raise CatalogIntegrityError(
                        f"archive revision {archive_id} changed row count during verification"
                    )
                self._verify_archive_rows(definition, data_type, archive, parsed, members)
            finally:
                parsed.close()
        self._check_sole_holders(
            table, {row["arrival_seq"]: row["revision_id"] for row in rows}, "row"
        )
        self._verify_archive_row_batches(definition, by_archive)
        return lineage

    def archive_row_count(self, data_type: str, symbol: str, archive_id: str) -> int:
        """Lines of the verified, strictly re-parsed object of one archive revision."""
        verified = self._verified_archives(data_type, symbol, [archive_id])
        return verified[archive_id].row_count

    def _verified_archives(
        self, data_type: str, symbol: str, archive_ids: Sequence[str]
    ) -> dict[str, VerifiedArchive]:
        """Each named archive revision: committed once, for this data type and symbol, lawful."""
        requested = tuple(dict.fromkeys(archive_ids))
        cached = {
            archive_id: self._archives[(data_type, symbol, archive_id)]
            for archive_id in requested
            if (data_type, symbol, archive_id) in self._archives
        }
        if len(cached) == len(requested):
            # A metadata cache cannot stand in for the old retained parser spools when ordering
            # failures: revalidate every strict parse before any caller checks member rows.
            for archive_id in requested:
                item = cached[archive_id]
                collected = self._lawful_archive_row(item.row)
                parsed = self._reparse(collected, data_type, archive_id)
                try:
                    if parsed.row_count != item.row_count:
                        raise CatalogIntegrityError(
                            f"archive revision {archive_id} changed row count during verification"
                        )
                finally:
                    parsed.close()
            return cached
        table = BINANCE_SPOT_ARCHIVES.table
        found: dict[str, list[Mapping[str, Any]]] = {}
        for chunk in _chunks(requested):
            # Every id is expected exactly once. The extra row is enough to prove a duplicate
            # exists, without materializing every corrupt copy of one revision in this query.
            rows = self._scan(
                BINANCE_SPOT_ARCHIVES,
                _member("revision_id", chunk),
                limit=len(chunk) + 1,
            )
            if len(rows) > len(chunk) + 1:
                raise CatalogIntegrityError(
                    f"{table}: archive revision lookup exceeded its bounded scan limit"
                )
            requested_chunk = set(chunk)
            for row in rows:
                archive_id = row["revision_id"]
                if archive_id not in requested_chunk:
                    raise CatalogIntegrityError(
                        f"{table}: archive revision lookup returned an unrequested revision"
                    )
                held = found.setdefault(archive_id, [])
                held.append(row)
                if len(held) > 1:
                    raise CatalogIntegrityError(
                        f"{table}: archive revision {archive_id} is committed "
                        f"{len(held)} time(s) or more"
                    )
        verified: dict[str, VerifiedArchive] = {}
        for archive_id in requested:
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
            collected = self._lawful_archive_row(row)
            # Preserve error precedence: every strict parser failure precedes the archive
            # holder / snapshot checks below. Close each parse immediately; member-row checks
            # perform a second serial parse after those historical checks have succeeded.
            parsed = self._reparse(collected, data_type, archive_id)
            try:
                verified[archive_id] = VerifiedArchive(
                    revision_id=archive_id,
                    time_unit=time_unit_for(data_type, row["coverage_start"]),
                    row=row,
                    row_count=parsed.row_count,
                )
            finally:
                parsed.close()
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
        with _spooled_snapshots_of_batches(self._adapter, table, batches) as snapshots:
            for batch_id, row in batches.items():
                snapshot = _one_snapshot(table, batch_id, snapshots.one(batch_id))
                check_batch_snapshot(BINANCE_SPOT_ARCHIVES, batch_id, snapshot, [row])
        # The cache stores only lineage metadata. The parsed rows are never retained across
        # archives or verifier calls; member checks reopen one strict parser spool at a time.
        if self._cache_archives and len(verified) == 1:
            for archive_id, item in verified.items():
                self._cache_archive((data_type, symbol, archive_id), item)
        return verified

    def _reparse(
        self, collected: CollectedObject, data_type: str, archive_id: str
    ) -> SpooledArchive:
        """The strict D1 parse of the published object (D3E-R3): what D2 must have written."""
        try:
            outcome = parse_archive_spooled(
                ArchiveParseRequest.for_collected_object(
                    collected, data_type=data_type, archive_revision_id=archive_id
                ),
                self._storage,
            )
        except IntegrityViolation as exc:
            raise CatalogIntegrityError(
                f"the object of archive revision {archive_id} does not match: {exc}"
            ) from None
        except StorageError as exc:
            mapped = self._storage_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        if isinstance(outcome, ArchiveRejection) or not isinstance(outcome, SpooledArchive):
            raise CatalogIntegrityError(
                f"archive revision {archive_id} is committed but its object does not parse: "
                "D2 never writes a revision for a rejected archive"
            )
        return outcome

    def _lawful_archive_row(self, stored: Mapping[str, Any]) -> CollectedObject:
        """Rebuild an archive revision row with the D2 builder from its published object.

        Inputs: the published object (looked up by its key), the official path / checksum and
        its source metadata, the collection request, the block base and ``knowledge_time``.
        Derived and compared: identity (D2 binding), the frozen D0 collector and source
        bindings, the availability decision under the frozen policy, no precedence edge.
        """
        revision = stored.get("revision_id")
        # Rebuilt at the version the row was committed with (ADR-0052 versioned replay, V1).
        version = contract_version.replay_version(
            stored.get("contract_schema_version"),
            what=f"committed archive revision {revision}",
        )
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
                schema_version=version,
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
        return collected

    def _verify_archive_rows(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        archive: VerifiedArchive,
        parsed: SpooledArchive,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Rebuild each row with the D2 builder from its natives and the archive revision."""
        table = definition.table
        schema = _PARSER_ROW_SCHEMAS[data_type]
        ordered = sorted(rows, key=lambda row: (row["archive_line_number"], row["revision_id"]))
        # The rows up to the first one naming no line of the object; their lines are taken from
        # the parsed object in one read (G3-P), then checked in order before that refusal, so the
        # first failing row and its message are exactly those of a row-by-row check.
        lines: list[int] = []
        outside: Mapping[str, Any] | None = None
        for row in ordered:
            line = row["archive_line_number"]
            if (
                not isinstance(line, int)
                or isinstance(line, bool)
                or not 1 <= line <= parsed.row_count
            ):
                outside = row
                break
            lines.append(line)
        truths = _archive_rows_at(parsed, lines)
        for row, line, truth in zip(ordered, lines, truths, strict=False):
            # D3E-R3: the parser columns must be exactly what the object holds at that line.
            wrong = sorted(name for name in schema.names if row[name] != truth[name])
            if wrong:
                raise CatalogIntegrityError(
                    f"{table}: row {row['revision_id']} is not line {line} of archive revision "
                    f"{archive.revision_id}'s object: {wrong}"
                )
        if outside is not None:
            raise CatalogIntegrityError(
                f"{table}: row {outside['revision_id']} (line "
                f"{outside['archive_line_number']!r}) is not a line of the "
                f"{parsed.row_count}-line object of archive revision "
                f"{archive.revision_id}"
            )
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
                # One write group: the rows inherit their archive revision's version (V1).
                contract_schema_version=archive.row["contract_schema_version"],
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
        found = self._indexed(table, prefixes)
        plans: dict[str, tuple[int, int, int, set[int]]] = {}
        for prefix, archive_id in sorted(prefixes.items()):
            summary = found.summaries[prefix]
            if summary.count == 0:
                raise CatalogIntegrityError(
                    f"{table}: archive revision {archive_id} has rows but no committed row batch"
                )
            if summary.newest_index != summary.count - 1 or summary.oldest_index != 0:
                raise CatalogIntegrityError(
                    f"{table}: the row batches of archive revision {archive_id} are not a "
                    "contiguous prefix"
                )
            size = summary.index_zero_rows
            last_size = summary.newest_rows
            if (
                size is None
                or last_size is None
                or size < 1
                or (summary.count > 2 and summary.regular_rows != size)
                or last_size > size
            ):
                raise CatalogIntegrityError(
                    f"{table}: the row batches of archive revision {archive_id} follow no single "
                    "microbatch plan"
                )
            needed: set[int] = set()
            for row in rows[archive_id]:
                position = row["archive_line_number"] - 1
                index = position // size if position >= 0 else -1
                batch_size = last_size if index == summary.count - 1 else size
                if (
                    not 0 <= index < summary.count
                    or batch_size is None
                    or position >= index * size + batch_size
                ):
                    raise CatalogIntegrityError(
                        f"{table}: row {row['revision_id']} (line {row['archive_line_number']}) "
                        f"is not committed by any batch of archive revision {archive_id}"
                )
                needed.add(index)
            plans[prefix] = (size, last_size, summary.count, needed)
        checked: dict[str, set[int]] = {prefix: set() for prefix in prefixes}
        observed = {prefix: 0 for prefix in prefixes}
        for batch_prefix, index, snapshot in self._batch_snapshots(table, found):
            observed[batch_prefix] += 1
            plan = plans[batch_prefix]
            size, last_size, count, needed = plan
            if index not in needed:
                continue
            archive_id = prefixes[batch_prefix]
            batch_size = last_size if index == count - 1 else size
            first = index * size + 1
            current = self._scan(
                definition,
                And(
                    _equals("archive_revision_id", archive_id),
                    And(
                        _at_least("archive_line_number", first),
                        _below("archive_line_number", first + batch_size),
                    ),
                ),
            )
            current.sort(key=lambda row: (row["archive_line_number"], row["revision_id"]))
            check_batch_snapshot(
                definition, _row_batch_id(archive_id, index), snapshot, current
            )
            checked[batch_prefix].add(index)
        for prefix, archive_id in prefixes.items():
            if (
                checked[prefix] != plans[prefix][3]
                or observed[prefix] != found.summaries[prefix].count
            ):
                raise CatalogIntegrityError(
                    f"{table}: the row batches of archive revision {archive_id} changed during "
                    "verification"
                )
