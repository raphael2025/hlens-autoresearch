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
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import EqualTo, In

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
)
from infrastructure.pit.selector import PIT_BINDING, REQUIRED_BINDINGS, PitSelection, PitSelector
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.store import BatchCommit

__all__ = [
    "QUALITY_RULE_HASH",
    "QUALITY_RULE_ID",
    "QUALITY_RULE_SPEC",
    "QUALITY_RULE_VERSION",
    "QualityReportError",
    "QualityReported",
    "QualityReporter",
]

QUALITY_RULE_ID: Final = "hlens.quality.canonical-partition"
QUALITY_RULE_VERSION: Final = "1.0.0"
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
    },
    "thresholds": "none (outliers need calibrated thresholds: a later rule version)",
    "evidence_gaps": "every Canonical revision of the partition with an evidence gap",
    "report_id": "<rule>@<version>.<table>.<venue symbol>.<day>.<sha256 of inputs>",
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
            report_id = (
                f"{QUALITY_RULE_ID}@{QUALITY_RULE_VERSION}.{canonical}.{symbol}.{day.isoformat()}."
                f"{_digest({'rule_hash': QUALITY_RULE_HASH, 'bindings': bindings})}"
            )
            selection = self._select(data_type, symbol, day, bindings)
            body = self._body(data_type, symbol, day, bindings, report_id, selection)
            committed = self._committed(report_id)
            if committed is not None:
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
            # Every fact the report describes — revisions and the mapped precedence edges that
            # decide its heads — must be known by its knowledge_time (E1-R3 / review C-2).
            floor = max(
                (
                    *(
                        r.availability.times.knowledge_time
                        for rs in selection.records.values()
                        for r in rs
                    ),
                    *(e.knowledge_time for es in selection.edges.values() for e in es),
                ),
                default=None,
            )
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

    def _select(
        self, data_type: str, symbol: str, day: date, bindings: Mapping[str, str]
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
        start = datetime.combine(day, time(), tzinfo=UTC)
        return PitSelector(self._adapter, self._storage).select(
            spec, data_type, symbol, start, start + _DAY
        )

    # ------------------------------------------------------------------ the report

    def _body(
        self,
        data_type: str,
        symbol: str,
        day: date,
        bindings: Mapping[str, str],
        report_id: str,
        selection: PitSelection,
    ) -> dict[str, Any]:
        canonical = selection.canonical_table
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
        for key in selection.conflicts:
            heads = {
                head
                for item in selection.selections
                if item.observation_key == key
                for head in item.maximal_heads
            }
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
        rows = self._rows(canonical, bindings, selection)
        if data_type == "klines_1m":
            events.extend(_bar_events(canonical, rows, start))
        else:
            events.extend(_trade_events(canonical, rows))
        gaps = [
            {
                "table": canonical,
                "revision_id": row["revision_id"],
                "gap": row["availability_evidence_gap"],
            }
            for row in sorted(rows, key=lambda item: item["revision_id"])
            if row["availability_evidence_gap"] is not None
        ]
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
            "evidence_gaps": gaps,
        }

    def _rows(
        self, canonical: str, bindings: Mapping[str, str], selection: PitSelection
    ) -> list[Mapping[str, Any]]:
        """The proven Canonical rows of the partition (as the selector verified them)."""
        wanted = {r.revision_id for rs in selection.records.values() for r in rs}
        if not wanted:
            return []
        table = next(t for t in rules.CANONICAL_TABLES.values() if t.table == canonical)
        columns = tuple(field.name for field in table.arrow_schema)
        found: list[Mapping[str, Any]] = []
        ids = sorted(wanted)
        for offset in range(0, len(ids), 256):
            found.extend(
                self._adapter.scan_columns(
                    canonical,
                    columns=columns,
                    row_filter=_member("revision_id", ids[offset : offset + 256]),
                    snapshot_id=bindings[canonical],
                ).to_pylist()
            )
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


def _member(column: str, values: Iterable[object]) -> Any:
    return In(column, set(values))  # type: ignore[call-arg, arg-type]


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


def _trade_events(table: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ids = sorted({int(row["venue_trade_id"]) for row in rows})
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
