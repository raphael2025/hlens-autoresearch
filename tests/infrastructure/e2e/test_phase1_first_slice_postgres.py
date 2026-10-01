"""PostgreSQL evidence for the G1 first-slice dataset path (roadmap #20).

Mirrors ``tests/infrastructure/dataset/test_dataset_postgres.py``: the same small BTCUSDT /
ETHUSDT fixture as ``test_phase1_first_slice.py``, built on the dedicated PostgreSQL test catalog
and rebuilt after a simulated process restart. Skips without ``HLENS_TEST_CATALOG_URI``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from core.domain.base import canonical_json
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS, DATASET_SELECTIONS
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e.first_slice_support import (
    BTC,
    DAY_END,
    DAY_START,
    ETH,
    KLINE_COUNT,
    ingest_bars_for,
    ingest_trades_for,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[ds.World]:
    with ds.postgres_world(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened


def test_first_slice_dataset_end_to_end_and_restart_rebuild_on_postgres(pg: ds.World) -> None:
    pg.listed(ds.TRADING, ds.L1)
    pg.trades()  # BTCUSDT aggTrades
    ingest_trades_for(pg, ETH, tag="eth")  # ETHUSDT aggTrades
    ingest_bars_for(pg, BTC, tag="btc", base="100")  # BTCUSDT 1m klines
    ingest_bars_for(pg, ETH, tag="eth", base="200")  # ETHUSDT 1m klines
    pg.report("agg_trades", symbols=(BTC, ETH), listing=False)
    pg.report("klines_1m", symbols=(BTC, ETH), listing=True)

    spec = pg.spec()
    # ADR-0077 DQ-10: the first v2 build is seeded as a pre-cutoff one; the rebuild is the replay
    first = ds.seed_historical_v2(
        pg.builder(), FIRST_SLICE_UNIVERSE, spec, "klines_1m", DAY_START, DAY_END
    )
    assert len(first.selection.rows) == 2 * KLINE_COUNT and not first.replayed
    assert [ds.symbol_of(m) for m in first.manifest.members] == ["BTC-USDT", "ETH-USDT"]

    pg.h.reopen()  # a fresh process on the same catalog
    second = pg.builder().build(FIRST_SLICE_UNIVERSE, spec, "klines_1m", DAY_START, DAY_END)
    assert second.replayed
    assert second.dataset_commit.snapshot_id == first.dataset_commit.snapshot_id
    assert canonical_json(second.manifest.model_dump(mode="json")) == canonical_json(
        first.manifest.model_dump(mode="json")
    )
    assert len(pg.h.rows(DATASET_MANIFESTS)) == 1
    assert (
        ManifestStore(pg.h.adapter, pg.builder()).load(first.manifest.content_hash())
        == first.manifest
    )
    assert pg.h.head(DATASET_SELECTIONS.table) == first.dataset_commit.snapshot_id
