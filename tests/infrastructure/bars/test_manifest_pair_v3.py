"""``pair_manifests`` over v3 evidence manifests (ADR-0077; C1-CONSUMERS).

Two v3 manifests pair under their own rule (``PAIR_RULE_V3_HASH``, same ``pair_hash_of`` formula,
a distinct rule text and hash from the v2 ``PAIR_RULE_HASH``), steps 8 - 9 proven by ordered merges
over their evidence streams; a v2 / v3 mix is refused; a v2 pair with an evidence verifier is
exactly the v2 pair (still under ``PAIR_RULE_HASH``). Every refusal checked here is also the v2
path's for the same world. Same SQLite ``World``; real v3 builds (``v3_support``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec
from infrastructure.bars.pair import (
    PAIR_RULE_V3_HASH,
    ManifestPairError,
    pair_hash_of,
    pair_manifests,
)
from infrastructure.bars.verified import VerifiedManifestCache
from infrastructure.dataset.builder import DatasetBuildSummary, DatasetBuilt
from infrastructure.feature.dataset import DatasetBindingError
from tests.infrastructure.bars import v3_support as v
from tests.infrastructure.bars.test_manifest_pair import MID_INTERVAL
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


class _Chain:
    """A feature (interval) and a price (point) dataset of each form over one set of specs."""

    def __init__(
        self,
        w: World,
        *,
        feature_spec: PointInTimeSpec | None = None,
        price_spec: PointInTimeSpec | None = None,
    ) -> None:
        self.w = w
        self.feature_spec = feature_spec or v.spec_of(w, interval=v.INTERVAL)
        self.price_spec = price_spec or v.spec_of(w)
        self.machinery = v.v3(w)
        self.v2_feature: DatasetBuilt = self._v2(self.feature_spec)
        self.v2_price: DatasetBuilt = self._v2(self.price_spec)
        self.v3_feature: DatasetBuildSummary = self.machinery.build(self.feature_spec)
        self.v3_price: DatasetBuildSummary = self.machinery.build(self.price_spec)

    def _v2(self, spec: PointInTimeSpec) -> DatasetBuilt:
        return rt.build(self.w, spec, data_type="klines_1m", window=v.DAY_WINDOW)

    @property
    def v2_hashes(self) -> tuple[str, str]:
        return self.v2_feature.manifest.content_hash(), self.v2_price.manifest.content_hash()

    @property
    def v3_hashes(self) -> tuple[str, str]:
        return self.v3_feature.manifest_hash, self.v3_price.manifest_hash

    def pair(self, feature: str, price: str, **kwargs: Any) -> Any:
        return pair_manifests(
            self.w.builder(), feature, price, evidence_verifier=self.machinery.verifier, **kwargs
        )


def _chain(w: World, **kwargs: Any) -> _Chain:
    v.ingest(w)
    return _Chain(w, **kwargs)


# ---------------------------------------------------------------------------------------------
# positive


def test_a_matching_v3_pair_is_bound_by_its_own_rule(w: World) -> None:
    chain = _chain(w)
    feature, price = chain.v3_hashes
    pair = chain.pair(feature, price)
    assert (pair.feature_manifest_hash, pair.price_manifest_hash) == (feature, price)
    assert pair.pair_hash == pair_hash_of(feature, price, rule_hash=PAIR_RULE_V3_HASH)
    assert chain.pair(feature, price) == pair  # deterministic
    assert chain.pair(feature, price, manifest_cache=VerifiedManifestCache()) == pair
    # the v2 pair of the same world is its own pair (other manifest hashes), still valid
    v2_pair = pair_manifests(w.builder(), *chain.v2_hashes)
    assert v2_pair.pair_hash != pair.pair_hash
    assert v2_pair.pair_hash == pair_hash_of(*chain.v2_hashes)  # the v2 rule, unchanged


def test_a_v2_pair_with_an_evidence_verifier_is_exactly_the_v2_pair(w: World) -> None:
    chain = _chain(w)
    assert chain.pair(*chain.v2_hashes) == pair_manifests(w.builder(), *chain.v2_hashes)


def test_both_v3_sides_without_the_adr_0032_assumption_also_pair(w: World) -> None:
    v.ingest(w)
    chain = _Chain(
        w,
        feature_spec=v.spec_of(w, with_assumption=False, interval=v.INTERVAL),
        price_spec=v.spec_of(w, with_assumption=False),
    )
    chain.pair(*chain.v3_hashes)


# ---------------------------------------------------------------------------------------------
# refusals (each also the v2 path's)


def test_a_v2_v3_mix_is_refused(w: World) -> None:
    chain = _chain(w)
    (v2_feature, v2_price), (v3_feature, v3_price) = chain.v2_hashes, chain.v3_hashes
    for feature, price in ((v2_feature, v3_price), (v3_feature, v2_price)):
        with pytest.raises(ManifestPairError, match="never one chain"):
            chain.pair(feature, price)


def test_swapped_v3_roles_are_refused(w: World) -> None:
    chain = _chain(w)
    feature, price = chain.v3_hashes
    with pytest.raises(ManifestPairError, match="feature manifest must be an interval"):
        chain.pair(price, feature)
    with pytest.raises(ManifestPairError, match="price manifest must be a point"):
        chain.pair(feature, feature)


def test_a_different_v3_instrument_set_is_refused(w: World) -> None:
    """ETHUSDT halts inside the feature interval: a member for part of it, excluded at its end."""
    w.listed()
    w.listed(ds.ETH_HALT, at=MID_INTERVAL)
    w.bars(count=v.BARS)
    w.report("klines_1m")
    chain = _Chain(w)
    with pytest.raises(ManifestPairError, match="part of the feature interval only"):
        pair_manifests(w.builder(), *chain.v2_hashes)
    with pytest.raises(ManifestPairError, match="part of the feature interval only"):
        chain.pair(*chain.v3_hashes)


@pytest.mark.parametrize("assumed_side", ["feature", "price"])
def test_a_different_v3_adr_0032_choice_is_refused(w: World, assumed_side: str) -> None:
    v.ingest(w)
    chain = _Chain(
        w,
        feature_spec=v.spec_of(w, with_assumption=assumed_side == "feature", interval=v.INTERVAL),
        price_spec=v.spec_of(w, with_assumption=assumed_side == "price"),
    )
    for pairing in (
        lambda: pair_manifests(w.builder(), *chain.v2_hashes),
        lambda: chain.pair(*chain.v3_hashes),
    ):
        with pytest.raises(ManifestPairError, match=f"ADR-0032 .* {assumed_side} manifest only"):
            pairing()


def test_a_v3_price_view_before_the_end_of_the_feature_interval_is_refused(w: World) -> None:
    v.ingest(w)
    earlier: datetime = ds.SIM - timedelta(days=1)
    chain = _Chain(w, price_spec=assumed_at(w, earlier))
    with pytest.raises(ManifestPairError, match="not the end of the feature interval"):
        chain.pair(*chain.v3_hashes)


def assumed_at(w: World, at: datetime) -> PointInTimeSpec:
    return v.assumed(w.spec(skip=v.OWN_TABLES, at=at))


def test_a_non_verifier_is_refused(w: World) -> None:
    chain = _chain(w)

    class _Trusting:
        """Not a ``StreamingEvidenceVerifier``: would accept any manifest."""

        def verify_evidence_manifest(self, manifest: Any, *, manifested: bool) -> None:
            return None

    with pytest.raises(DatasetBindingError, match="StreamingEvidenceVerifier"):
        pair_manifests(
            w.builder(),
            *chain.v3_hashes,
            evidence_verifier=_Trusting(),  # type: ignore[arg-type]
        )
