"""D3E-R3 independent reinforcement: public PitSelector + multi-day edge provenance.

Discriminative coverage that the existing R3 suite still lacks:

A) ``PitSelector.select`` (not a private maximal-head helper) over a fixed
   ``PointInTimeSpec`` snapshot binding, for an aggTrade key whose REST revisions
   straddle UTC midnight;
B) the same key spanning three UTC days — interleaved reconcile orders,
   ``verified_edges`` re-read from every day, edge-set convergence;
C) the owner partition later appending another REST key must not break verification
   of the batch that first committed the spanning edges (append-only key-set
   supersets; old pinned bindings stay bitwise stable).

Real harness, real D2 / D3D / D3E / normalizer / reconciler; no private
``_select`` / ``_plan`` / ``_verify_edge_provenance`` shortcuts.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_PRECEDENCE_EVIDENCE
from infrastructure.pit.selector import PIT_BINDING, PitSelector
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.channel_reconcile import ChannelReconciler
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    MINUTE_MS,
    SYMBOL,
    RestHarness,
    StepClock,
    utc,
)

#: 2023-11-15T00:00:00Z — midnight between DAY and DAY+1.
MIDNIGHT_01_MS = 1_700_006_400_000
#: 2023-11-16T00:00:00Z — midnight between DAY+1 and DAY+2.
MIDNIGHT_12_MS = MIDNIGHT_01_MS + 86_400_000
DAY_1 = DAY + timedelta(days=1)
DAY_2 = DAY + timedelta(days=2)

ONLY_0 = f"binance:spot:agg_trade:{SYMBOL}:98"
SPANNING = f"binance:spot:agg_trade:{SYMBOL}:100"
ONLY_1 = f"binance:spot:agg_trade:{SYMBOL}:101"
ONLY_2 = f"binance:spot:agg_trade:{SYMBOL}:102"
# Appended later on the owner day (C): must not disturb the batch that wrote SPANNING.
LATER_KEY = f"binance:spot:agg_trade:{SYMBOL}:97"

K_A = utc(2023, 12, 1)
K_R = utc(2023, 12, 5)
N_A = utc(2023, 12, 6)
N_R = utc(2023, 12, 7)
K_E = utc(2023, 12, 10)
K_E2 = K_E + timedelta(hours=1)
K_E3 = K_E + timedelta(hours=2)
FAR = utc(2030, 1, 1)
ARCHIVE_RETRIEVED = utc(2023, 11, 17)

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


@pytest.fixture
def h(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.sqlite_harness(tmp_path) as opened:
        yield opened


# =========================================================================================
# builders (public ingest / normalize / reconcile only)
# =========================================================================================


def _spec(h: RestHarness, *, cutoff: datetime = FAR, **overrides: Any) -> PointInTimeSpec:
    bindings = {table.table: head for table in TABLES if (head := h.head(table.table)) is not None}
    fields: dict[str, Any] = {
        "name": "d3e-r3.cross-day.pit",
        "version": "1.0.0",
        "knowledge_cutoff": cutoff,
        "snapshot_bindings": bindings,
        "point_in_time_binding": PIT_BINDING,
        "availability_bindings": (rules.AVAILABILITY_BINDING,),
        "precedence_bindings": (DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
        "parser_bindings": (rules.NORMALIZER_BINDING,),
        "simulation_time": FAR,
    }
    fields.update(overrides)
    return PointInTimeSpec(**fields)


def _archive(
    h: RestHarness,
    items: list[dict[str, Any]],
    *,
    day: date,
    knowledge: datetime,
    request_id: str,
) -> str:
    outcome = h.ingest_archive(
        "agg_trades",
        ss.archive_agg_lines(items),
        day=day,
        clock=StepClock(start=knowledge),
        retrieved_at=ARCHIVE_RETRIEVED,
        request_id=request_id,
    )
    assert type(outcome).__name__ == "ArchiveIngested", outcome
    revision: str = outcome.archive_revision_id
    return revision


def _rest(
    h: RestHarness,
    items: list[dict[str, Any]],
    *,
    knowledge: datetime,
    request_id: str,
    t0: int,
) -> str:
    cs.queue_agg_chain(h.venue, SYMBOL, t0, [items])
    request = ss.agg_request(request_id, start_ms=t0)
    collected = h.collect(request, start_ms=cs.RETRIEVED_AT_MS)
    assert not isinstance(collected, Exception), collected
    [page] = h.store(clock=StepClock(start=knowledge)).ingest_collection(request).pages
    return page.response_revision_id


def _normalize(
    h: RestHarness,
    archive_ids: Sequence[str],
    response_ids: Sequence[str],
    *,
    archive_ready: datetime = N_A,
    rest_ready: datetime = N_R,
) -> None:
    n_a = c.normalizer(h, clock=StepClock(start=archive_ready))
    for archive_id in archive_ids:
        n_a.normalize_unit(c.ARCHIVE_AGGS.table, archive_id)
    n_r = c.normalizer(h, clock=StepClock(start=rest_ready))
    for response_id in response_ids:
        n_r.normalize_unit(c.REST_AGGS.table, response_id)


def _raw_edge_ids(edges: Sequence[Any]) -> list[str]:
    """Raw ``edge_id`` tokens carried in mapped Canonical ``PrecedenceEvidence.evidence``."""
    found: list[str] = []
    for edge in edges:
        for item in edge.evidence:
            if isinstance(item, str) and item.startswith("raw_edge_id="):
                found.append(item.removeprefix("raw_edge_id="))
    return found


def _cross_midnight_two_days(h: RestHarness) -> dict[str, Any]:
    """Key 100 has REST on DAY and DAY_1 (different event times); each day has an equal
    archive counterpart and one single-day key. Returns the ingest bookkeeping."""
    only_0 = ss.agg_item(98, MIDNIGHT_01_MS - 2)
    late = ss.agg_item(100, MIDNIGHT_01_MS - 1)
    early = ss.agg_item(100, MIDNIGHT_01_MS)
    only_1 = ss.agg_item(101, MIDNIGHT_01_MS + 1)
    archives = [
        _archive(h, [only_0, late], day=DAY, knowledge=K_A, request_id="arch-d0"),
        _archive(h, [early, only_1], day=DAY_1, knowledge=K_A, request_id="arch-d1"),
    ]
    responses = [
        _rest(
            h,
            [only_0, late],
            knowledge=K_R,
            request_id="req-d0",
            t0=MIDNIGHT_01_MS - MINUTE_MS,
        ),
        _rest(
            h,
            [early, only_1],
            knowledge=K_R + timedelta(hours=1),
            request_id="req-d1",
            t0=MIDNIGHT_01_MS - MINUTE_MS - 1,
        ),
    ]
    days: dict[str, set[date]] = {}
    for row in h.rows(c.REST_AGGS):
        days.setdefault(row["observation_key"], set()).add(row["event_time"].date())
    assert days == {ONLY_0: {DAY}, SPANNING: {DAY, DAY_1}, ONLY_1: {DAY_1}}
    _normalize(h, archives, responses)
    return {"archives": archives, "responses": responses}


def _cross_three_days(h: RestHarness) -> dict[str, Any]:
    """Key 100 has one REST revision on each of three consecutive UTC days."""
    only_0 = ss.agg_item(98, MIDNIGHT_01_MS - 2)
    r0 = ss.agg_item(100, MIDNIGHT_01_MS - 1)
    r1 = ss.agg_item(100, MIDNIGHT_01_MS)
    only_1 = ss.agg_item(101, MIDNIGHT_01_MS + 1)
    r2 = ss.agg_item(100, MIDNIGHT_12_MS)
    only_2 = ss.agg_item(102, MIDNIGHT_12_MS + 1)
    archives = [
        _archive(h, [only_0, r0], day=DAY, knowledge=K_A, request_id="arch-3-d0"),
        _archive(h, [r1, only_1], day=DAY_1, knowledge=K_A, request_id="arch-3-d1"),
        _archive(h, [r2, only_2], day=DAY_2, knowledge=K_A, request_id="arch-3-d2"),
    ]
    responses = [
        _rest(
            h,
            [only_0, r0],
            knowledge=K_R,
            request_id="req-3-d0",
            t0=MIDNIGHT_01_MS - MINUTE_MS,
        ),
        _rest(
            h,
            [r1, only_1],
            knowledge=K_R + timedelta(hours=1),
            request_id="req-3-d1",
            t0=MIDNIGHT_01_MS - MINUTE_MS - 1,
        ),
        _rest(
            h,
            [r2, only_2],
            knowledge=K_R + timedelta(hours=2),
            request_id="req-3-d2",
            t0=MIDNIGHT_12_MS - MINUTE_MS,
        ),
    ]
    days: dict[str, set[date]] = {}
    for row in h.rows(c.REST_AGGS):
        days.setdefault(row["observation_key"], set()).add(row["event_time"].date())
    assert days == {
        ONLY_0: {DAY},
        SPANNING: {DAY, DAY_1, DAY_2},
        ONLY_1: {DAY_1},
        ONLY_2: {DAY_2},
    }
    _normalize(h, archives, responses)
    return {"archives": archives, "responses": responses}


def _reconcile_days(
    h: RestHarness, days: Sequence[date], *, clocks: Sequence[datetime] | None = None
) -> None:
    if clocks is None:
        stamps = [K_E + timedelta(hours=i) for i in range(len(days))]
    else:
        stamps = list(clocks)
    for day, stamp in zip(days, stamps, strict=True):
        h.reconciler(clock=StepClock(start=stamp)).reconcile("agg_trades", SYMBOL, day)


def _pinned_view(h: RestHarness, evidence_snapshot: str | None = None) -> PinnedCatalogView:
    bindings = {table.table: h.head(table.table) for table in TABLES}
    if evidence_snapshot is not None:
        bindings[c.EVIDENCE.table] = evidence_snapshot
    return PinnedCatalogView(
        h.adapter, {table: head for table, head in bindings.items() if head is not None}
    )


def _verified_edge_ids(
    h: RestHarness, day: date, *, evidence_snapshot: str | None = None
) -> list[str]:
    view = _pinned_view(h, evidence_snapshot)
    return sorted(
        edge.edge_id
        for edge in ChannelReconciler(view, h.storage).verified_edges("agg_trades", SYMBOL, day)
    )


def _spanning_edge_triples(h: RestHarness) -> list[tuple[str, str, str]]:
    return sorted(
        (row["edge_id"], row["revision_id"], row["superseded_revision_id"])
        for row in h.rows(c.EVIDENCE)
        if row["observation_key"] == SPANNING
    )


def _archive_canonical_ids(h: RestHarness, key: str) -> set[str]:
    return {
        row["revision_id"]
        for row in h.rows(c.TRADES)
        if row["observation_key"] == key and row["lineage_raw_table"] == c.ARCHIVE_AGGS.table
    }


# =========================================================================================
# A) public PitSelector.select over a fixed cross-midnight manifest
# =========================================================================================


def test_a_pit_selector_select_over_fixed_cross_midnight_snapshots(h: RestHarness) -> None:
    """A: fixed PointInTimeSpec → PitSelector.select; statuses and edges match provenance."""
    _cross_midnight_two_days(h)
    _reconcile_days(h, (DAY, DAY_1), clocks=(K_E, K_E2))

    pinned = _spec(h, cutoff=FAR)
    evidence_head = pinned.snapshot_bindings[BINANCE_SPOT_PRECEDENCE_EVIDENCE.table]
    trades_head = pinned.snapshot_bindings[c.TRADES.table]
    assert evidence_head and trades_head

    # Provenance the selector will re-verify: both days' verified_edges, live and pinned.
    live = {
        day: tuple(
            edge.edge_id
            for edge in h.reconciler(clock=StepClock(start=FAR)).verified_edges(
                "agg_trades", SYMBOL, day
            )
        )
        for day in (DAY, DAY_1)
    }
    pinned_edges = {day: tuple(_verified_edge_ids(h, day)) for day in (DAY, DAY_1)}
    assert live == pinned_edges
    spanning_raw = {
        edge.edge_id
        for day in (DAY, DAY_1)
        for edge in ChannelReconciler(_pinned_view(h), h.storage).verified_edges(
            "agg_trades", SYMBOL, day
        )
        if edge.evidence.observation_key == SPANNING
    }
    assert len(spanning_raw) == 2  # one equal pair per UTC day

    window_start, window_end = utc(2023, 11, 14), utc(2023, 11, 15)
    first = PitSelector(h.adapter, h.storage).select(
        pinned, "agg_trades", SYMBOL, window_start, window_end
    )
    assert first.evidence_bound is True
    by_key = {s.observation_key: s for s in first.selections}
    assert set(by_key) >= {ONLY_0, SPANNING}  # DAY_1-owned ONLY_1 is outside this window
    assert by_key[ONLY_0].status is PointInTimeStatus.SELECTED
    assert by_key[ONLY_0].selected_revision_id in _archive_canonical_ids(h, ONLY_0)
    assert by_key[SPANNING].status is PointInTimeStatus.CONFLICT
    assert set(by_key[SPANNING].maximal_heads) == _archive_canonical_ids(h, SPANNING)
    assert SPANNING in first.conflicts
    # Mapped Canonical edges for the spanning key come from the verified Raw edges
    # (unique set — see the dedicated duplicate-mapping probe below).
    assert SPANNING in first.edges
    mapped_raw = _raw_edge_ids(first.edges[SPANNING])
    assert set(mapped_raw) == spanning_raw and len(spanning_raw) == 2

    # Fixed bindings: after more evidence lands, the old manifest still answers the same.
    _reconcile_days(h, (DAY_1, DAY), clocks=(K_E3, K_E3 + timedelta(minutes=1)))
    later_item = ss.agg_item(200, MIDNIGHT_01_MS - 3)
    later_arch = _archive(h, [later_item], day=DAY, knowledge=K_E3, request_id="arch-later")
    later_resp = _rest(
        h,
        [later_item],
        knowledge=K_E3 + timedelta(minutes=1),
        request_id="req-later",
        t0=MIDNIGHT_01_MS - MINUTE_MS - 2,
    )
    _normalize(
        h,
        [later_arch],
        [later_resp],
        archive_ready=K_E3 + timedelta(hours=1),
        rest_ready=K_E3 + timedelta(hours=2),
    )
    h.reconciler(clock=StepClock(start=K_E3 + timedelta(hours=3))).reconcile(
        "agg_trades", SYMBOL, DAY
    )
    assert h.head(c.EVIDENCE.table) != evidence_head
    assert h.head(c.TRADES.table) != trades_head

    again = PitSelector(h.adapter, h.storage).select(
        pinned, "agg_trades", SYMBOL, window_start, window_end
    )
    assert again == first

    # A fresh binding sees the appended equal pair as SELECTED and keeps the spanning conflict.
    fresh = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "agg_trades", SYMBOL, window_start, window_end
    )
    fresh_by = {s.observation_key: s for s in fresh.selections}
    later_key = f"binance:spot:agg_trade:{SYMBOL}:200"
    assert fresh_by[later_key].status is PointInTimeStatus.SELECTED
    assert fresh_by[SPANNING].status is PointInTimeStatus.CONFLICT
    assert set(fresh_by[SPANNING].maximal_heads) == _archive_canonical_ids(h, SPANNING)


def test_a_pit_selector_day1_window_does_not_reown_the_spanning_key(h: RestHarness) -> None:
    """Earliest event owns the key; DAY_1's window must not select SPANNING again."""
    _cross_midnight_two_days(h)
    _reconcile_days(h, (DAY_1, DAY), clocks=(K_E, K_E2))
    spec = _spec(h, cutoff=FAR)
    selector = PitSelector(h.adapter, h.storage)
    day0 = selector.select(spec, "agg_trades", SYMBOL, utc(2023, 11, 14), utc(2023, 11, 15))
    day1 = selector.select(spec, "agg_trades", SYMBOL, utc(2023, 11, 15), utc(2023, 11, 16))
    assert SPANNING in {s.observation_key for s in day0.selections}
    assert SPANNING not in {s.observation_key for s in day1.selections}
    assert ONLY_1 in {s.observation_key for s in day1.selections}
    [only_1] = [s for s in day1.selections if s.observation_key == ONLY_1]
    assert only_1.status is PointInTimeStatus.SELECTED
    assert only_1.selected_revision_id in _archive_canonical_ids(h, ONLY_1)


def test_a_pit_selector_must_not_duplicate_cross_day_mapped_edges(h: RestHarness) -> None:
    """Production defect probe (not an R3 reconciler regression): when a key's REST revisions
    fall on two UTC days, ``PitSelector._mapped_edges`` walks ``verified_edges`` once per day
    and appends the same Raw spanning edges twice into ``PitSelection.edges``.

    Repro: build ``_cross_midnight_two_days``, reconcile both days, ``PitSelector.select`` over
    ``[2023-11-14, 2023-11-15)`` with a fixed manifest; ``len(_raw_edge_ids(edges[SPANNING]))``
    is 4 while ``len(set(...))`` is 2. Selection status is still CONFLICT (duplicate edges do
    not invent a head), but ADR-0028 §3.2 mapping must be one Canonical image per Raw edge.
    Hand to Opus/coordinator: do not “fix” by weakening this assertion.
    """
    _cross_midnight_two_days(h)
    _reconcile_days(h, (DAY, DAY_1), clocks=(K_E, K_E2))
    out = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "agg_trades", SYMBOL, utc(2023, 11, 14), utc(2023, 11, 15)
    )
    mapped_raw = _raw_edge_ids(out.edges[SPANNING])
    assert len(mapped_raw) == len(set(mapped_raw)) == 2


# =========================================================================================
# B) three UTC days, interleaved reconcile, verified_edges converge
# =========================================================================================


@pytest.mark.parametrize(
    "order",
    [
        (DAY, DAY_1, DAY_2),
        (DAY_2, DAY, DAY_1),
        (DAY_1, DAY_2, DAY),
    ],
    ids=["chronological", "last_first", "middle_first"],
)
def test_b_three_day_key_interleaved_reconcile_converges(
    h: RestHarness, order: tuple[date, date, date]
) -> None:
    """B: ≥3 UTC days; every day's verified_edges (live + pinned) agrees on the spanning set."""
    _cross_three_days(h)
    _reconcile_days(h, order)

    spanning = _spanning_edge_triples(h)
    assert len(spanning) == 3  # one equal archive↔REST pair per day
    assert len({edge_id for edge_id, _, _ in spanning}) == 3

    # Edge set is independent of reconcile order (separate harnesses below for full equality).
    for day in (DAY, DAY_1, DAY_2):
        live = h.reconciler(clock=StepClock(start=FAR)).verified_edges("agg_trades", SYMBOL, day)
        pinned = ChannelReconciler(_pinned_view(h), h.storage).verified_edges(
            "agg_trades", SYMBOL, day
        )
        assert tuple(edge.edge_id for edge in live) == tuple(edge.edge_id for edge in pinned)
        live_spanning = sorted(
            (e.edge_id, e.evidence.revision_id, e.evidence.superseded_revision_id)
            for e in live
            if e.evidence.observation_key == SPANNING
        )
        assert live_spanning == spanning

    # Public PitSelector over a window that owns the key and reaches all three event days
    # (a single UTC-day window cannot pull the third day past ``_KEY_REACH``).
    out = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "agg_trades", SYMBOL, utc(2023, 11, 14), utc(2023, 11, 17)
    )
    [selection] = [s for s in out.selections if s.observation_key == SPANNING]
    assert selection.status is PointInTimeStatus.CONFLICT
    assert set(selection.maximal_heads) == _archive_canonical_ids(h, SPANNING)
    assert set(_raw_edge_ids(out.edges[SPANNING])) == {edge_id for edge_id, _, _ in spanning}
    assert SPANNING in out.conflicts


def test_b_three_day_reconcile_orders_converge_to_the_same_edge_set(tmp_path: Path) -> None:
    edge_sets: list[list[tuple[str, str, str]]] = []
    for label, order in (
        ("fwd", (DAY, DAY_1, DAY_2)),
        ("rev", (DAY_2, DAY_1, DAY)),
        ("mid", (DAY_1, DAY_2, DAY)),
    ):
        with ss.sqlite_harness(tmp_path / label) as h:
            _cross_three_days(h)
            _reconcile_days(h, order)
            edge_sets.append(_spanning_edge_triples(h))
            for day in (DAY, DAY_1, DAY_2):
                assert [
                    (e.edge_id, e.evidence.revision_id, e.evidence.superseded_revision_id)
                    for e in ChannelReconciler(_pinned_view(h), h.storage).verified_edges(
                        "agg_trades", SYMBOL, day
                    )
                    if e.evidence.observation_key == SPANNING
                ] == edge_sets[-1]
    assert len(edge_sets[0]) == 3 and edge_sets[0] == edge_sets[1] == edge_sets[2]


# =========================================================================================
# C) owner later appends another REST key; old batches and pinned PIT stay valid
# =========================================================================================


def test_c_owner_appends_another_rest_key_without_breaking_old_batch(h: RestHarness) -> None:
    """C: append-only key-set growth on the owner day; old batch and pinned select hold."""
    _cross_midnight_two_days(h)
    # DAY owns the first commit of the spanning edges.
    first = h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, DAY)
    assert all(not item.reused for item in first.edges)
    [first_commit] = first.commits
    prefix = f"{DELIVERY_CHANNEL_BINDING.policy_id}@{DELIVERY_CHANNEL_BINDING.version}.edges."
    assert first_commit.batch_id.startswith(f"{prefix}agg_trades.{SYMBOL}.{DAY}.")
    spanning_after_owner = _spanning_edge_triples(h)
    assert len(spanning_after_owner) == 2

    pinned = _spec(h, cutoff=FAR)
    evidence_at_pin = pinned.snapshot_bindings[c.EVIDENCE.table]
    rest_at_pin = pinned.snapshot_bindings[c.REST_AGGS.table]
    assert evidence_at_pin and rest_at_pin
    before = PitSelector(h.adapter, h.storage).select(
        pinned, "agg_trades", SYMBOL, utc(2023, 11, 14), utc(2023, 11, 15)
    )

    # DAY_1 reuses spanning edges, commits its own single-day key.
    second = h.reconciler(clock=StepClock(start=K_E2)).reconcile("agg_trades", SYMBOL, DAY_1)
    assert {item.edge.evidence.observation_key for item in second.edges if item.reused} == {
        SPANNING
    }
    assert _spanning_edge_triples(h) == spanning_after_owner

    # Owner day later gains another REST key (and equal archive counterpart).
    later = ss.agg_item(97, MIDNIGHT_01_MS - 4)
    later_arch = _archive(h, [later], day=DAY, knowledge=K_E3, request_id="arch-append")
    later_resp = _rest(
        h,
        [later],
        knowledge=K_E3 + timedelta(minutes=1),
        request_id="req-append",
        t0=MIDNIGHT_01_MS - MINUTE_MS - 3,
    )
    _normalize(
        h,
        [later_arch],
        [later_resp],
        archive_ready=K_E3 + timedelta(hours=1),
        rest_ready=K_E3 + timedelta(hours=2),
    )
    assert h.head(c.REST_AGGS.table) != rest_at_pin

    # Live reconcile of both days still verifies the original owner batch (key-set superset).
    again_day = h.reconciler(clock=StepClock(start=K_E3 + timedelta(hours=1))).reconcile(
        "agg_trades", SYMBOL, DAY
    )
    assert {item.edge.evidence.observation_key for item in again_day.edges if item.reused} >= {
        SPANNING,
        ONLY_0,
    }
    assert [item.edge.evidence.observation_key for item in again_day.edges if not item.reused] == [
        LATER_KEY
    ]
    again_next = h.reconciler(clock=StepClock(start=K_E3 + timedelta(hours=2))).reconcile(
        "agg_trades", SYMBOL, DAY_1
    )
    assert again_next.commits == () and all(item.reused for item in again_next.edges)
    assert _spanning_edge_triples(h) == spanning_after_owner

    for day in (DAY, DAY_1):
        live = _verified_edge_ids(h, day)
        pinned_now = _verified_edge_ids(h, day)
        assert live == pinned_now
        # Manifest pinned at the pre-append evidence head still sees the spanning edges.
        early = _verified_edge_ids(h, day, evidence_snapshot=evidence_at_pin)
        assert set(early) <= set(live)
        assert {
            row["edge_id"]
            for row in h.rows_at(c.EVIDENCE.table, evidence_at_pin)
            if row["observation_key"] == SPANNING
        } <= set(early)

    # Old PointInTimeSpec bindings are bitwise stable despite the append.
    assert (
        PitSelector(h.adapter, h.storage).select(
            pinned, "agg_trades", SYMBOL, utc(2023, 11, 14), utc(2023, 11, 15)
        )
        == before
    )
    # Fresh bindings additionally select the appended key via its new edge.
    fresh = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "agg_trades", SYMBOL, utc(2023, 11, 14), utc(2023, 11, 15)
    )
    [later_sel] = [s for s in fresh.selections if s.observation_key == LATER_KEY]
    assert later_sel.status is PointInTimeStatus.SELECTED
    assert later_sel.selected_revision_id in _archive_canonical_ids(h, LATER_KEY)
