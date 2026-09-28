"""DATA-4: sealed dataset-pair G5 consumers over v3 evidence manifests (ADR-0049 / 0077)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from infrastructure.feature.dataset import load_any_manifest
from plugins.features import BarVolumeSumProvider
from research.loop.dataset_source import DatasetCatalog, SealedDatasetPair
from research.loop.segment import SealedDataRefused
from research.validation.sealed_oos import SealedEvaluation, SealedWindow
from tests.infrastructure.bars import v3_support as v3
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt

START, END = v3.DAY_WINDOW
WINDOW = (START, END)
FAMILY = "dataset_g5_v3_test_family"
FEATURE = BarVolumeSumProvider.spec(2, available_lag=timedelta(minutes=1))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        v3.ingest(opened)
        yield opened


def _sealed_case(world: World) -> tuple[DatasetCatalog, SealedDatasetPair, Any, Any, Any]:
    """Real v3 sealed manifests plus a catalog wired to their streaming verifier."""
    feature_pit = v3.spec_of(world, interval=v3.INTERVAL)
    price_pit = v3.spec_of(world)
    machinery = v3.v3(world)
    feature = machinery.build(feature_pit, window=WINDOW)
    price = machinery.build(price_pit, window=WINDOW)
    builder = world.builder()
    research_price = load_any_manifest(builder, price.manifest_hash, machinery.verifier)
    catalog = DatasetCatalog(
        adapter=world.h.adapter,
        storage=world.h.storage,
        builder=builder,
        evidence_verifier=machinery.verifier,
    )
    pair = SealedDatasetPair(
        catalog,
        symbol="BTC-USDT",
        feature_manifest_hash=feature.manifest_hash,
        price_manifest_hash=price.manifest_hash,
        window=WINDOW,
        as_of=ds.SIM,
        research_price=research_price,
    )
    return catalog, pair, feature, price, machinery


def test_a_v3_pair_is_evaluable_without_loading_its_manifests(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, pair, _, _, _ = _sealed_case(world)

    def unexpected_load(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("evaluable must use the declaration and cutoff only")

    monkeypatch.setattr("research.loop.dataset_source.load_any_manifest", unexpected_load)
    assert "after this round's cutoff" in (pair.evaluable(END - timedelta(microseconds=1)) or "")
    assert pair.evaluable(ds.SIM) is None
    assert pair.binding() == {}


def test_a_v3_pair_releases_bars_runs_signals_and_binds_v3_evidence(
    world: World,
) -> None:
    _, pair, feature, price, _ = _sealed_case(world)
    evaluation = SealedEvaluation(FAMILY, SealedWindow(*WINDOW))

    bars = pair.release(evaluation)

    assert evaluation.taken("bars")
    assert bars
    assert all(WINDOW[0] <= bar.interval_start < bar.interval_end <= WINDOW[1] for bar in bars)
    before_signals = pair.binding()
    assert before_signals["manifest_content_hash"] == price.manifest_hash
    assert before_signals["dataset_bars"].bars == bars
    assert before_signals["manifest_pair"].feature_manifest_hash == feature.manifest_hash
    assert before_signals["manifest_pair"].price_manifest_hash == price.manifest_hash
    assert before_signals["feature_manifest_hashes"] == ()

    provider = BarVolumeSumProvider((FEATURE,))
    signals = pair.signals(provider, FEATURE, bars)

    assert len(signals) == len(bars)
    assert all(signal.instrument == "BTC-USDT" for signal in signals)
    assert pair.binding()["feature_manifest_hashes"] == (feature.manifest_hash,)


@pytest.mark.parametrize("v2_side", ["feature", "price"])
def test_a_v2_v3_mixed_sealed_pair_is_refused(
    world: World, v2_side: str
) -> None:
    feature_pit = v3.spec_of(world, interval=v3.INTERVAL)
    price_pit = v3.spec_of(world)
    v2_feature = rt.build(world, feature_pit, data_type="klines_1m", window=WINDOW)
    v2_price = rt.build(world, price_pit, data_type="klines_1m", window=WINDOW)
    machinery = v3.v3(world)
    v3_feature = machinery.build(feature_pit, window=WINDOW)
    v3_price = machinery.build(price_pit, window=WINDOW)
    builder = world.builder()
    research_price = load_any_manifest(builder, v3_price.manifest_hash, machinery.verifier)
    catalog = DatasetCatalog(
        adapter=world.h.adapter,
        storage=world.h.storage,
        builder=builder,
        evidence_verifier=machinery.verifier,
    )
    if v2_side == "feature":
        feature_hash, price_hash = v2_feature.manifest.content_hash(), v3_price.manifest_hash
    else:
        feature_hash, price_hash = v3_feature.manifest_hash, v2_price.manifest.content_hash()
    pair = SealedDatasetPair(
        catalog,
        symbol="BTC-USDT",
        feature_manifest_hash=feature_hash,
        price_manifest_hash=price_hash,
        window=WINDOW,
        as_of=ds.SIM,
        research_price=research_price,
    )

    with pytest.raises(SealedDataRefused, match="never one chain"):
        pair.release(SealedEvaluation(FAMILY, SealedWindow(*WINDOW)))
