"""F1 point-in-time selector (ADR-0023 §5, ADR-0028 §3.2 / §7; roadmap #18).

Real Raw rows (D2 / D3E), the real normalizer and reconciler; selections are checked against
values written down from ADR-0023 §5 / ADR-0028 §4 by hand.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus, PolicyBinding, PolicyRole
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.pit import selector as selector_module
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from infrastructure.pit.selector import (
    PIT_BINDING,
    PitConflictError,
    PitSelector,
    PitSpecError,
)
from infrastructure.quality.reporter import QualityReporter, evidence_gaps_of
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    SYMBOL,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

K_A, K_R = utc(2023, 12, 1), utc(2023, 12, 5)
N_A, N_R = utc(2023, 12, 6), utc(2023, 12, 7)
K_E = utc(2023, 12, 10)
FAR = utc(2030, 1, 1)
MINUTE = timedelta(minutes=1)
START, END = utc(2023, 11, 14), utc(2023, 11, 15)
KEY = f"binance:spot:agg_trade:{SYMBOL}:100"
TABLES = (
    c.ARCHIVES,
    c.ARCHIVE_AGGS,
    c.ARCHIVE_KLINES,
    c.RESPONSES,
    c.REST_AGGS,
    c.REST_KLINES,
    c.EVIDENCE,
    c.TRADES,
    c.BARS,
)


def _spec(
    h: RestHarness,
    *,
    cutoff: datetime,
    at: datetime = FAR,
    interval: tuple[datetime, datetime] | None = None,
    skip: tuple[str, ...] = (),
    **overrides: Any,
) -> PointInTimeSpec:
    bindings = {
        table.table: head
        for table in TABLES
        if table.table not in skip and (head := h.head(table.table)) is not None
    }
    fields: dict[str, Any] = {
        "name": "test.pit",
        "version": "1.0.0",
        "knowledge_cutoff": cutoff,
        "snapshot_bindings": bindings,
        "point_in_time_binding": PIT_BINDING,
        "availability_bindings": (rules.AVAILABILITY_BINDING,),
        "precedence_bindings": (DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
        "parser_bindings": (rules.NORMALIZER_BINDING,),
    }
    if interval is None:
        fields["simulation_time"] = at
    else:
        fields["simulation_start"], fields["simulation_end"] = interval
    fields.update(overrides)
    return PointInTimeSpec(**fields)


def _chain(h: RestHarness, *, reconcile: bool = True, count: int = 1) -> tuple[str, str]:
    items = ss.agg_items(count)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_R)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
    if reconcile:
        h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)
    return archive, response


def _select(h: RestHarness, spec: PointInTimeSpec) -> Any:
    return PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, START, END)


def _canonical(h: RestHarness, channel_table: str) -> dict[str, Any]:
    [row] = [row for row in h.rows(c.TRADES) if row["lineage_raw_table"] == channel_table]
    return row


# =========================================================================================
# the four cutoffs, lineage, gaps
# =========================================================================================


def test_the_four_cutoffs_select_as_adr_0028_section_4(h: RestHarness) -> None:
    archive, _ = _chain(h)
    a = _canonical(h, c.ARCHIVE_AGGS.table)
    r = _canonical(h, c.REST_AGGS.table)
    expected = {
        N_A - c.TICK: (PointInTimeStatus.ABSENT, ()),
        N_A: (PointInTimeStatus.SELECTED, (a["revision_id"],)),
        N_R: (PointInTimeStatus.CONFLICT, tuple(sorted((a["revision_id"], r["revision_id"])))),
        K_E - c.TICK: (
            PointInTimeStatus.CONFLICT,
            tuple(sorted((a["revision_id"], r["revision_id"]))),
        ),
        K_E: (PointInTimeStatus.SELECTED, (a["revision_id"],)),
    }
    for cutoff, (status, heads) in expected.items():
        [selection] = _select(h, _spec(h, cutoff=cutoff)).selections
        assert (selection.status, selection.maximal_heads) == (status, heads), cutoff
    out = _select(h, _spec(h, cutoff=K_E))
    [lineage] = out.lineage
    assert (lineage.canonical_table, lineage.canonical_revision_id) == (
        "canonical.trades",
        a["revision_id"],
    )
    assert (lineage.raw_table, lineage.raw_revision_id) == (
        "raw.binance_spot_agg_trades",
        a["lineage_raw_revision_id"],
    )
    assert (lineage.source_table, lineage.source_revision_id) == (
        "raw.binance_spot_archives",
        archive,
    )
    [gap] = out.evidence_gaps
    assert (gap.table, gap.revision_id, gap.gap) == (
        "canonical.trades",
        a["revision_id"],
        a["availability_evidence_gap"],
    )
    assert out.conflicts == ()
    conflicted = _select(h, _spec(h, cutoff=N_R))
    assert conflicted.conflicts == (KEY,) and conflicted.lineage == ()
    with pytest.raises(PitConflictError, match="competing heads"):
        conflicted.require_no_conflict()


def test_available_time_gates_the_simulation_axis(h: RestHarness) -> None:
    _chain(h)
    a = _canonical(h, c.ARCHIVE_AGGS.table)
    # Canonical available_time = the Raw ingest time (evidence gap, ADR-0023 §2).
    before = a["available_time"] - c.TICK
    [selection] = _select(h, _spec(h, cutoff=FAR, at=before)).selections
    assert selection.status is PointInTimeStatus.ABSENT


def test_an_interval_is_evaluated_where_a_selection_can_change(h: RestHarness) -> None:
    _chain(h)
    a = _canonical(h, c.ARCHIVE_AGGS.table)
    r = _canonical(h, c.REST_AGGS.table)
    assert a["available_time"] < r["available_time"]
    start, end = utc(2023, 11, 15), utc(2023, 12, 31)
    out = _select(h, _spec(h, cutoff=FAR, interval=(start, end)))
    # absent until the archive revision is available, then the archive lineage throughout
    # (the REST revision becomes available later but the mapped edge already orders it).
    assert [(s.simulation_time, s.status, s.selected_revision_id) for s in out.selections] == [
        (start, PointInTimeStatus.ABSENT, None),
        (a["available_time"], PointInTimeStatus.SELECTED, a["revision_id"]),
    ]


# =========================================================================================
# the bound snapshots decide, not the current heads
# =========================================================================================


def test_an_old_spec_reproduces_its_result_after_new_evidence(h: RestHarness) -> None:
    _chain(h, reconcile=False)
    old = _spec(h, cutoff=FAR)
    first = _select(h, old)
    assert first.conflicts == (KEY,)  # no edge was persisted at those snapshots
    h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)
    # More Raw and Canonical data lands after the old spec was bound: heads move.
    [later] = c.ingest_rest(
        h, "agg_trades", ss.agg_items(1, first_id=900), knowledge=K_E, request_id="req-later"
    )
    c.normalizer(h, clock=StepClock(start=K_E)).normalize_unit(c.REST_AGGS.table, later)
    assert old.snapshot_bindings[c.TRADES.table] != h.head(c.TRADES.table)
    again = _select(h, old)
    assert again == first  # bitwise the same answer from the same bindings
    new = _select(h, _spec(h, cutoff=FAR))
    assert new.conflicts == () and len(new.lineage) == 2  # key 100 via the edge, key 900 alone


def test_an_unbound_evidence_table_only_ever_yields_conflicts(h: RestHarness) -> None:
    """Unbound evidence = no edges: both channels -> conflict; recorded in the result."""
    _chain(h)
    out = _select(h, _spec(h, cutoff=FAR, skip=(c.EVIDENCE.table,)))
    assert out.conflicts == (KEY,) and out.evidence_bound is False
    assert _select(h, _spec(h, cutoff=FAR)).evidence_bound is True


def test_rest_only_keys_select_the_same_with_or_without_the_evidence_binding(
    h: RestHarness,
) -> None:
    """A never-written evidence table cannot be bound; REST-only keys involve no edge anyway."""
    [response] = c.ingest_rest(h, "agg_trades", ss.agg_items(1), knowledge=K_R)
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
    assert h.head(c.EVIDENCE.table) is None  # nothing to bind
    [selection] = _select(h, _spec(h, cutoff=FAR)).selections
    assert selection.status is PointInTimeStatus.SELECTED


def test_an_archive_only_scope_needs_no_evidence_binding(h: RestHarness) -> None:
    [item] = ss.agg_items(1)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines([item]), knowledge=K_A)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    [selection] = _select(h, _spec(h, cutoff=FAR)).selections
    assert selection.status is PointInTimeStatus.SELECTED


def test_selection_is_deterministic(h: RestHarness) -> None:
    _chain(h, count=3)
    spec = _spec(h, cutoff=FAR)
    assert _select(h, spec) == _select(h, spec)
    assert len(_select(h, spec).lineage) == 3


def test_a_mismatch_stays_a_conflict(h: RestHarness) -> None:
    items = ss.agg_items(1)
    archive_items = [dict(items[0], p="92792.04000000")]
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(archive_items), knowledge=K_A)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_R)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
    h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)
    assert _select(h, _spec(h, cutoff=FAR)).conflicts == (KEY,)


# =========================================================================================
# fail closed
# =========================================================================================

_WRONG = PolicyBinding(
    role=PolicyRole.PARSER,
    policy_id="hlens.canonical.binance-spot.normalizer",
    version="1.0.0",
    policy_hash="0" * 64,
)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"parser_bindings": (_WRONG,)}, "parser_bindings"),
        ({"precedence_bindings": (DELIVERY_CHANNEL_BINDING,)}, "precedence-map"),
        (
            {"point_in_time_binding": PIT_BINDING.model_copy(update={"policy_hash": "1" * 64})},
            "PIT rule",
        ),
    ],
)
def test_wrong_bindings_are_refused(h: RestHarness, overrides: dict[str, Any], match: str) -> None:
    _chain(h)
    with pytest.raises(PitSpecError, match=match):
        _select(h, _spec(h, cutoff=FAR, **overrides))


def test_an_unbound_canonical_table_and_a_bad_window_are_refused(h: RestHarness) -> None:
    _chain(h)
    with pytest.raises(PitSpecError, match="does not bind canonical.trades"):
        _select(h, _spec(h, cutoff=FAR, skip=(c.TRADES.table,)))
    selector = PitSelector(h.adapter, h.storage)
    with pytest.raises(PitSpecError, match="UTC datetime"):
        selector.select(_spec(h, cutoff=FAR), "agg_trades", SYMBOL, START.replace(tzinfo=None), END)
    with pytest.raises(PitSpecError, match="must not be empty"):
        selector.select(_spec(h, cutoff=FAR), "agg_trades", SYMBOL, END, END)


def test_a_forged_canonical_row_is_refused(h: RestHarness) -> None:
    _chain(h)
    row = _canonical(h, c.REST_AGGS.table)
    h.delete_rows(c.TRADES, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(
        c.TRADES, [dict(row, available_time=row["available_time"] - timedelta(days=9))], "x"
    )
    with pytest.raises(CatalogIntegrityError):
        _select(h, _spec(h, cutoff=FAR))


def test_an_unbound_raw_table_makes_its_canonical_rows_unprovable(h: RestHarness) -> None:
    _chain(h)
    with pytest.raises(CatalogIntegrityError, match="no Raw element revision"):
        _select(h, _spec(h, cutoff=FAR, skip=(c.REST_AGGS.table,)))


def test_r3_a_recommitted_earlier_edge_cannot_change_a_selection(h: RestHarness) -> None:
    """Review C-1: before D3E-R3 this turned the honest conflict at 12-08 into a selection."""
    _chain(h)
    cutoff = utc(2023, 12, 8)
    assert _select(h, _spec(h, cutoff=cutoff)).conflicts == (KEY,)
    [edge] = h.rows(c.EVIDENCE)
    h.delete_rows(c.EVIDENCE, EqualTo("edge_id", edge["edge_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(c.EVIDENCE, [dict(edge, knowledge_time=K_R)], "forged-edge")
    with pytest.raises(CatalogIntegrityError, match="not exactly what an edge batch"):
        _select(h, _spec(h, cutoff=cutoff))


# =========================================================================================
# G3-S2: a narrow window proves only the committed batches it reads
# =========================================================================================


@dataclass
class _RawScans(ProxyCatalog):
    """Counts full-width reads of the archive element table (proof windows)."""

    widths: list[tuple[int, int]] = field(default_factory=list)
    columns: list[tuple[str, ...]] = field(default_factory=list)

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_columns(table, **kwargs)
        if table == c.ARCHIVE_AGGS.table:
            self.widths.append((len(kwargs["columns"]), result.num_rows))
            self.columns.append(tuple(kwargs["columns"]))
        return result


def test_a_narrow_window_proves_only_the_batches_it_reads(h: RestHarness) -> None:
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    c.normalizer(h, clock=StepClock(start=N_A), microbatch_rows=2).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    first = utc(2023, 11, 14, 22, 14)  # ss.T0: the unit's first trade
    log = _RawScans(h.adapter)
    out = PitSelector(log, h.storage).select(
        _spec(h, cutoff=FAR), "agg_trades", SYMBOL, first + 2 * MINUTE, first + 4 * MINUTE
    )
    assert sorted(row["arrival_seq"] for row in out.selected_rows.values()) == [3, 4]
    # One Raw proof window of the second batch (positions 3-4) — not the unit's four windows.
    assert [rows for width, rows in log.widths if width > 3 and rows <= 2] == [2]
    whole = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "agg_trades", SYMBOL, START, END
    )
    assert {r: whole.selected_rows[r] for r in out.selected_rows} == dict(out.selected_rows)


def test_one_selector_proves_each_unit_once_across_slices(h: RestHarness) -> None:
    """G3-S3: bound snapshots never change, so slices under one spec reuse the proofs, and
    every slice equals what a fresh selector returns."""
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    c.normalizer(h, clock=StepClock(start=N_A), microbatch_rows=2).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    first = utc(2023, 11, 14, 22, 14)
    slices = [(first, first + 3 * MINUTE), (first + 3 * MINUTE, first + 7 * MINUTE)]
    spec = _spec(h, cutoff=FAR)
    log = _RawScans(h.adapter)
    shared = PitSelector(log, h.storage)
    for start, end in slices:
        out = shared.select(spec, "agg_trades", SYMBOL, start, end)
        fresh = PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, start, end)
        assert out.selections == fresh.selections
        assert dict(out.selected_rows) == dict(fresh.selected_rows)
    # The unit's positions (a narrow read) were read once for both slices.
    assert log.columns.count(("archive_line_number", "symbol")) == 1
    # A new spec (other bindings) starts over.
    later = _spec(h, cutoff=K_A)
    shared.select(later, "agg_trades", SYMBOL, *slices[0])
    assert shared._bound == tuple(sorted(later.snapshot_bindings.items()))


# =========================================================================================
# G3-S3-R1: a key's revisions are evaluated together whatever window reads it (review G-1)
# =========================================================================================


def _straddle(h: RestHarness) -> str:
    """Trade 100: archive copy at 21:59:59.999, REST copy at 22:14 — a mismatch, no edge."""
    items = ss.agg_items(3)
    archive_items = [dict(item) for item in items]
    archive_items[0]["T"] = ss.T0 - 14 * ss.MINUTE_MS - 1
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(archive_items), knowledge=K_A)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_R)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
    h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)
    return f"binance:spot:agg_trade:{SYMBOL}:100"


@pytest.mark.parametrize(
    ("window", "owned"),
    [
        ((utc(2023, 11, 14), utc(2023, 11, 15)), True),
        ((utc(2023, 11, 14, 21), utc(2023, 11, 14, 22)), True),  # holds the earliest copy
        ((utc(2023, 11, 14, 22), utc(2023, 11, 14, 23)), False),
    ],
)
def test_a_window_never_sees_one_side_of_a_conflict(
    h: RestHarness, window: tuple[datetime, datetime], owned: bool
) -> None:
    """Review G-1 / H-1: a straddling key is evaluated with all its revisions, by the one window
    holding its earliest event; a touching reader sees it in every window it touches."""
    key = _straddle(h)
    selector = PitSelector(h.adapter, h.storage)
    spec = _spec(h, cutoff=FAR)
    out = selector.select(spec, "agg_trades", SYMBOL, *window)
    assert (key in out.conflicts) is owned and (key in out.records) is owned
    if owned:
        assert len(out.records[key]) == 2
    touching = selector.select(spec, "agg_trades", SYMBOL, *window, touching=True)
    assert key in touching.conflicts and len(touching.records[key]) == 2


def test_adjacent_windows_never_select_a_key_twice(h: RestHarness) -> None:
    """Review H-1: with only the archive copy known, the key is selected by one hour only."""
    key = _straddle(h)
    spec = _spec(h, cutoff=N_A)
    selector = PitSelector(h.adapter, h.storage)
    hours = [
        (utc(2023, 11, 14, 21), utc(2023, 11, 14, 22)),
        (utc(2023, 11, 14, 22), utc(2023, 11, 14, 23)),
    ]
    selected = [
        s.selected_revision_id
        for start, end in hours
        for s in selector.select(spec, "agg_trades", SYMBOL, start, end).selections
        if s.observation_key == key and s.status is PointInTimeStatus.SELECTED
    ]
    whole = selector.select(spec, "agg_trades", SYMBOL, START, END)
    assert len(selected) == 1
    assert selected == [
        s.selected_revision_id
        for s in whole.selections
        if s.observation_key == key and s.status is PointInTimeStatus.SELECTED
    ]


def test_the_day_report_keeps_a_conflict_whose_revisions_straddle_slices(h: RestHarness) -> None:
    key = _straddle(h)
    out = QualityReporter(h.adapter, h.storage, clock=StepClock(start=utc(2023, 12, 20))).report(
        "agg_trades", SYMBOL, ss.DAY
    )
    competing = [e for e in out.row["events"] if e["event_type"] == "competing_heads"]
    assert [e["observation_key"] for e in competing] == [key]
    # Each revision's gap is listed once, although both slices evaluated the key.
    gaps = [gap["revision_id"] for gap in evidence_gaps_of(h.adapter, out.report_id)]
    assert (
        len(gaps)
        == len(set(gaps))
        == len([r for r in h.rows(c.TRADES) if r["availability_evidence_gap"] is not None])
    )


# =========================================================================================
# G3-S3-R3: chains of a key's revisions (review G3 cursor-1)
# =========================================================================================


def _revisions(*offsets: timedelta, key: str = "k") -> list[dict[str, Any]]:
    base = utc(2023, 11, 14)
    return [
        {"observation_key": key, "revision_id": f"{key}{i}", "event_time": base + offset}
        for i, offset in enumerate(offsets)
    ]


def _owners(rows: list[dict[str, Any]], step: timedelta, days: int = 5) -> dict[str, int]:
    """How many windows of a partition of time own each revision."""

    def read(low: datetime, high: datetime) -> list[Mapping[str, Any]]:
        return [row for row in rows if low <= row["event_time"] < high]

    counts = {row["revision_id"]: 0 for row in rows}
    start = utc(2023, 11, 13)
    while start < utc(2023, 11, 13) + timedelta(days=days):
        window = [row for row in rows if start <= row["event_time"] < start + step]
        if window:
            for row in selector_module._key_closure(
                read, "event_time", start, start + step, touching=False
            ):
                counts[row["revision_id"]] += 1
        start += step
    return counts


@pytest.mark.parametrize("step", [timedelta(hours=1), timedelta(hours=6), timedelta(days=1)])
@pytest.mark.parametrize(
    "offsets",
    [
        (timedelta(0), timedelta(hours=21.6), timedelta(hours=43.2)),  # a chain over 1.8 days
        (timedelta(0), timedelta(hours=2)),  # a plain straddle
        (timedelta(0), timedelta(days=3)),  # two chains: each owned once
        (timedelta(hours=5),),
    ],
)
def test_every_revision_is_owned_by_exactly_one_window(
    step: timedelta, offsets: tuple[timedelta, ...]
) -> None:
    assert set(_owners(_revisions(*offsets), step).values()) == {1}


def test_a_chain_is_read_whole_whichever_window_reads_it() -> None:
    rows = _revisions(timedelta(0), timedelta(hours=21.6), timedelta(hours=43.2))

    def read(low: datetime, high: datetime) -> list[Mapping[str, Any]]:
        return [row for row in rows if low <= row["event_time"] < high]

    late = utc(2023, 11, 14) + timedelta(hours=43)
    touching = selector_module._key_closure(
        read, "event_time", late, late + timedelta(hours=1), touching=True
    )
    assert {row["revision_id"] for row in touching} == {"k0", "k1", "k2"}


# =========================================================================================
# ADR-0032 (D-HIST): the archive event-time availability assumption
# =========================================================================================

TRADE_AT = utc(2023, 11, 14, 22, 14)  # ss.T0: the fixture trades' event time
WITH_ASSUMPTION = (rules.AVAILABILITY_BINDING, ASSUMPTION_BINDING)


def _archive_only(h: RestHarness) -> str:
    items = ss.agg_items(1)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    return f"binance:spot:agg_trade:{SYMBOL}:100"


def _status(h: RestHarness, at: datetime, **overrides: Any) -> Any:
    out = _select(h, _spec(h, cutoff=FAR, at=at, **overrides))
    [selection] = out.selections
    return selection.status, out


def test_without_the_assumption_history_before_ingest_is_invisible(h: RestHarness) -> None:
    _archive_only(h)
    status, out = _status(h, TRADE_AT + timedelta(hours=1))
    assert status is PointInTimeStatus.ABSENT and out.assumed == {}


def test_the_bound_assumption_makes_an_archive_trade_available_at_event_time_plus_latency(
    h: RestHarness,
) -> None:
    _archive_only(h)
    [row] = h.rows(c.TRADES)
    before, _ = _status(
        h, TRADE_AT + ASSUMPTION_LATENCY - timedelta(microseconds=1),
        availability_bindings=WITH_ASSUMPTION,
    )  # fmt: skip
    status, out = _status(h, TRADE_AT + ASSUMPTION_LATENCY, availability_bindings=WITH_ASSUMPTION)
    assert before is PointInTimeStatus.ABSENT and status is PointInTimeStatus.SELECTED
    effective = TRADE_AT + ASSUMPTION_LATENCY
    assert out.assumed == {row["revision_id"]: (row["available_time"], effective)}
    assert out.selected_rows[row["revision_id"]]["available_time"] == effective
    # The stored row is unchanged and its evidence gap is still listed and bound.
    assert h.rows(c.TRADES)[0]["available_time"] == row["available_time"]
    assert [gap.revision_id for gap in out.evidence_gaps] == [row["revision_id"]]


def test_the_assumption_never_moves_a_rest_revision(h: RestHarness) -> None:
    items = ss.agg_items(1)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_R)
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
    status, out = _status(h, TRADE_AT + timedelta(hours=1), availability_bindings=WITH_ASSUMPTION)
    assert status is PointInTimeStatus.ABSENT and out.assumed == {}


def test_a_bar_becomes_available_at_its_close_plus_latency(h: RestHarness) -> None:
    items = ss.kline_items(1)
    archive = c.ingest_archive(h, "klines_1m", ss.archive_kline_lines(items), knowledge=K_A)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_KLINES.table, archive)
    [bar] = h.rows(c.BARS)
    spec = _spec(h, cutoff=FAR, at=bar["interval_end"] + ASSUMPTION_LATENCY)
    spec = spec.model_copy(update={"availability_bindings": WITH_ASSUMPTION})
    out = PitSelector(h.adapter, h.storage).select(spec, "klines_1m", SYMBOL, START, END)
    [selection] = out.selections
    assert selection.status is PointInTimeStatus.SELECTED
    assert out.assumed[bar["revision_id"]][1] == bar["interval_end"] + ASSUMPTION_LATENCY


def test_another_version_or_hash_of_the_assumption_is_refused(h: RestHarness) -> None:
    _archive_only(h)
    forged = ASSUMPTION_BINDING.model_copy(update={"policy_hash": "0" * 64})
    with pytest.raises(PitSpecError, match="archive-event-time-assumption"):
        _select(
            h,
            _spec(h, cutoff=FAR, availability_bindings=(rules.AVAILABILITY_BINDING, forged)),
        )
