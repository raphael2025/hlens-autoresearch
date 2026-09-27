"""G2 / wrong time units around 2025-01-01 (archives: ms before, μs from that UTC day on).

The unit is fixed by the archive's date (D1), never guessed per value. An archive whose ticks are
in the other unit must stop at D1 and leave nothing that a later stage could select; a correct μs
archive must meet its ms REST copy as one head; a sub-millisecond trade must never be folded
into its ms REST copy, so a build over it fails closed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from infrastructure.dataset.builder import DatasetSpecError
from infrastructure.pit.selector import PitConflictError
from infrastructure.quality.reporter import QualityReportError
from infrastructure.revision.store import ArchiveRejected
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import DAY, DAY_2025, T0, T0_2025, utc

MINUTE_MS = ss.MINUTE_MS
#: Knowledge of the 2025 fixtures (all after the archive of 2025-01-01 is retrievable).
K_ARCHIVE, N_ARCHIVE = utc(2025, 1, 5), utc(2025, 1, 6)
K_REST, N_REST = utc(2025, 1, 20), utc(2025, 1, 21)
K_EDGE, K_REPORT, SIM_2025 = utc(2025, 2, 1), utc(2025, 2, 10), utc(2025, 3, 1)
WINDOW_2025 = (utc(2025, 1, 1), utc(2025, 1, 1, 1))
REST_RETRIEVED_2025 = T0_2025 + 10_000 * MINUTE_MS


def _world_2025(w: World, lines: list[str], items: list[dict[str, Any]] | None) -> None:
    """Listing + a μs archive of 2025-01-01 (+ its ms REST copy), normalized and reconciled."""
    w.listed()
    archive = rt.archived(
        rt.archive_trades(w, lines, knowledge=K_ARCHIVE, day=DAY_2025, retrieved_at=utc(2025, 1, 3))
    )
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=N_ARCHIVE)
    if items is not None:
        [response] = rt.rest_trades(
            w, items, knowledge=K_REST, t0=T0_2025, retrieved_ms=REST_RETRIEVED_2025
        )
        rt.normalize(w, c.REST_AGGS.table, response, at=N_REST)
    rt.reconcile(w, at=K_EDGE, day=DAY_2025)
    rt.report(w, at=K_REPORT, days=(DAY_2025,))


def _build_2025(w: World) -> Any:
    return rt.build(w, at=SIM_2025, cutoff=SIM_2025, window=WINDOW_2025)


@pytest.mark.parametrize(
    ("day", "first_ms", "factor", "retrieved_at"),
    [
        # ms ticks in an archive of 2025-01-01 (should be μs): read as μs, they fall in 1970.
        (DAY_2025, T0_2025, 1, utc(2025, 1, 3)),
        # μs ticks in an archive of 2023-11-14 (should be ms): read as ms, they fall after 50000.
        (DAY, T0, 1000, c.ARCHIVE_RETRIEVED),
        # the right unit, the wrong day: a 2025-01-01 trade inside the 2024-12-31 (ms) archive.
        (date(2024, 12, 31), T0_2025, 1, utc(2025, 1, 3)),
        # the right unit, the wrong day: a 2024-12-31 trade inside the 2025-01-01 (μs) archive.
        (DAY_2025, T0_2025 - 10 * MINUTE_MS, 1000, utc(2025, 1, 3)),
    ],
    ids=["ms-in-2025", "us-in-2023", "2025-trade-in-2024-archive", "2024-trade-in-2025-archive"],
)
def test_an_archive_in_the_wrong_unit_stops_at_d1_and_reaches_no_dataset(
    w: World, day: date, first_ms: int, factor: int, retrieved_at: datetime
) -> None:
    w.listed()
    items = ss.agg_items(2, first_ms=first_ms)
    outcome = rt.archive_trades(
        w,
        ss.archive_agg_lines(items, factor=factor),
        knowledge=retrieved_at + timedelta(days=1),
        day=day,
        retrieved_at=retrieved_at,
    )
    assert isinstance(outcome, ArchiveRejected), outcome
    for table in (c.ARCHIVES, c.ARCHIVE_AGGS, c.TRADES):
        assert w.h.rows(table) == []
    event_day = ss.at_ms(first_ms).date()
    midnight = datetime.combine(event_day, datetime.min.time(), tzinfo=UTC)
    # E3 has nothing to describe and F3 has no Canonical snapshot to bind: no dataset.
    with pytest.raises(QualityReportError, match="no snapshot"):
        rt.report(w, at=K_REPORT, days=(event_day,))
    before = rt.outputs(w)
    with pytest.raises(DatasetSpecError, match=f"does not bind {c.TRADES.table}"):
        rt.build(w, at=SIM_2025, cutoff=SIM_2025, window=(midnight, midnight + timedelta(days=1)))
    assert rt.outputs(w) == before == (None, None)


def test_the_switch_day_reconciles_microseconds_with_milliseconds_into_one_head(w: World) -> None:
    items = ss.agg_items(3, first_ms=T0_2025)
    _world_2025(w, ss.archive_agg_lines(items, factor=1000), items)
    built = _build_2025(w)
    lineage = {item.canonical_revision_id: item for item in built.manifest.lineage}
    assert [lineage[row["revision_id"]].raw_table for row in built.selection.rows] == [
        c.ARCHIVE_AGGS.table
    ] * 3
    assert sorted(row["event_time"] for row in built.selection.rows) == [
        ss.at_ms(item["T"]) for item in items
    ]
    raw_ticks = sorted(row["timestamp_raw"] for row in w.h.rows(c.ARCHIVE_AGGS))
    assert raw_ticks == [item["T"] * 1000 for item in items]  # really μs rows


def test_a_sub_millisecond_trade_is_never_folded_into_its_rest_copy(w: World) -> None:
    items = ss.agg_items(2, first_ms=T0_2025)
    lines = ss.archive_agg_lines(items, factor=1000)
    lines[0] = lines[0].replace(f",{items[0]['T'] * 1000},", f",{items[0]['T'] * 1000 + 123},")
    _world_2025(w, lines, items)
    before = rt.outputs(w)
    with pytest.raises(PitConflictError, match="1 observation key"):
        _build_2025(w)
    assert rt.outputs(w) == before


def test_without_its_rest_copy_the_sub_millisecond_archive_trade_keeps_its_microseconds(
    w: World,
) -> None:
    items = ss.agg_items(1, first_ms=T0_2025)
    lines = ss.archive_agg_lines(items, factor=1000)
    lines[0] = lines[0].replace(f",{items[0]['T'] * 1000},", f",{items[0]['T'] * 1000 + 123},")
    _world_2025(w, lines, None)
    [row] = _build_2025(w).selection.rows
    assert row["event_time"] == ss.at_ms(items[0]["T"]) + timedelta(microseconds=123)
    assert ds.K_A < K_ARCHIVE  # the 2025 fixture is known after the default 2023 one
