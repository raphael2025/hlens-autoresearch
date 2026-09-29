"""PostgreSQL evidence for F1: selection at bound snapshots on the runtime catalog."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.contracts.revision import PointInTimeStatus
from infrastructure.pit.selector import PitSelector
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.pit.test_selector import END, FAR, K_E, KEY, START, _chain, _spec
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, StepClock

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.postgres_harness(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened


def test_bound_snapshots_decide_on_postgres(pg: RestHarness) -> None:
    _chain(pg, reconcile=False)
    old = _spec(pg, cutoff=FAR)
    selector = PitSelector(
        pg.adapter, pg.storage, canonical_scratch_directory=pg.tmp_path / "canonical-scratch"
    )
    first = selector.select(old, "agg_trades", SYMBOL, START, END)
    assert first.conflicts == (KEY,)
    pg.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)
    restarted = PitSelector(
        pg.reopen(), pg.storage, canonical_scratch_directory=pg.tmp_path / "canonical-scratch"
    )  # a fresh process
    assert restarted.select(old, "agg_trades", SYMBOL, START, END) == first
    new = restarted.select(_spec(pg, cutoff=FAR), "agg_trades", SYMBOL, START, END)
    [selection] = new.selections
    assert selection.status is PointInTimeStatus.SELECTED
    [row] = [row for row in pg.rows(c.TRADES) if row["lineage_raw_table"] == c.ARCHIVE_AGGS.table]
    assert selection.selected_revision_id == row["revision_id"]
