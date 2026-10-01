"""PostgreSQL evidence for F2 / F3: end to end on the runtime catalog, rebuilt after a restart."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.domain.base import canonical_json
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS, DATASET_SELECTIONS
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseUnconstructible
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, L1, L2, START, World

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[World]:
    with ds.postgres_world(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened


def test_end_to_end_and_restart_rebuild_on_postgres(pg: World) -> None:
    pg.listed(ds.TRADING, L1)
    pg.trades()
    pg.report()
    spec = pg.spec()
    # ADR-0077 DQ-10: the first v2 build is seeded as a pre-cutoff one; the rebuild is the replay
    first = ds.seed_historical_v2(
        pg.builder(), FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END
    )
    assert len(first.selection.rows) == 3 and not first.replayed
    pg.h.reopen()  # a fresh process on the same catalog
    second = pg.builder().build(FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END)
    assert second.replayed
    assert canonical_json(second.manifest.model_dump(mode="json")) == canonical_json(
        first.manifest.model_dump(mode="json")
    )
    assert len(pg.h.rows(DATASET_MANIFESTS)) == 1
    assert (
        ManifestStore(pg.h.adapter, pg.builder()).load(first.manifest.content_hash())
        == first.manifest
    )


def test_before_the_first_observation_nothing_is_written_on_postgres(pg: World) -> None:
    pg.listed(ds.TRADING, L2)
    pg.trades()
    pg.report()
    with pytest.raises(UniverseUnconstructible):
        pg.builder().build(FIRST_SLICE_UNIVERSE, pg.spec(at=L1), "agg_trades", START, END)
    assert pg.h.rows(DATASET_MANIFESTS) == []
    assert pg.h.head(DATASET_SELECTIONS.table) is None
