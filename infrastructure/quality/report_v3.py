"""Bounded canonical-partition v3 Quality reporter.

This is an additive ADR-0093 path.  The legacy ``QualityReporter`` and its v1/v2 rows are
intentionally left alone.  The v3 path pins its finite input set, proves Raw-to-Canonical
coverage with sorted RunSet joins, validates REST responses one page at a time, then writes the
three evidence streams before the fixed-size manifest commit point.

All capacities and byte limits are caller supplied.  The PIT implementation still has its
documented per-key graph/history working-set boundary; this module makes no E1-CAP-1 or fixed
total-RSS claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import groupby, zip_longest
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from pyiceberg.expressions import AlwaysTrue, And, EqualTo, GreaterThanOrEqual, LessThan

from core.contracts.catalog import BatchConflict, CommitConflict, TableNotFound
from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import (
    ObjectRef,
    PublishResult,
    StagedObject,
    StageRequest,
    StorageAdapter,
)
from core.contracts.universe import PitConflictHeadEvidence
from core.domain.base import FrozenMapping, canonical_json
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import check_rest_page
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.pit.selector import (
    PIT_BINDING,
    REQUIRED_BINDINGS,
    PitRunParams,
    PitSelector,
)
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality.manifest_store import (
    QualityReportManifestStore,
    derive_quality_report_id,
)
from infrastructure.quality.report_projection import (
    CANONICAL_PARTITION_V3_RULE_HASH,
    CANONICAL_PARTITION_V3_RULE_ID,
    CANONICAL_PARTITION_V3_RULE_VERSION,
    CanonicalPartitionV3EvidenceGapProjector,
    CanonicalPartitionV3Projector,
)
from infrastructure.quality.report_streams import (
    QualityReportStreamLimits,
    QualityReportStreamWriter,
    iter_quality_report_stream,
)
from infrastructure.quality.reporter import RawNotDerived
from infrastructure.quality.scratch import local_storage_roots_overlap
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.rest_identity import PAGE_LIMIT
from infrastructure.revision.row_integrity import ACCEPTED, PersistedRowVerifier, history_from
from infrastructure.revision.store import RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = ["QualityReportV3Error", "QualityReportedV3", "QualityReporterV3"]

_UTC_ZERO: Final = timedelta(0)
_DAY: Final = timedelta(days=1)
_INPUT_TABLES: Final[Mapping[str, tuple[str, ...]]] = {
    "agg_trades": (
        rules.CANONICAL_TABLES["agg_trades"].table,
        BINANCE_SPOT_AGG_TRADES.table,
        BINANCE_SPOT_REST_AGG_TRADES.table,
        BINANCE_SPOT_ARCHIVES.table,
        BINANCE_SPOT_REST_RESPONSES.table,
        BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
    ),
    "klines_1m": (
        rules.CANONICAL_TABLES["klines_1m"].table,
        BINANCE_SPOT_KLINES_1M.table,
        BINANCE_SPOT_REST_KLINES_1M.table,
        BINANCE_SPOT_ARCHIVES.table,
        BINANCE_SPOT_REST_RESPONSES.table,
        BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
    ),
}
_RAW_TABLES: Final[Mapping[str, tuple[str, ...]]] = {
    "agg_trades": (BINANCE_SPOT_AGG_TRADES.table, BINANCE_SPOT_REST_AGG_TRADES.table),
    "klines_1m": (BINANCE_SPOT_KLINES_1M.table, BINANCE_SPOT_REST_KLINES_1M.table),
}
_REQUIRED_SNAPSHOT_TABLES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        data_type: tuple(
            table for table in tables if table != BINANCE_SPOT_PRECEDENCE_EVIDENCE.table
        )
        for data_type, tables in _INPUT_TABLES.items()
    }
)


def _identity_rule_hash_registry() -> Mapping[str, str]:
    hashes = {
        "quality": CANONICAL_PARTITION_V3_RULE_HASH,
        "pit": PIT_BINDING.policy_hash,
    }
    for binding in (
        *REQUIRED_BINDINGS["availability_bindings"],
        *REQUIRED_BINDINGS["precedence_bindings"],
        *REQUIRED_BINDINGS["parser_bindings"],
        DELIVERY_CHANNEL_BINDING,
        rules.PRECEDENCE_MAP_BINDING,
    ):
        hashes[f"{binding.policy_id}@{binding.version}"] = binding.policy_hash
    return MappingProxyType(dict(sorted(hashes.items())))


_IDENTITY_RULE_HASHES: Final = _identity_rule_hash_registry()
_MAX_IDENTITY_RULE_HASHES: Final = len(_IDENTITY_RULE_HASHES)


class QualityReportV3Error(Exception):
    """A v3 report cannot be proved or committed."""


class _RunStorage:
    """Write all scratch objects separately; read market objects from evidence storage."""

    def __init__(self, evidence: StorageAdapter, scratch: StorageAdapter) -> None:
        self._evidence = evidence
        self._scratch = scratch

    def stage(self, request: StageRequest, content: Any) -> StagedObject:
        return self._scratch.stage(request, content)

    def publish(self, staged: StagedObject) -> PublishResult:
        return self._scratch.publish(staged)

    def lookup(self, key: str) -> ObjectRef | None:
        return self._scratch.lookup(key) or self._evidence.lookup(key)

    def open_read(self, ref: ObjectRef) -> Any:
        scratch_ref = self._scratch.lookup(ref.key)
        if scratch_ref is not None and (scratch_ref.sha256, scratch_ref.size) == (
            ref.sha256,
            ref.size,
        ):
            return self._scratch.open_read(ref)
        return self._evidence.open_read(ref)


@dataclass(frozen=True, slots=True)
class QualityReportedV3:
    report_id: str
    manifest: Mapping[str, Any]
    reused: bool


def _filter_day(
    data_type: str, symbol: str, start: datetime, end: datetime, *, canonical: bool = False
) -> Any:
    col = "event_time" if data_type == "agg_trades" else "interval_start"
    target_symbol = rules.SYMBOLS[symbol].symbol if canonical else symbol
    return And(
        EqualTo("symbol", target_symbol),  # type: ignore[call-arg, arg-type]
        And(
            GreaterThanOrEqual(col, start),  # type: ignore[call-arg, arg-type]
            LessThan(col, end),  # type: ignore[call-arg, arg-type]
        ),
    )


def _scan_rows(
    adapter: RevisionCatalog,
    table: str,
    columns: Sequence[str],
    *,
    row_filter: Any,
    snapshot_id: str,
) -> Iterator[dict[str, Any]]:
    reader = adapter.scan_column_batches(
        table, columns=columns, row_filter=row_filter, snapshot_id=snapshot_id
    )
    try:
        for batch in reader:
            yield from batch.to_pylist()
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


def _row_size(row: Mapping[str, Any], maximum: int) -> None:
    """Conservatively cap a row without building its whole serialized representation."""
    size = 0
    pending: list[Any] = [row]

    def add(amount: int) -> None:
        nonlocal size
        size += amount
        if size > maximum:
            raise QualityReportV3Error("input row exceeds max_input_record_bytes")

    def add_text(value: str) -> None:
        add(2)
        for offset in range(0, len(value), 128):
            try:
                escaped = json.dumps(value[offset : offset + 128], ensure_ascii=False)[1:-1]
                add(len(escaped.encode("utf-8")))
            except UnicodeEncodeError as exc:
                raise QualityReportV3Error("input row text is not valid UTF-8") from exc

    while pending:
        value = pending.pop()
        if isinstance(value, str):
            add_text(value)
        elif isinstance(value, Mapping):
            add(2 + max(0, len(value) - 1) + len(value))
            for key in value:
                if not isinstance(key, str):
                    raise QualityReportV3Error("input row has a non-text object key")
                add_text(key)
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            add(2 + max(0, len(value) - 1))
            pending.extend(value)
        elif value is None or isinstance(value, bool):
            add(4 if value is True else 5)
        elif isinstance(value, (int, float)):
            add(len(json.dumps(value, allow_nan=False).encode("ascii")))
        elif isinstance(value, datetime):
            add_text(value.isoformat())
        else:
            # Decimal and fixed-width Arrow values use a bounded scalar representation.
            add(len(str(value)))
    add(1)  # LF / scratch-row framing reserve


def _merge_join(
    storage: StorageAdapter,
    raw: RunRef | None,
    images: RunRef | None,
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    """Yield every raw identity with its unique image, without a cardinality-sized set."""

    @contextmanager
    def rows(ref: RunRef | None) -> Iterator[Iterator[Mapping[str, Any]]]:
        if ref is None:
            yield iter(())
        else:
            with iter_run(storage, ref) as values:
                yield values

    with rows(raw) as raw_rows, rows(images) as image_rows:
        image = next(image_rows, None)
        for source in raw_rows:
            revision = source["revision_id"]
            while image is not None and image["raw_revision_id"] < revision:
                image = next(image_rows, None)
            matched: Mapping[str, Any] | None = None
            if image is not None and image["raw_revision_id"] == revision:
                matched = image
                image = next(image_rows, None)
                if image is not None and image["raw_revision_id"] == revision:
                    raise CatalogIntegrityError(
                        f"Raw revision {revision} has multiple Canonical images"
                    )
            yield source, matched


def _stream_ref_row(ref: Any) -> dict[str, Any]:
    return {
        "format_id": ref.format,
        "record_count": ref.record_count,
        "leaf_count": ref.leaf_count,
        "depth": ref.depth,
        "root_key": ref.root_key,
        "root_sha256": ref.root_sha256,
        "root_size": ref.root_size,
    }


class QualityReporterV3:
    """Append or verify one canonical-partition@3.0.0 report using bounded evidence streams."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        canonical_scratch_directory: Path,
        scratch_storage: StorageAdapter,
        clock: Callable[[], datetime],
        pit_params: PitRunParams,
        run_capacity: int,
        merge_fanout: int,
        run_limits: RunLimits,
        stream_limits: QualityReportStreamLimits,
        max_event_record_bytes: int,
        max_revision_record_bytes: int,
        max_gap_record_bytes: int,
        max_input_record_bytes: int,
        max_manifest_record_bytes: int,
        max_identity_bytes: int,
        retries: int,
    ) -> None:
        for name, number, minimum in (
            ("run_capacity", run_capacity, 1),
            ("merge_fanout", merge_fanout, 2),
            ("max_event_record_bytes", max_event_record_bytes, 1),
            ("max_revision_record_bytes", max_revision_record_bytes, 1),
            ("max_gap_record_bytes", max_gap_record_bytes, 1),
            ("max_input_record_bytes", max_input_record_bytes, 1),
            ("max_manifest_record_bytes", max_manifest_record_bytes, 1),
            ("max_identity_bytes", max_identity_bytes, 1),
            ("retries", retries, 1),
        ):
            if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not isinstance(pit_params, PitRunParams) or not isinstance(run_limits, RunLimits):
            raise ValueError("pit_params and run_limits must be their explicit limits types")
        if not isinstance(stream_limits, QualityReportStreamLimits):
            raise ValueError("stream_limits must be QualityReportStreamLimits")
        if scratch_storage is storage:
            raise ValueError(
                "scratch_storage must be a distinct adapter; caller must ensure its namespace "
                "is isolated from evidence storage"
            )
        if local_storage_roots_overlap(storage, scratch_storage):
            raise ValueError("scratch_storage roots must not overlap evidence storage roots")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._adapter = adapter
        self._storage = storage
        self._scratch = scratch_storage
        self._clock = clock
        self._pit_params = pit_params
        self._capacity = run_capacity
        self._fanout = merge_fanout
        self._run_limits = run_limits
        self._stream_limits = stream_limits
        self._limits = {
            "event": max_event_record_bytes,
            "revision": max_revision_record_bytes,
            "gap": max_gap_record_bytes,
            "input": max_input_record_bytes,
            "manifest": max_manifest_record_bytes,
            "identity": max_identity_bytes,
            "hashes": _MAX_IDENTITY_RULE_HASHES,
            "retries": retries,
            "scratch_record": run_limits.leaf_max_bytes - 512,
        }
        self._identity_hashes = _IDENTITY_RULE_HASHES
        self._stores = {
            data_type: QualityReportManifestStore(
                adapter,
                quality_rule_id=CANONICAL_PARTITION_V3_RULE_ID,
                quality_rule_version=CANONICAL_PARTITION_V3_RULE_VERSION,
                quality_rule_hash=CANONICAL_PARTITION_V3_RULE_HASH,
                allowed_snapshot_tables=tables,
                required_snapshot_tables=_REQUIRED_SNAPSHOT_TABLES[data_type],
                identity_rule_hashes=_IDENTITY_RULE_HASHES,
                max_identity_rule_hashes=_MAX_IDENTITY_RULE_HASHES,
                stream_limits=stream_limits,
                max_manifest_record_bytes=max_manifest_record_bytes,
            )
            for data_type, tables in _INPUT_TABLES.items()
        }
        self._run_storage = _RunStorage(storage, scratch_storage)
        self._selector = PitSelector(
            adapter,
            self._run_storage,
            canonical_scratch_directory=canonical_scratch_directory,
        )

    def report(
        self, data_type: str, symbol: str, day: date, *, existing_only: bool = False
    ) -> QualityReportedV3:
        if data_type not in _INPUT_TABLES:
            raise QualityReportV3Error(f"unsupported data_type {data_type!r}")
        if not isinstance(symbol, str) or not symbol.strip():
            raise QualityReportV3Error("symbol must be non-empty text")
        if symbol not in rules.SYMBOLS:
            raise QualityReportV3Error(f"unsupported symbol {symbol!r}")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise QualityReportV3Error("day must be a datetime.date")
        start = datetime.combine(day, time(), tzinfo=UTC)
        end = start + _DAY
        tables = _INPUT_TABLES[data_type]
        for _ in range(self._limits["retries"]):
            bindings = self._pin(tables)
            canonical = rules.CANONICAL_TABLES[data_type].table
            if canonical not in bindings:
                raise QualityReportV3Error(f"{canonical} has no pinned snapshot")
            binding_rows = [
                {"table": table, "snapshot_id": sid} for table, sid in sorted(bindings.items())
            ]
            report_id = derive_quality_report_id(
                quality_rule_id=CANONICAL_PARTITION_V3_RULE_ID,
                quality_rule_version=CANONICAL_PARTITION_V3_RULE_VERSION,
                quality_rule_hash=CANONICAL_PARTITION_V3_RULE_HASH,
                identity_rule_hashes=self._identity_hashes,
                max_identity_rule_hashes=self._limits["hashes"],
                subject_table=canonical,
                subject_snapshot_id=bindings[canonical],
                subject_symbol=symbol,
                subject_start=start,
                subject_end=end,
                snapshot_bindings=binding_rows,
                max_identity_bytes=self._limits["identity"],
            )
            store = self._stores[data_type]
            existing = store.lookup(report_id)
            if existing is None and existing_only:
                raise QualityReportV3Error(f"no committed v3 quality report {report_id}")
            try:
                refs, event_counts, floor = self._derive_streams(
                    data_type,
                    symbol,
                    start,
                    end,
                    bindings,
                    report_id,
                    existing_only=existing is not None,
                )
            except (CommitConflict, BatchConflict):
                continue
            if existing is not None:
                if floor is not None and existing["knowledge_time"] < floor:
                    raise CatalogIntegrityError(
                        "stored report knowledge_time predates a Canonical revision or edge"
                    )
                expected = {"events": refs[0], "event_revisions": refs[1], "evidence_gaps": refs[2]}
                if any(existing[name] != _stream_ref_row(ref) for name, ref in expected.items()):
                    raise CatalogIntegrityError(
                        "stored v3 stream descriptors differ from re-derivation"
                    )
                for name, expected_ref in expected.items():
                    with (
                        iter_quality_report_stream(
                            self._storage,
                            self._quality_ref(name, existing[name]),
                            limits=self._stream_limits,
                        ) as stored_rows,
                        iter_quality_report_stream(
                            self._scratch, expected_ref, limits=self._stream_limits
                        ) as derived_rows,
                    ):
                        sentinel = object()
                        for old, new in zip_longest(stored_rows, derived_rows, fillvalue=sentinel):
                            if old is sentinel or new is sentinel or old != new:
                                raise CatalogIntegrityError(
                                    f"stored v3 {name} rows differ from re-derivation"
                                )
                return QualityReportedV3(report_id, existing, True)
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _UTC_ZERO:
                raise QualityReportV3Error("clock must return aware UTC")
            if floor is not None and now < floor:
                raise QualityReportV3Error(
                    "report clock precedes a Canonical revision or precedence edge"
                )
            row = {
                "report_id": report_id,
                "quality_rule_id": CANONICAL_PARTITION_V3_RULE_ID,
                "quality_rule_version": CANONICAL_PARTITION_V3_RULE_VERSION,
                "quality_rule_hash": CANONICAL_PARTITION_V3_RULE_HASH,
                "subject_table": canonical,
                "subject_snapshot_id": bindings[canonical],
                "subject_symbol": symbol,
                "subject_start": start,
                "subject_end": end,
                "snapshot_bindings": binding_rows,
                "knowledge_time": now,
                "events": refs[0],
                "event_revisions": refs[1],
                "evidence_gaps": refs[2],
            }
            try:
                committed, reused = store.commit_or_reuse(row)
            except (CommitConflict, BatchConflict):
                continue
            if reused:
                # A concurrent/retried report of this ID won: every stream root is equal (the
                # store compared all but knowledge_time), so only its knowledge floor is left.
                if floor is not None and committed["knowledge_time"] < floor:
                    raise CatalogIntegrityError(
                        "stored report knowledge_time predates a Canonical revision or edge"
                    )
                return QualityReportedV3(report_id, committed, True)
            return QualityReportedV3(report_id, committed, False)
        raise QualityReportV3Error("v3 report lost repeated manifest commit races")

    @staticmethod
    def _quality_ref(name: str, row: Mapping[str, Any]) -> Any:
        from infrastructure.quality.report_streams import QualityReportStreamRef

        return QualityReportStreamRef(
            name,
            row["format_id"],
            row["record_count"],
            row["leaf_count"],
            row["depth"],
            row["root_key"],
            row["root_sha256"],
            row["root_size"],
        )

    def _pin(self, tables: Sequence[str]) -> dict[str, str]:
        for _ in range(self._limits["retries"]):
            first = {table: self._head(table) for table in tables}
            second = {table: self._head(table) for table in tables}
            if first == second:
                return {table: sid for table, sid in first.items() if sid is not None}
        raise QualityReportV3Error("input tables kept moving while pinning")

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def _check_raw_derived(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        bindings: Mapping[str, str],
    ) -> None:
        canonical = rules.CANONICAL_TABLES[data_type].table
        time_column = "event_time" if data_type == "agg_trades" else "interval_start"
        for raw_table in _RAW_TABLES[data_type]:
            raw_snapshot = bindings.get(raw_table)
            if raw_snapshot is None:
                continue
            unit_column = rules.raw_channel_of(raw_table).lineage_column
            raw_builder = RunSetBuilder(
                self._scratch,
                key=lambda row: row["revision_id"],
                capacity=self._capacity,
                merge_fanout=self._fanout,
                limits=self._run_limits,
            )
            image_builder = RunSetBuilder(
                self._scratch,
                key=lambda row: row["raw_revision_id"],
                capacity=self._capacity,
                merge_fanout=self._fanout,
                limits=self._run_limits,
            )
            missing_units = RunSetBuilder(
                self._scratch,
                key=lambda row: row["unit"],
                capacity=self._capacity,
                merge_fanout=self._fanout,
                limits=self._run_limits,
            )
            with raw_builder, image_builder, missing_units:
                for row in _scan_rows(
                    self._adapter,
                    raw_table,
                    ("revision_id", unit_column, time_column),
                    row_filter=_filter_day(data_type, symbol, start, end),
                    snapshot_id=raw_snapshot,
                ):
                    _row_size(
                        row,
                        min(self._limits["input"], self._limits["scratch_record"]),
                    )
                    raw_builder.add({"revision_id": row["revision_id"], "unit": row[unit_column]})
                image_filter = And(
                    EqualTo("lineage_raw_table", raw_table),  # type: ignore[call-arg, arg-type]
                    _filter_day(data_type, symbol, start, end, canonical=True),
                )
                for row in _scan_rows(
                    self._adapter,
                    canonical,
                    ("lineage_raw_revision_id", "revision_id"),
                    row_filter=image_filter,
                    snapshot_id=bindings[canonical],
                ):
                    _row_size(
                        row,
                        min(self._limits["input"], self._limits["scratch_record"]),
                    )
                    image_builder.add(
                        {
                            "raw_revision_id": row["lineage_raw_revision_id"],
                            "canonical_revision_id": row["revision_id"],
                        }
                    )
                raw_root, image_root = raw_builder.finish(), image_builder.finish()
                missing = 0
                for raw, image in _merge_join(self._scratch, raw_root, image_root):
                    if image is None:
                        missing += 1
                        missing_units.add({"unit": raw["unit"]})
                unit_root = missing_units.finish()
            if missing:
                assert unit_root is not None
                normalized_unit = False
                with iter_run(self._scratch, unit_root) as units:
                    previous_unit: str | None = None
                    for unit_row in units:
                        unit = unit_row["unit"]
                        if unit == previous_unit:
                            continue
                        previous_unit = unit
                        prefix = f"{rules.NORMALIZER_ID}@{rules.NORMALIZER_VERSION}.{unit}."
                        if any(
                            snapshot.batch_id is not None and snapshot.batch_id.startswith(prefix)
                            for snapshot in history_from(
                                self._adapter, canonical, bindings[canonical]
                            )
                        ):
                            normalized_unit = True
                            break
                if normalized_unit:
                    raise CatalogIntegrityError(
                        f"{missing} Raw revision(s) from {raw_table} belong to normalized units "
                        f"but have no {canonical} image"
                    )
                raise RawNotDerived(
                    f"{missing} Raw revision(s) from {raw_table} have no {canonical} image"
                )

    def _check_rest_pages(
        self, data_type: str, symbol: str, day: date, bindings: Mapping[str, str]
    ) -> None:
        response_table = BINANCE_SPOT_REST_RESPONSES.table
        snapshot = bindings.get(response_table)
        if snapshot is None:
            return
        [channel] = [
            rules.raw_channel_of(table)
            for table in _RAW_TABLES[data_type]
            if rules.raw_channel_of(table).name != "archive"
        ]
        start = datetime.combine(day, time(), tzinfo=UTC)
        end = start + _DAY
        predicate = And(
            And(
                EqualTo("data_type", data_type),  # type: ignore[call-arg, arg-type]
                EqualTo("symbol", symbol),  # type: ignore[call-arg, arg-type]
            ),
            And(
                And(
                    EqualTo("decode_outcome", ACCEPTED),  # type: ignore[call-arg, arg-type]
                    GreaterThanOrEqual("element_count", 1),  # type: ignore[call-arg, arg-type]
                ),
                And(
                    LessThan("answered_start", end),  # type: ignore[call-arg, arg-type]
                    GreaterThanOrEqual("answered_end", start),  # type: ignore[call-arg, arg-type]
                ),
            ),
        )
        view = PinnedCatalogView(self._adapter, bindings)
        verifier = PersistedRowVerifier(view, self._storage)
        for page in _scan_rows(
            self._adapter,
            response_table,
            ("revision_id", "element_count"),
            row_filter=predicate,
            snapshot_id=snapshot,
        ):
            revision, count = page["revision_id"], page["element_count"]
            valid_count = isinstance(count, int) and not isinstance(count, bool)
            if not valid_count or not 1 <= count <= PAGE_LIMIT:
                raise CatalogIntegrityError(
                    f"REST page {revision} has invalid element_count {count!r}; "
                    f"expected an integer in [1, {PAGE_LIMIT}]"
                )
            own: set[int] = set()
            element_snapshot = bindings.get(channel.element.table)
            if element_snapshot is not None:
                element_filter = EqualTo(channel.lineage_column, revision)  # type: ignore[call-arg, arg-type]
                for row in _scan_rows(
                    self._adapter,
                    channel.element.table,
                    ("element_index",),
                    row_filter=element_filter,
                    snapshot_id=element_snapshot,
                ):
                    index = row["element_index"]
                    if index in own:
                        raise CatalogIntegrityError(
                            f"REST page {revision} duplicates element index {index}"
                        )
                    own.add(index)
                    if len(own) > count:
                        raise CatalogIntegrityError(
                            f"REST page {revision} has more element indexes than element_count"
                        )
            check_rest_page(view, verifier, channel, revision, own)

    def _canonical_runs(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        bindings: Mapping[str, str],
        *,
        pit_key_root: RunRef | None,
        gap_builder: RunSetBuilder,
    ) -> tuple[RunRef | None, RunRef | None, RunRef | None, RunRef | None, datetime | None]:
        """Stage the complete, PIT-proven day by revision, event time, and trade ID.

        ``iter_bounded`` is exhausted before this scan and verifies complete target-key history
        under these exact pinned bindings. Its streamed key set is compared with the keys in this
        canonical symbol/day predicate; every scanned day row therefore belongs to a key whose
        complete history passed the bounded selector. The v2 selection record surface includes
        all row revisions, not only final selected heads, so v3 projects all day rows in this
        proved subset.
        """
        canonical = rules.CANONICAL_TABLES[data_type].table
        columns: Sequence[str]
        if data_type == "klines_1m":
            columns = (
                "revision_id",
                "observation_key",
                "interval_start",
                "interval_end",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quote_volume",
                "taker_buy_base_volume",
                "taker_buy_quote_volume",
                "availability_evidence_gap",
                "knowledge_time",
            )
            time_column = "interval_start"
        else:
            columns = (
                "revision_id",
                "observation_key",
                "event_time",
                "venue_trade_id",
                "availability_evidence_gap",
                "knowledge_time",
            )
            time_column = "event_time"
        revision_builder = RunSetBuilder(
            self._scratch,
            key=lambda row: row["revision_id"],
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        canonical_key_builder = RunSetBuilder(
            self._scratch,
            key=lambda row: row["observation_key"],
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        time_builder = RunSetBuilder(
            self._scratch,
            key=lambda row: (row[time_column], row["revision_id"]),
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        trade_builder = (
            RunSetBuilder(
                self._scratch,
                key=lambda row: (row["venue_trade_id"], row["revision_id"]),
                capacity=self._capacity,
                merge_fanout=self._fanout,
                limits=self._run_limits,
            )
            if data_type == "agg_trades"
            else None
        )
        with revision_builder, canonical_key_builder, time_builder:
            if trade_builder is not None:
                trade_builder.__enter__()
            try:
                for row in _scan_rows(
                    self._adapter,
                    canonical,
                    columns,
                    row_filter=_filter_day(data_type, symbol, start, end, canonical=True),
                    snapshot_id=bindings[canonical],
                ):
                    _row_size(
                        row,
                        min(self._limits["input"], self._limits["scratch_record"]),
                    )
                    revision_builder.add(row)
                    canonical_key_builder.add({"observation_key": row["observation_key"]})
                revision_root = revision_builder.finish()
                canonical_key_root = canonical_key_builder.finish()
                floor: datetime | None = None
                if canonical_key_root is not None or pit_key_root is not None:
                    if canonical_key_root is None or pit_key_root is None:
                        raise CatalogIntegrityError(
                            "PIT key coverage differs from the pinned Canonical day rows"
                        )
                    with (
                        iter_run(self._scratch, canonical_key_root) as canonical_keys,
                        iter_run(self._scratch, pit_key_root) as pit_keys,
                    ):
                        canonical_key = next(canonical_keys, None)
                        pit_key = next(pit_keys, None)
                        previous_canonical: str | None = None
                        previous_pit: str | None = None
                        while True:
                            # Skip repeats on both sides before comparing; the markers move only
                            # after a compared pair, so one side's repeat never hides the other's
                            # next distinct key.
                            while (
                                canonical_key is not None
                                and canonical_key["observation_key"] == previous_canonical
                            ):
                                canonical_key = next(canonical_keys, None)
                            while (
                                pit_key is not None and pit_key["observation_key"] == previous_pit
                            ):
                                pit_key = next(pit_keys, None)
                            if canonical_key is None and pit_key is None:
                                break
                            canonical_value = (
                                None if canonical_key is None else canonical_key["observation_key"]
                            )
                            pit_value = None if pit_key is None else pit_key["observation_key"]
                            if canonical_value != pit_value:
                                raise CatalogIntegrityError(
                                    "PIT key coverage differs from the pinned Canonical day rows"
                                )
                            previous_canonical, previous_pit = canonical_value, pit_value
                            canonical_key = next(canonical_keys, None)
                            pit_key = next(pit_keys, None)
                if revision_root is not None:
                    previous_revision: str | None = None
                    with iter_run(self._scratch, revision_root) as canonical_rows:
                        for canonical_row in canonical_rows:
                            revision = canonical_row["revision_id"]
                            if revision == previous_revision:
                                raise CatalogIntegrityError(
                                    f"Canonical revision {revision} occurs more than once"
                                )
                            previous_revision = revision
                            floor = (
                                canonical_row["knowledge_time"]
                                if floor is None
                                else max(floor, canonical_row["knowledge_time"])
                            )
                            time_builder.add(canonical_row)
                            if trade_builder is not None:
                                trade_builder.add(
                                    {
                                        "venue_trade_id": int(canonical_row["venue_trade_id"]),
                                        "revision_id": revision,
                                    }
                                )
                            if canonical_row["availability_evidence_gap"] is not None:
                                gap_builder.add(
                                    {
                                        "table": canonical,
                                        "revision_id": revision,
                                        "gap": canonical_row["availability_evidence_gap"],
                                    }
                                )
                time_root = time_builder.finish()
                trade_root = trade_builder.finish() if trade_builder is not None else None
                return revision_root, time_root, trade_root, canonical_key_root, floor
            finally:
                if trade_builder is not None:
                    trade_builder.close()

    def _edge_floor(
        self,
        bindings: Mapping[str, str],
        canonical_keys: RunRef | None,
    ) -> datetime | None:
        """Return the latest knowledge time of precedence evidence for this day's keys."""
        table = BINANCE_SPOT_PRECEDENCE_EVIDENCE.table
        snapshot = bindings.get(table)
        if snapshot is None or canonical_keys is None:
            return None
        builder = RunSetBuilder(
            self._scratch,
            key=lambda row: (row["observation_key"], row["knowledge_time"]),
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        with builder:
            for row in _scan_rows(
                self._adapter,
                table,
                ("observation_key", "knowledge_time"),
                row_filter=AlwaysTrue(),
                snapshot_id=snapshot,
            ):
                _row_size(
                    row,
                    min(self._limits["input"], self._limits["scratch_record"]),
                )
                builder.add(row)
            edge_root = builder.finish()
        if edge_root is None:
            return None
        latest: datetime | None = None
        with (
            iter_run(self._scratch, canonical_keys) as keys,
            iter_run(self._scratch, edge_root) as edges,
        ):
            key = next(keys, None)
            edge = next(edges, None)
            previous_key: str | None = None
            while key is not None:
                key_value = key["observation_key"]
                if key_value == previous_key:
                    key = next(keys, None)
                    continue
                previous_key = key_value
                while edge is not None and edge["observation_key"] < key_value:
                    edge = next(edges, None)
                while edge is not None and edge["observation_key"] == key_value:
                    value = edge["knowledge_time"]
                    latest = value if latest is None else max(latest, value)
                    edge = next(edges, None)
                key = next(keys, None)
        return latest

    def _derive_streams(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        bindings: Mapping[str, str],
        report_id: str,
        *,
        existing_only: bool,
    ) -> tuple[tuple[Any, Any, Any], tuple[int, int, int], datetime | None]:
        self._check_raw_derived(data_type, symbol, start, end, bindings)
        self._check_rest_pages(data_type, symbol, start.date(), bindings)
        target = self._scratch if existing_only else self._storage
        events = QualityReportStreamWriter(target, "events", limits=self._stream_limits)
        revisions = QualityReportStreamWriter(target, "event_revisions", limits=self._stream_limits)
        gaps = QualityReportStreamWriter(target, "evidence_gaps", limits=self._stream_limits)
        event_projector = CanonicalPartitionV3Projector(
            self._scratch,
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
            max_event_record_bytes=self._limits["event"],
            max_revision_record_bytes=self._limits["revision"],
        )
        gap_projector = CanonicalPartitionV3EvidenceGapProjector(
            self._scratch,
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
            max_gap_record_bytes=self._limits["gap"],
        )
        conflict_builder = RunSetBuilder(
            self._scratch,
            key=lambda row: (row["observation_key"], row["revision_id"]),
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        pit_key_builder = RunSetBuilder(
            self._scratch,
            key=lambda row: row["observation_key"],
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        gap_builder = RunSetBuilder(
            self._scratch,
            key=lambda row: (row["table"], row["revision_id"]),
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        floor: datetime | None = None
        try:
            with conflict_builder, pit_key_builder, gap_builder:

                def save_conflict(item: PitConflictHeadEvidence) -> None:
                    row = {
                        "observation_key": item.observation_key,
                        "revision_id": item.revision_id,
                    }
                    _row_size(row, self._limits["scratch_record"])
                    conflict_builder.add(row)

                spec = PointInTimeSpec(
                    name="hlens.quality.canonical-partition-v3",
                    version="1.0.0",
                    simulation_time=datetime.max.replace(tzinfo=UTC),
                    knowledge_cutoff=datetime.max.replace(tzinfo=UTC),
                    snapshot_bindings=FrozenMapping(bindings),
                    point_in_time_binding=PIT_BINDING,
                    availability_bindings=REQUIRED_BINDINGS["availability_bindings"],
                    precedence_bindings=(DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
                    parser_bindings=REQUIRED_BINDINGS["parser_bindings"],
                )
                with self._selector.iter_bounded(
                    spec,
                    data_type,
                    symbol,
                    start,
                    end,
                    params=self._pit_params,
                    touching=True,
                    conflict_sink=save_conflict,
                ) as records:
                    for record in records:
                        key_row = {"observation_key": record.observation_key}
                        _row_size(key_row, self._limits["scratch_record"])
                        pit_key_builder.add(key_row)
                conflict_root = conflict_builder.finish()
                pit_key_root = pit_key_builder.finish()
                canonical_root, time_root, trade_root, canonical_key_root, floor = (
                    self._canonical_runs(
                        data_type,
                        symbol,
                        start,
                        end,
                        bindings,
                        pit_key_root=pit_key_root,
                        gap_builder=gap_builder,
                    )
                )
                edge_floor = self._edge_floor(bindings, canonical_key_root)
                if edge_floor is not None:
                    floor = edge_floor if floor is None else max(floor, edge_floor)
                gap_root = gap_builder.finish()
            # Inputs event is fixed-size; bindings are also committed in the manifest identity.
            with event_projector.project_event(
                event_type="report_inputs",
                table=None,
                observation_key=None,
                revision_ids=(),
                event_start=start,
                event_end=end,
                detail=canonical_json(dict(sorted(bindings.items()))),
            ) as projected:
                events.append(projected.event_record)
                for revision_record in projected.revision_records:
                    revisions.append(revision_record)
            # Conflict heads are grouped from the external sort; each event's revision iterator is
            # streamed directly into event_revisions and is never collected as a Python tuple.
            if conflict_root is not None:
                with iter_run(self._scratch, conflict_root) as rows:
                    for key, group in groupby(rows, key=lambda row: row["observation_key"]):

                        def head_ids(items: Iterator[Mapping[str, Any]] = group) -> Iterator[str]:
                            for item in items:
                                yield item["revision_id"]

                        with event_projector.project_event(
                            event_type="competing_heads",
                            table=rules.CANONICAL_TABLES[data_type].table,
                            observation_key=key,
                            revision_ids=head_ids(),
                            event_start=None,
                            event_end=None,
                            detail="multiple maximal heads under the bound snapshots",
                        ) as projected:
                            events.append(projected.event_record)
                            for revision_record in projected.revision_records:
                                revisions.append(revision_record)

            canonical = rules.CANONICAL_TABLES[data_type].table
            if data_type == "klines_1m":
                if time_root is not None:
                    minute = start
                    gap_start: datetime | None = None
                    with iter_run(self._scratch, time_root) as ordered_rows:
                        for row in ordered_rows:
                            present = row["interval_start"]
                            while minute < present:
                                gap_start = minute if gap_start is None else gap_start
                                minute += timedelta(minutes=1)
                            if minute == present:
                                if gap_start is not None:
                                    with event_projector.project_event(
                                        event_type="bar_1m_gap",
                                        table=canonical,
                                        observation_key=None,
                                        revision_ids=(),
                                        event_start=gap_start,
                                        event_end=present,
                                        detail=(
                                            f"{int((present - gap_start).total_seconds() // 60)} "
                                            "minute(s) without any Canonical bar revision"
                                        ),
                                    ) as projected:
                                        events.append(projected.event_record)
                                    gap_start = None
                                minute = present + timedelta(minutes=1)
                    if minute < end:
                        gap_start = minute if gap_start is None else gap_start
                    if gap_start is not None:
                        with event_projector.project_event(
                            event_type="bar_1m_gap",
                            table=canonical,
                            observation_key=None,
                            revision_ids=(),
                            event_start=gap_start,
                            event_end=end,
                            detail=f"{int((end - gap_start).total_seconds() // 60)} minute(s) "
                            "without any Canonical bar revision",
                        ) as projected:
                            events.append(projected.event_record)
                elif data_type == "klines_1m":
                    with event_projector.project_event(
                        event_type="bar_1m_gap",
                        table=canonical,
                        observation_key=None,
                        revision_ids=(),
                        event_start=start,
                        event_end=end,
                        detail="1440 minute(s) without any Canonical bar revision",
                    ) as projected:
                        events.append(projected.event_record)
                if canonical_root is not None:
                    with iter_run(self._scratch, canonical_root) as canonical_rows:
                        for row in canonical_rows:
                            problems: list[str] = []
                            if row["low"] > row["high"]:
                                problems.append("low > high")
                            if not row["low"] <= min(row["open"], row["close"]):
                                problems.append("low above open/close")
                            if not max(row["open"], row["close"]) <= row["high"]:
                                problems.append("high below open/close")
                            if row["taker_buy_base_volume"] > row["volume"]:
                                problems.append("taker buy base volume > volume")
                            if row["taker_buy_quote_volume"] > row["quote_volume"]:
                                problems.append("taker buy quote volume > quote volume")
                            if problems:
                                with event_projector.project_event(
                                    event_type="bar_1m_invariant_violation",
                                    table=canonical,
                                    observation_key=row["observation_key"],
                                    revision_ids=(row["revision_id"],),
                                    event_start=row["interval_start"],
                                    event_end=row["interval_end"],
                                    detail="; ".join(problems),
                                ) as projected:
                                    events.append(projected.event_record)
                                    for revision_record in projected.revision_records:
                                        revisions.append(revision_record)
            elif trade_root is not None:
                with iter_run(self._scratch, trade_root) as trade_rows:
                    previous_id: int | None = None
                    for row in trade_rows:
                        current_id = row["venue_trade_id"]
                        if current_id == previous_id:
                            continue
                        if previous_id is not None and current_id != previous_id + 1:
                            with event_projector.project_event(
                                event_type="agg_trade_id_discontinuity",
                                table=canonical,
                                observation_key=None,
                                revision_ids=(),
                                event_start=None,
                                event_end=None,
                                detail=f"aggregate trade ids jump from {previous_id} to "
                                f"{current_id} ({current_id - previous_id - 1} id(s) absent)",
                            ) as projected:
                                events.append(projected.event_record)
                        previous_id = current_id

            def gap_items() -> Iterator[tuple[str, str, str]]:
                if gap_root is None:
                    return
                with iter_run(self._scratch, gap_root) as gap_rows:
                    for row in gap_rows:
                        yield row["table"], row["revision_id"], row["gap"]

            with gap_projector.project_gaps(
                quality_report_id=report_id, gaps=gap_items()
            ) as projected_gaps:
                gap_count = projected_gaps.record_count
                with event_projector.project_event(
                    event_type="evidence_gaps",
                    table="quality.availability_evidence_gaps",
                    observation_key=None,
                    revision_ids=(),
                    event_start=start,
                    event_end=end,
                    detail=f"{gap_count} evidence gap record(s) in the v3 evidence_gaps stream",
                ) as projected:
                    events.append(projected.event_record)
                for gap_row in projected_gaps.records:
                    gaps.append(gap_row)
            refs = (events.finish(), revisions.finish(), gaps.finish())
            return refs, (refs[0].record_count, refs[1].record_count, refs[2].record_count), floor
        except BaseException:
            events.close()
            revisions.close()
            gaps.close()
            raise
