"""Per-partition Canonical quality reports (Phase 1 E3; roadmap #16; ADR-0023 §2, ADR-0028 §7).

``QualityReporter.report(data_type, symbol, day)`` writes one row of
``quality.data_quality_reports`` for one Canonical partition (table × venue symbol × UTC day)
under the rule set ``hlens.quality.canonical-partition@1.0.0``:

1. **inputs** — a pinned read of the current heads of every table the partition's truth depends
   on (the Canonical table, both Raw element tables and their source tables, the Raw evidence
   table); the report is then computed on a ``PinnedCatalogView`` of exactly those snapshots, so it
   is reproducible from its own ``report_inputs`` event;
2. **proof** — everything is read through ``PitSelector`` (Canonical rows re-normalized, Raw edges
   re-derived); a partition that does not prove fails closed with no report;
3. **events** (deterministic ids; no numeric threshold anywhere — outlier rules need calibrated
   thresholds and are left to a later rule version):
   - ``report_inputs`` — the bound snapshots, as canonical JSON;
   - ``competing_heads`` — keys with more than one maximal head with everything the snapshots know;
   - ``bar_1m_gap`` — maximal runs of minutes of the day with no Canonical bar revision at all;
   - ``agg_trade_id_discontinuity`` — jumps between consecutive aggregate trade ids of the day
     (a fact: Binance does not promise contiguous ids);
   - ``bar_1m_invariant_violation`` — a revision whose ``low``/``high`` do not bound
     ``open``/``close``, or whose taker volumes exceed the totals;
4. **evidence gaps** — every Canonical revision of the partition with an availability evidence
   gap, as ``AvailabilityEvidenceGap`` rows (``quality_report_id`` = this report);
5. **idempotency** — ``report_id`` is derived from the rule set, partition and bound snapshots.
   A committed report with that id is re-derived and reused (its first ``knowledge_time``); only
   a missing report reads the injected clock (after every proof, before the commit).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual, LessThan

from core.contracts.catalog import BatchConflict, CommitConflict, CommitRequest, TableNotFound
from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import StorageAdapter
from core.domain.base import FrozenMapping, canonical_json
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError, PyIcebergCatalogAdapter
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
    DATA_QUALITY_REPORTS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.pit.selector import PIT_BINDING, REQUIRED_BINDINGS, PitSelection, PitSelector
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.row_integrity import batch, check_batch_snapshot, history_from
from infrastructure.revision.store import BatchCommit

__all__ = [
    "QUALITY_RULE_HASH",
    "QUALITY_RULE_ID",
    "QUALITY_RULE_SPEC",
    "QUALITY_RULE_VERSION",
    "QualityReportError",
    "QualityReported",
    "QualityReporter",
    "evidence_gaps_of",
    "quality_report_id",
]

QUALITY_RULE_ID: Final = "hlens.quality.canonical-partition"
QUALITY_RULE_VERSION: Final = "2.0.0"
QUALITY_RULE_SPEC: Final[dict[str, Any]] = {
    "rule": QUALITY_RULE_ID,
    "version": QUALITY_RULE_VERSION,
    "subject": "one Canonical partition: table x venue symbol x UTC day",
    "inputs": "current heads of the Canonical table, both Raw element tables, their source tables "
    "and the Raw evidence table, pinned; the report is computed on exactly those snapshots",
    "proof": "hlens.pit.maximal-head@1.0.0 selector proofs (Canonical re-normalized, edges "
    "re-derived); an unprovable partition gets no report",
    "events": {
        "report_inputs": "the bound snapshots (canonical JSON)",
        "competing_heads": "key with >1 maximal head, knowledge_cutoff and simulation at the end "
        "of time",
        "bar_1m_gap": "maximal run of minutes of the day without any Canonical bar revision",
        "agg_trade_id_discontinuity": "jump between consecutive aggregate trade ids of the day",
        "bar_1m_invariant_violation": "low/high do not bound open/close, low > high, or a taker "
        "volume exceeds its total",
        "evidence_gaps": "number of evidence-gap rows and batches of this report (ADR-0031)",
    },
    "thresholds": "none (outliers need calibrated thresholds: a later rule version)",
    "evidence_gaps": "every Canonical revision of the partition with an evidence gap, as rows of "
    "quality.availability_evidence_gaps in batches <report_id>.gaps.<index:08d> (<= 25000 rows, "
    "sorted by revision_id, in slice order) committed before the report row; the report row "
    "lists none and carries an evidence_gaps event with the counts (ADR-0031)",
    "report_id": "<rule>@<version>.<table>.<venue symbol>.<day>.<sha256 of the rule hashes used "
    "(this set, the PIT rule, its required policies) and the bound snapshots>",
    "knowledge_time": "first commit's clock reading, reused by every replay",
}
QUALITY_RULE_HASH: Final = hashlib.sha256(
    canonical_json(QUALITY_RULE_SPEC).encode("utf-8")
).hexdigest()

_FAR: Final = datetime(9999, 12, 31, tzinfo=UTC)
_DAY: Final = timedelta(days=1)
_MINUTE: Final = timedelta(minutes=1)
_ATTEMPTS: Final = 8
_ZERO: Final = timedelta(0)
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


#: Selection slice per data type (G3-S3): a trade day is proven an hour at a time; a bar day
#: (1 440 rows) at once. Keys never straddle slices, so the report is the whole-day report.
_SLICES: Final[Mapping[str, timedelta]] = {
    "agg_trades": timedelta(hours=1),
    "klines_1m": _DAY,
}


@dataclass
class _Partition:
    """What the report needs from the proven partition, gathered slice by slice."""

    floor: datetime | None = None
    conflicts: dict[str, set[str]] = field(default_factory=dict)
    bars: list[Mapping[str, Any]] = field(default_factory=list)
    trade_ids: list[int] = field(default_factory=list)
    gap_rows: int = 0
    gap_batches: int = 0

    def absorb(self, selection: PitSelection) -> None:
        times = [
            *(r.availability.times.knowledge_time for rs in selection.records.values() for r in rs),
            *(e.knowledge_time for es in selection.edges.values() for e in es),
        ]
        if times:
            latest = max(times)
            self.floor = latest if self.floor is None else max(self.floor, latest)
        for key in selection.conflicts:
            self.conflicts.setdefault(key, set()).update(
                head
                for item in selection.selections
                if item.observation_key == key
                for head in item.maximal_heads
            )


def quality_report_id(canonical: str, symbol: str, day: date, bindings: Mapping[str, str]) -> str:
    """The report's identity: rule set, partition, bound snapshots and every rule it relies on.

    The PIT rule and the policies its selections apply are hashed in too (review H-3): a change
    to any of them is a new report, never a committed report that no longer re-derives.
    """
    rules_used = {
        "quality": QUALITY_RULE_HASH,
        "pit": PIT_BINDING.policy_hash,
        **{
            f"{binding.policy_id}@{binding.version}": binding.policy_hash
            for bound in REQUIRED_BINDINGS.values()
            for binding in bound
        },
    }
    return (
        f"{QUALITY_RULE_ID}@{QUALITY_RULE_VERSION}.{canonical}.{symbol}.{day.isoformat()}."
        f"{_digest({'rules': rules_used, 'bindings': dict(bindings)})}"
    )


class QualityReportError(Exception):
    """A report cannot be produced honestly (scope, clock, contention); nothing is committed."""


@dataclass(frozen=True, slots=True)
class QualityReported:
    report_id: str
    row: Mapping[str, Any]
    commit: BatchCommit | None
    reused: bool


def _digest(document: Any) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _event(
    event_type: str,
    *,
    table: str | None,
    key: str | None,
    revisions: Iterable[str],
    start: datetime | None,
    end: datetime | None,
    detail: str,
) -> dict[str, Any]:
    revision_ids = sorted(set(revisions))
    identity = {
        "rule": f"{QUALITY_RULE_ID}@{QUALITY_RULE_VERSION}",
        "type": event_type,
        "table": table,
        "key": key,
        "revisions": revision_ids,
        "start": None if start is None else start.isoformat(),
        "end": None if end is None else end.isoformat(),
        "detail": detail,
    }
    return {
        "event_id": f"{event_type}.{_digest(identity)[:32]}",
        "event_type": event_type,
        "table": table,
        "observation_key": key,
        "revision_ids": revision_ids,
        "event_start": start,
        "event_end": end,
        "detail": detail,
    }


class QualityReporter:
    """Writes deterministic, reproducible partition quality reports; one writer per table."""

    def __init__(
        self,
        adapter: PyIcebergCatalogAdapter,
        storage: StorageAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._selector = PitSelector(adapter, storage)

    def report(self, data_type: str, symbol: str, day: date) -> QualityReported:
        tables = _INPUT_TABLES.get(data_type)
        if tables is None:
            raise QualityReportError(f"unsupported data_type {data_type!r}")
        if symbol not in rules.SYMBOLS:
            raise QualityReportError(f"{symbol!r} is not a first-slice venue symbol")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise QualityReportError("day must be a datetime.date (UTC)")
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            bindings = self._pinned_heads(tables)
            canonical = rules.CANONICAL_TABLES[data_type].table
            if canonical not in bindings:
                raise QualityReportError(f"{canonical} has no snapshot: nothing to report on")
            report_id = quality_report_id(canonical, symbol, day, bindings)
            committed = self._committed(report_id)
            # A committed report is only ever verified: its gap batches are checked, never written.
            partition = self._survey(
                data_type, symbol, day, bindings, report_id, verify=committed is not None
            )
            body = self._body(data_type, symbol, day, bindings, report_id, partition)
            # Every fact the report describes — revisions and the mapped precedence edges that
            # decide its heads — must be known by its knowledge_time (E1-R3 / review C-2), for a
            # fresh report and for a committed one it would reuse alike (E1-R4).
            floor = partition.floor
            if committed is not None:
                if floor is not None and committed["knowledge_time"] < floor:
                    raise CatalogIntegrityError(
                        f"quality report {report_id} is committed with a knowledge_time before a "
                        "revision or precedence edge it describes"
                    )
                expected = self._row(body, committed["knowledge_time"])
                mismatched = sorted(k for k, v in expected.items() if committed[k] != v)
                if mismatched:
                    raise CatalogIntegrityError(
                        f"quality report {report_id} disagrees with its re-derivation: {mismatched}"
                    )
                return QualityReported(report_id, committed, None, True)
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _ZERO:
                raise QualityReportError("the clock must return timezone-aware UTC")
            if floor is not None and now < floor:
                raise QualityReportError(
                    "the report clock precedes a revision or precedence edge it describes"
                )
            row = self._row(body, now)
            try:
                commit = self._commit(report_id, row)
            except (CommitConflict, BatchConflict) as exc:
                last = exc
                continue
            return QualityReported(report_id, row, commit, False)
        raise QualityReportError(f"report of {data_type} {symbol} {day} lost races") from last

    # ------------------------------------------------------------------ inputs and proof

    def _pinned_heads(self, tables: Sequence[str]) -> dict[str, str]:
        for _ in range(_ATTEMPTS):
            first = {table: self._head(table) for table in tables}
            second = {table: self._head(table) for table in tables}
            if first == second:
                return {table: head for table, head in sorted(first.items()) if head is not None}
        raise QualityReportError("the input tables kept moving")

    def _survey(
        self,
        data_type: str,
        symbol: str,
        day: date,
        bindings: Mapping[str, str],
        report_id: str,
        *,
        verify: bool,
    ) -> _Partition:
        """The proven partition, selected slice by slice (G3-S3); gaps go to their own table.

        Each slice's evidence gaps are written (or, for a committed report, verified) as batches
        of ``quality.availability_evidence_gaps`` as soon as the slice is proven (ADR-0031), so
        nothing but counts is kept across slices.
        """
        partition = _Partition()
        gaps = _GapWriter(self._adapter, report_id, symbol, day, verify=verify)
        # One selector for every slice: it proves each unit and day once under these bindings.
        self._selector = PitSelector(self._adapter, self._storage)
        canonical = rules.CANONICAL_TABLES[data_type].table
        start = datetime.combine(day, time(), tzinfo=UTC)
        step = _SLICES[data_type]
        for begin in self._occupied(data_type, symbol, bindings, start, step):
            end = min(begin + step, start + _DAY)
            selection = self._select(data_type, symbol, begin, end, bindings)
            partition.absorb(selection)
            rows = self._rows(canonical, bindings, selection, symbol, begin, end)
            if data_type == "klines_1m":
                partition.bars.extend(rows)
            else:
                partition.trade_ids.extend(int(row["venue_trade_id"]) for row in rows)
            gaps.add(
                canonical,
                [
                    (row["revision_id"], row["availability_evidence_gap"])
                    for row in rows
                    if row["availability_evidence_gap"] is not None
                ],
            )
        partition.gap_rows, partition.gap_batches = gaps.close()
        return partition

    def _occupied(
        self,
        data_type: str,
        symbol: str,
        bindings: Mapping[str, str],
        start: datetime,
        step: timedelta,
    ) -> list[datetime]:
        """Slice starts of the day holding at least one Canonical row (one narrow scan).

        A slice without rows has no key, so nothing of it can enter the report; skipping it
        leaves the report unchanged.
        """
        canonical = rules.CANONICAL_TABLES[data_type].table
        column = _time_column(data_type)
        times = self._adapter.scan_columns(
            canonical,
            columns=(column,),
            row_filter=_slice_filter(symbol, column, start, start + _DAY),
            snapshot_id=bindings[canonical],
        ).column(column)
        return sorted({start + ((value - start) // step) * step for value in times.to_pylist()})

    def _select(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        bindings: Mapping[str, str],
    ) -> PitSelection:
        spec = PointInTimeSpec(
            name="hlens.quality.partition-view",
            version="1.0.0",
            simulation_time=_FAR,
            knowledge_cutoff=_FAR,
            snapshot_bindings=FrozenMapping(bindings),
            point_in_time_binding=PIT_BINDING,
            availability_bindings=REQUIRED_BINDINGS["availability_bindings"],
            precedence_bindings=(DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
            parser_bindings=REQUIRED_BINDINGS["parser_bindings"],
        )
        # Every key touching the slice: a straddling key is evaluated in each slice it touches
        # and de-duplicated here (conflicts by key, rows by the slice that holds their event).
        return self._selector.select(spec, data_type, symbol, start, end, touching=True)

    # ------------------------------------------------------------------ the report

    def _body(
        self,
        data_type: str,
        symbol: str,
        day: date,
        bindings: Mapping[str, str],
        report_id: str,
        partition: _Partition,
    ) -> dict[str, Any]:
        canonical = rules.CANONICAL_TABLES[data_type].table
        start = datetime.combine(day, time(), tzinfo=UTC)
        events = [
            _event(
                "report_inputs",
                table=None,
                key=None,
                revisions=(),
                start=start,
                end=start + _DAY,
                detail=canonical_json(dict(bindings)),
            )
        ]
        for key, heads in sorted(partition.conflicts.items()):
            events.append(
                _event(
                    "competing_heads",
                    table=canonical,
                    key=key,
                    revisions=heads,
                    start=None,
                    end=None,
                    detail=f"{len(heads)} maximal heads with everything the bound snapshots know",
                )
            )
        if data_type == "klines_1m":
            events.extend(_bar_events(canonical, partition.bars, start))
        else:
            events.extend(_trade_events(canonical, partition.trade_ids))
        events.append(
            _event(
                "evidence_gaps",
                table=QUALITY_EVIDENCE_GAPS.table,
                key=None,
                revisions=(),
                start=start,
                end=start + _DAY,
                detail=f"{partition.gap_rows} evidence gap(s) in {partition.gap_batches} "
                f"batch(es) {report_id}.gaps.* of {QUALITY_EVIDENCE_GAPS.table}",
            )
        )
        return {
            "report_id": report_id,
            "quality_rule_id": QUALITY_RULE_ID,
            "quality_rule_version": QUALITY_RULE_VERSION,
            "quality_rule_hash": QUALITY_RULE_HASH,
            "subject_table": canonical,
            "subject_snapshot_id": bindings[canonical],
            "subject_symbol": symbol,
            "subject_start": start,
            "subject_end": start + _DAY,
            "events": events,
            "evidence_gaps": [],
        }

    def _rows(
        self,
        canonical: str,
        bindings: Mapping[str, str],
        selection: PitSelection,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[Mapping[str, Any]]:
        """The slice's proven Canonical rows: one scan of the slice, exactly the proven set."""
        # The selection also evaluates revisions of its keys outside the slice (key closure);
        # the slice's own rows are those whose event lies in it.
        wanted = {
            r.revision_id
            for rs in selection.records.values()
            for r in rs
            if start <= r.availability.times.event_time < end
        }
        table = next(t for t in rules.CANONICAL_TABLES.values() if t.table == canonical)
        data_type = next(k for k, t in rules.CANONICAL_TABLES.items() if t.table == canonical)
        found: list[Mapping[str, Any]] = self._adapter.scan_columns(
            canonical,
            columns=tuple(field.name for field in table.arrow_schema),
            row_filter=_slice_filter(symbol, _time_column(data_type), start, end),
            snapshot_id=bindings[canonical],
        ).to_pylist()
        if {row["revision_id"] for row in found} != wanted or len(found) != len(wanted):
            raise CatalogIntegrityError(f"{canonical} rows do not match the proven revisions")
        return found

    @staticmethod
    def _row(body: Mapping[str, Any], knowledge_time: datetime) -> dict[str, Any]:
        row = dict(body, knowledge_time=knowledge_time)
        normalised: list[dict[str, Any]] = pa.Table.from_pylist(
            [row], schema=DATA_QUALITY_REPORTS.arrow_schema
        ).to_pylist()
        return normalised[0]

    # ------------------------------------------------------------------ catalog

    def _committed(self, report_id: str) -> Mapping[str, Any] | None:
        columns = tuple(field.name for field in DATA_QUALITY_REPORTS.arrow_schema)
        rows = self._adapter.scan_columns(
            DATA_QUALITY_REPORTS.table,
            columns=columns,
            row_filter=EqualTo("report_id", report_id),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        if len(rows) > 1:
            raise CatalogIntegrityError(f"quality report {report_id} is committed twice")
        return rows[0] if rows else None

    def _commit(self, report_id: str, row: Mapping[str, Any]) -> BatchCommit:
        definition = DATA_QUALITY_REPORTS
        table = pa.Table.from_pylist([dict(row)], schema=definition.arrow_schema)
        request = CommitRequest(
            table=definition.table,
            batch_id=report_id,
            batch_fingerprint=definition.fingerprint_rule.fingerprint(table),
            row_count=1,
            expected_parent_snapshot_id=self._head(definition.table),
        )
        result = self._adapter.commit_batch(request, table)
        if self._committed(report_id) != table.to_pylist()[0]:
            raise CatalogIntegrityError(f"quality report {report_id} reads back differently")
        return BatchCommit(
            table=definition.table,
            batch_id=report_id,
            snapshot_id=result.snapshot.snapshot_id,
            outcome=result.outcome,
            row_count=1,
        )

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


_GAP_BATCH_ROWS: Final = 25_000
_DIGEST_MODULUS: Final = 1 << 256


def _gap_digest(row: Mapping[str, Any]) -> int:
    document = {
        "quality_report_id": row["quality_report_id"],
        "table": row["table"],
        "revision_id": row["revision_id"],
        "gap": row["gap"],
        "subject_symbol": row["subject_symbol"],
        "subject_start": row["subject_start"].isoformat(),
    }
    return int(_digest(document), 16)


_GAP_INDEX_DIGITS: Final = 8


class _GapWriter:
    """Streams a report's evidence gaps into ``quality.availability_evidence_gaps`` (ADR-0031).

    Batches ``<report_id>.gaps.<index:08d>`` of at most 25 000 rows, sorted by ``revision_id``
    within each slice, numbered in slice order. Writing replays idempotently (same id, same
    content); with ``verify`` nothing is written and every batch must already be committed with
    exactly these rows, and no further batch of the report may exist.
    """

    def __init__(
        self, adapter: Any, report_id: str, symbol: str, day: date, *, verify: bool
    ) -> None:
        self._adapter = adapter
        self._report_id = report_id
        self._symbol = symbol
        self._start = datetime.combine(day, time(), tzinfo=UTC)
        self._verify = verify
        self._rows = 0
        self._index = 0
        self._digest = 0
        self._committed: dict[str, list[Any]] | None = None

    def _batch_id(self, index: int) -> str:
        return f"{self._report_id}.gaps.{index:0{_GAP_INDEX_DIGITS}d}"

    def _history(self) -> dict[str, list[Any]]:
        if self._committed is None:
            found: dict[str, list[Any]] = {}
            prefix = f"{self._report_id}.gaps."
            info = self._adapter.load_table(QUALITY_EVIDENCE_GAPS.table)
            if info is None:
                raise TableNotFound(f"table {QUALITY_EVIDENCE_GAPS.table} does not exist")
            head = None if info.current_snapshot is None else info.current_snapshot.snapshot_id
            for snapshot in history_from(self._adapter, QUALITY_EVIDENCE_GAPS.table, head):
                if snapshot.batch_id is not None and snapshot.batch_id.startswith(prefix):
                    found.setdefault(snapshot.batch_id, []).append(snapshot)
            self._committed = found
        return self._committed

    def add(self, table: str, gaps: Sequence[tuple[str, str]]) -> None:
        ordered = sorted(gaps)
        for offset in range(0, len(ordered), _GAP_BATCH_ROWS):
            rows = [
                {
                    "quality_report_id": self._report_id,
                    "table": table,
                    "revision_id": revision,
                    "gap": gap,
                    "subject_symbol": self._symbol,
                    "subject_start": self._start,
                }
                for revision, gap in ordered[offset : offset + _GAP_BATCH_ROWS]
            ]
            self._put(self._batch_id(self._index), rows)
            self._index += 1
            self._rows += len(rows)
            for row in rows:
                self._digest = (self._digest + _gap_digest(row)) % _DIGEST_MODULUS

    def _put(self, batch_id: str, rows: list[dict[str, Any]]) -> None:
        definition = QUALITY_EVIDENCE_GAPS
        if self._verify:
            snapshots = self._history().get(batch_id, [])
            if len(snapshots) != 1:
                raise CatalogIntegrityError(
                    f"evidence-gap batch {batch_id} is committed {len(snapshots)} time(s)"
                )
            check_batch_snapshot(definition, batch_id, snapshots[0], rows)
            return
        table = batch(definition, rows)
        info = self._adapter.load_table(definition.table)
        parent = (
            None
            if info is None or info.current_snapshot is None
            else (info.current_snapshot.snapshot_id)
        )
        request = CommitRequest(
            table=definition.table,
            batch_id=batch_id,
            batch_fingerprint=definition.fingerprint_rule.fingerprint(table),
            row_count=len(rows),
            expected_parent_snapshot_id=parent,
        )
        try:
            self._adapter.commit_batch(request, table)
        except BatchConflict:
            raise CatalogIntegrityError(
                f"evidence-gap batch {batch_id} is committed with other content"
            ) from None

    def close(self) -> tuple[int, int]:
        if self._verify:
            expected = {self._batch_id(index) for index in range(self._index)}
            extra = sorted(set(self._history()) - expected)
            if extra:
                raise CatalogIntegrityError(
                    f"evidence-gap batches {extra[:3]} belong to no gap of report {self._report_id}"
                )
        # The rows the table holds for this report must be exactly the rows written: a batch
        # snapshot keeps its fingerprint when rows are deleted or added later, so the rows
        # themselves are counted and digested (an order-independent sum of row hashes).
        found = self._adapter.scan_columns(
            QUALITY_EVIDENCE_GAPS.table,
            columns=("table", "revision_id", "gap", "subject_symbol", "subject_start"),
            row_filter=EqualTo("quality_report_id", self._report_id),  # type: ignore[call-arg, arg-type]
        )
        digest = 0
        for row in found.to_pylist():
            row["quality_report_id"] = self._report_id
            digest = (digest + _gap_digest(row)) % _DIGEST_MODULUS
        if found.num_rows != self._rows or digest != self._digest:
            raise CatalogIntegrityError(
                f"{QUALITY_EVIDENCE_GAPS.table} holds {found.num_rows} gap row(s) of report "
                f"{self._report_id}, not exactly the {self._rows} it wrote"
            )
        return self._rows, self._index


def evidence_gaps_of(adapter: Any, report_id: str) -> list[dict[str, Any]]:
    """The evidence-gap rows of one report (ADR-0031), sorted by (table, revision_id).

    Rows of ``quality.availability_evidence_gaps`` exist without a report row only for a report
    that never completed; callers bind gaps through a committed report id.
    """
    columns = tuple(field.name for field in QUALITY_EVIDENCE_GAPS.arrow_schema)
    rows: list[dict[str, Any]] = adapter.scan_columns(
        QUALITY_EVIDENCE_GAPS.table,
        columns=columns,
        row_filter=EqualTo("quality_report_id", report_id),  # type: ignore[call-arg, arg-type]
    ).to_pylist()
    return sorted(rows, key=lambda row: (row["table"], row["revision_id"]))


def _time_column(data_type: str) -> str:
    return "event_time" if data_type == "agg_trades" else "interval_start"


def _slice_filter(symbol: str, column: str, start: datetime, end: datetime) -> Any:
    return And(
        EqualTo("symbol", rules.SYMBOLS[symbol].symbol),  # type: ignore[call-arg, arg-type]
        And(
            GreaterThanOrEqual(column, start),  # type: ignore[call-arg, arg-type]
            LessThan(column, end),  # type: ignore[call-arg, arg-type]
        ),
    )


def _bar_events(
    table: str, rows: Sequence[Mapping[str, Any]], day_start: datetime
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    present = {row["interval_start"] for row in rows}
    minute = day_start
    run_start: datetime | None = None
    while minute < day_start + _DAY:
        if minute not in present:
            run_start = minute if run_start is None else run_start
        elif run_start is not None:
            events.append(_gap(table, run_start, minute))
            run_start = None
        minute += _MINUTE
    if run_start is not None:
        events.append(_gap(table, run_start, day_start + _DAY))
    for row in sorted(rows, key=lambda item: item["revision_id"]):
        problems = []
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
            events.append(
                _event(
                    "bar_1m_invariant_violation",
                    table=table,
                    key=row["observation_key"],
                    revisions=(row["revision_id"],),
                    start=row["interval_start"],
                    end=row["interval_end"],
                    detail="; ".join(problems),
                )
            )
    return events


def _gap(table: str, start: datetime, end: datetime) -> dict[str, Any]:
    minutes = (end - start) // _MINUTE
    return _event(
        "bar_1m_gap",
        table=table,
        key=None,
        revisions=(),
        start=start,
        end=end,
        detail=f"{minutes} minute(s) without any Canonical bar revision",
    )


def _trade_events(table: str, trade_ids: Iterable[int]) -> list[dict[str, Any]]:
    ids = sorted(set(trade_ids))
    events = []
    for previous, current in zip(ids, ids[1:], strict=False):
        if current != previous + 1:
            events.append(
                _event(
                    "agg_trade_id_discontinuity",
                    table=table,
                    key=None,
                    revisions=(),
                    start=None,
                    end=None,
                    detail=f"aggregate trade ids jump from {previous} to {current} "
                    f"({current - previous - 1} id(s) absent)",
                )
            )
    return events
