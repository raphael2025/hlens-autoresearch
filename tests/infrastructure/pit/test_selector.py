"""F1 point-in-time selector (ADR-0023 §5, ADR-0028 §3.2 / §7; roadmap #18).

Real Raw rows (D2 / D3E), the real normalizer and reconciler; selections are checked against
values written down from ADR-0023 §5 / ADR-0028 §4 by hand.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus, PolicyBinding, PolicyRole
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.pit.selector import (
    PIT_BINDING,
    PitConflictError,
    PitSelector,
    PitSpecError,
)
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, StepClock, utc

K_A, K_R = utc(2023, 12, 1), utc(2023, 12, 5)
N_A, N_R = utc(2023, 12, 6), utc(2023, 12, 7)
K_E = utc(2023, 12, 10)
FAR = utc(2030, 1, 1)
START, END = utc(2023, 11, 14), utc(2023, 11, 15)
KEY = f"binance:spot:agg_trade:{SYMBOL}:100"
TABLES = (c.ARCHIVES, c.ARCHIVE_AGGS, c.RESPONSES, c.REST_AGGS, c.EVIDENCE, c.TRADES)


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


def test_an_unbound_evidence_table_leaves_the_conflict(h: RestHarness) -> None:
    _chain(h)
    out = _select(h, _spec(h, cutoff=FAR, skip=(c.EVIDENCE.table,)))
    assert out.conflicts == (KEY,)


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
    with pytest.raises(PitSpecError, match="midnight"):
        PitSelector(h.adapter, h.storage).select(
            _spec(h, cutoff=FAR), "agg_trades", SYMBOL, START + timedelta(hours=1), END
        )


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
