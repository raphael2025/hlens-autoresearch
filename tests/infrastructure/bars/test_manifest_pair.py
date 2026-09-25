"""Verified pairing of one chain's feature (interval) and price (point) manifests (backlog E1).

``pair_manifests`` loads both manifests through the builder's verifying ``ManifestStore`` and
proves they describe the same market data: same upstream snapshots, the same ADR-0032 choice, the
same instruments over the whole feature interval and the same lineage, with the price view at the
end of the feature interval and one knowledge cutoff. Same SQLite ``World`` fixtures as
``test_dataset_bars.py`` (real stores, mock venue; nothing under test is faked); five bars.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec
from infrastructure.bars import (
    PAIR_RULE_HASH,
    ManifestPair,
    ManifestPairError,
    pair_manifests,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.feature.dataset import DatasetBindingError
from tests.infrastructure.bars.test_dataset_bars import BARS, DAY_WINDOW, TAMPERED, _assumed
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision.rest_store_support import utc

START = utc(2023, 11, 14)
#: A listing observation inside the feature interval (after START, before ds.SIM).
MID_INTERVAL = utc(2023, 11, 16)


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _ingest(w: World) -> None:
    w.listed()
    w.bars(count=BARS)
    w.report("klines_1m")


def _build(w: World, spec: PointInTimeSpec) -> DatasetBuilt:
    return rt.build(w, spec, data_type="klines_1m", window=DAY_WINDOW)


def _feature(
    w: World, *, assumed: bool = True, end: datetime = ds.SIM, **fields: Any
) -> DatasetBuilt:
    spec = w.spec(skip=rt.OWN, interval=(START, end), **fields)
    return _build(w, _assumed(spec) if assumed else spec)


def _price(w: World, *, assumed: bool = True, **fields: Any) -> DatasetBuilt:
    spec = w.spec(skip=rt.OWN, **fields)
    return _build(w, _assumed(spec) if assumed else spec)


def _pair(w: World, feature: DatasetBuilt, price: DatasetBuilt) -> ManifestPair:
    return pair_manifests(
        w.builder(), feature.manifest.content_hash(), price.manifest.content_hash()
    )


# ---------------------------------------------------------------------------------------------
# positive


def test_a_matching_pair_is_bound_by_hash(w: World) -> None:
    _ingest(w)
    feature, price = _feature(w), _price(w)
    pair = _pair(w, feature, price)
    assert pair.feature_manifest_hash == feature.manifest.content_hash()
    assert pair.price_manifest_hash == price.manifest.content_hash()
    assert pair.pair_hash not in (pair.feature_manifest_hash, pair.price_manifest_hash)
    assert _pair(w, feature, price) == pair  # deterministic
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.pair_hash = "0" * 64  # type: ignore[misc]
    assert PAIR_RULE_HASH and len(pair.pair_hash) == 64


def test_a_hand_made_pair_hash_is_refused(w: World) -> None:
    _ingest(w)
    pair = _pair(w, _feature(w), _price(w))
    with pytest.raises(ManifestPairError, match="does not bind"):
        ManifestPair(
            pair.feature_manifest_hash, pair.price_manifest_hash, pair.feature_manifest_hash
        )
    with pytest.raises(ManifestPairError, match="does not bind"):
        ManifestPair(pair.price_manifest_hash, pair.feature_manifest_hash, pair.pair_hash)


def test_both_sides_without_the_adr_0032_assumption_also_pair(w: World) -> None:
    _ingest(w)
    _pair(w, _feature(w, assumed=False), _price(w, assumed=False))


# ---------------------------------------------------------------------------------------------
# refusals: snapshots, instruments, ADR-0032


def test_a_different_upstream_snapshot_is_refused(w: World) -> None:
    _ingest(w)
    feature = _feature(w)
    w.more_trades()  # canonical.trades and the shared Raw REST tables move on: new heads
    w.report("klines_1m")  # the bar reports of the new heads (a build needs them)
    price = _price(w)
    assert feature.manifest.point_in_time.snapshot_bindings != (
        price.manifest.point_in_time.snapshot_bindings
    )
    with pytest.raises(ManifestPairError, match="upstream snapshots differ"):
        _pair(w, feature, price)


def test_a_different_instrument_set_is_refused(w: World) -> None:
    """ETHUSDT halts inside the feature interval: a member for part of it, excluded at its end."""
    w.listed()
    w.listed(ds.ETH_HALT, at=MID_INTERVAL)
    w.bars(count=BARS)
    w.report("klines_1m")
    feature, price = _feature(w), _price(w)
    assert {ds.symbol_of(m) for m in price.manifest.members} == {"BTC-USDT"}
    assert {ds.symbol_of(m) for m in feature.manifest.members} == {"BTC-USDT", "ETH-USDT"}
    with pytest.raises(ManifestPairError, match="part of the feature interval only"):
        _pair(w, feature, price)


@pytest.mark.parametrize("assumed_side", ["feature", "price"])
def test_a_different_adr_0032_choice_is_refused(w: World, assumed_side: str) -> None:
    _ingest(w)
    feature = _feature(w, assumed=assumed_side == "feature")
    price = _price(w, assumed=assumed_side == "price")
    with pytest.raises(ManifestPairError, match=f"ADR-0032 .* {assumed_side} manifest only"):
        _pair(w, feature, price)


# ---------------------------------------------------------------------------------------------
# refusals: the time relation


def test_a_price_view_before_the_end_of_the_feature_interval_is_refused(w: World) -> None:
    _ingest(w)
    feature, price = _feature(w), _price(w, at=ds.SIM - timedelta(days=1))
    with pytest.raises(ManifestPairError, match="not the end of the feature interval"):
        _pair(w, feature, price)


def test_a_feature_interval_ending_before_the_price_view_is_refused(w: World) -> None:
    _ingest(w)
    feature, price = _feature(w, end=ds.SIM - timedelta(days=1)), _price(w)
    with pytest.raises(ManifestPairError, match="not the end of the feature interval"):
        _pair(w, feature, price)


def test_different_knowledge_cutoffs_are_refused(w: World) -> None:
    _ingest(w)
    feature, price = _feature(w), _price(w, cutoff=ds.SIM - timedelta(days=1))
    with pytest.raises(ManifestPairError, match="knowledge cutoff"):
        _pair(w, feature, price)


def test_swapped_roles_are_refused(w: World) -> None:
    _ingest(w)
    feature, price = _feature(w), _price(w)
    with pytest.raises(ManifestPairError, match="feature manifest must be an interval"):
        _pair(w, price, feature)
    with pytest.raises(ManifestPairError, match="price manifest must be a point"):
        _pair(w, feature, feature)


# ---------------------------------------------------------------------------------------------
# refusals: unverified or forged manifests


@pytest.mark.parametrize("side", ["feature", "price"])
@pytest.mark.parametrize("attack", sorted(TAMPERED))
def test_a_manifest_that_does_not_load_is_refused(w: World, attack: str, side: str) -> None:
    _ingest(w)
    feature, price = _feature(w), _price(w)
    bad = TAMPERED[attack](w, feature if side == "feature" else price)
    hashes = {"feature": feature.manifest.content_hash(), "price": price.manifest.content_hash()}
    hashes[side] = bad
    with pytest.raises((DatasetBindingError, CatalogIntegrityError)):
        pair_manifests(w.builder(), hashes["feature"], hashes["price"])


def test_there_is_no_unverified_entry(w: World) -> None:
    """No parameter takes a manifest object; a non-builder verifier is refused."""
    names = set(inspect.signature(pair_manifests).parameters)
    assert names == {"builder", "feature_manifest_hash", "price_manifest_hash"}
    _ingest(w)
    feature, price = _feature(w), _price(w)

    class _Trusting:
        def manifests(self) -> Any:
            return self

        def load(self, content: str) -> Any:
            return (
                feature.manifest if content == feature.manifest.content_hash() else price.manifest
            )

    with pytest.raises(DatasetBindingError, match="DatasetBuilder"):
        pair_manifests(
            _Trusting(),  # type: ignore[arg-type]
            feature.manifest.content_hash(),
            price.manifest.content_hash(),
        )
