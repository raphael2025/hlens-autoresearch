"""``DatasetIngestStage`` over v3 evidence manifests (ADR-0077; C1-CONSUMERS — data access only).

A ``DatasetCatalog`` carrying the builder catalog's ``StreamingEvidenceVerifier`` accepts rounds
declaring v3 manifest hashes: the source identity stays the declared content hashes, and the round
reads the same bars, feature observations, decision grid and feature values as a round declaring
the v2 manifests of the same world (only the manifest hashes, and what binds them, differ). A v2
round with the verifier is exactly the v2 round; a v3 round without it is refused by the store.
Nothing here resolves ACTIVE / source / metric authority (ADR-0080 BLOCKED) or schedules rounds.
Same SQLite dataset ``World`` as the bar tests; real v3 builds (``tests.infrastructure.bars
.v3_support``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.worker.loop import LifecycleGuard, RoundContext, StageResult
from core.contracts.universe import ResearchDatasetManifest
from infrastructure.bars.pair import PAIR_RULE_V3_HASH, ManifestPairError, pair_hash_of
from infrastructure.bars.verified import VerifiedManifestCache
from infrastructure.dataset.manifests import ManifestFormError
from infrastructure.feature.dataset import load_any_manifest
from plugins.features import BarVolumeSumProvider
from research.loop import dataset_source
from research.loop.dataset_source import (
    DatasetCatalog,
    DatasetIngestStage,
    DatasetRound,
    DatasetSegment,
)
from tests import factories
from tests.infrastructure.bars import v3_support as v
from tests.infrastructure.bars.test_manifest_pair_v3 import _Chain
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World

BTC = "BTC-USDT"
MINUTE = timedelta(minutes=1)
FEATURE = BarVolumeSumProvider.spec(2, available_lag=MINUTE)
#: Keys of the ingest summary that describe the data (not which manifests bound it).
DATA_KEYS = (
    "source",
    "symbol",
    "pair_rule_hash",
    "sealed_manifest_hash",
    "sealed_window",
    "data_window",
    "price_view",
    "price_cutoff",
    "latest_available_time",
    "feature_observations",
    "research_bars",
    "accumulated_research_bars",
    "sealed_bars_withheld",
    "decision_times",
)


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _chain(w: World) -> _Chain:
    v.ingest(w)
    return _Chain(w)


def _catalog(w: World, chain: _Chain | None = None, **kwargs: Any) -> DatasetCatalog:
    verifier = None if chain is None else chain.machinery.verifier
    return DatasetCatalog(
        w.h.adapter, w.h.storage, w.builder(), evidence_verifier=verifier, **kwargs
    )


def _run(catalog: DatasetCatalog, hashes: tuple[str, str]) -> StageResult:
    """Round 0 of a loop over the default test Profile (research window 2020 .. 2024), cutoff at
    the price view."""
    stage = DatasetIngestStage(
        catalog,
        factories.validation_profile(),
        symbol=BTC,
        rounds=(DatasetRound(*hashes),),
        compute_seconds=Decimal(1),
        decision_step=MINUTE,
        decision_warmup=timedelta(0),
        label_horizon=MINUTE,
    )
    context = RoundContext(
        loop_id="dataset_v3_ingest",
        round_index=0,
        seed=0,
        as_of=ds.SIM,
        _guard=LifecycleGuard("dataset-v3-test"),
    )
    return stage.run(context)


def _segment(result: StageResult) -> DatasetSegment:
    segment = result.artifacts["segment"]
    assert isinstance(segment, DatasetSegment)
    return segment


# ---------------------------------------------------------------------------------------------
# a v3 round reads the v2 round's data


def test_a_v3_round_reads_the_data_of_the_v2_round(w: World) -> None:
    chain = _chain(w)
    v2, got = _run(_catalog(w), chain.v2_hashes), _run(_catalog(w, chain), chain.v3_hashes)
    assert set(got.summary) == set(v2.summary)  # the summary's shape is unchanged
    for key in DATA_KEYS:
        assert got.summary[key] == v2.summary[key], key
    feature, price = chain.v3_hashes
    assert (got.summary["feature_manifest_hash"], got.summary["price_manifest_hash"]) == (
        feature,
        price,
    )
    assert got.summary["manifest_pair_hash"] == pair_hash_of(
        feature, price, rule_hash=PAIR_RULE_V3_HASH
    )
    ours, theirs = _segment(got), _segment(v2)
    assert ours.prices.bars == theirs.prices.bars and len(ours.prices.bars) == v.BARS
    assert ours.observations == theirs.observations
    assert ours.decision_times == theirs.decision_times
    assert ours.manifest_content_hash == price
    assert ours.dataset_snapshots(ds.SIM) == (
        chain.v3_feature.manifest.dataset,
        chain.v3_price.manifest.dataset,
    )


def test_a_v3_round_computes_the_v2_rounds_feature_values(w: World) -> None:
    chain = _chain(w)
    ours = _segment(_run(_catalog(w, chain), chain.v3_hashes))
    theirs = _segment(_run(_catalog(w), chain.v2_hashes))
    provider = BarVolumeSumProvider((FEATURE,))
    [(our_request, our_result)], our_signals, _ = ours.feature_runs(
        provider, FEATURE, chunk=1, cache={}
    )
    [(their_request, their_result)], their_signals, _ = theirs.feature_runs(
        provider, FEATURE, chunk=1, cache={}
    )
    assert our_request.manifest_content_hash == chain.v3_feature.manifest_hash
    assert our_request.observations == their_request.observations
    assert our_request.evaluation_times == their_request.evaluation_times
    assert our_result.values == their_result.values
    assert our_signals == their_signals


def test_v2_manifest_reselection_passes_the_builder_scratch_path(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(w)
    seen: list[Path] = []

    class Selection:
        def require_no_conflict(self) -> None:
            pass

    class RecordingPitSelector:
        def __init__(
            self, adapter: Any, storage: Any, *, canonical_scratch_directory: Path
        ) -> None:
            del adapter, storage
            seen.append(canonical_scratch_directory)

        def select(self, *_args: Any) -> object:
            return Selection()

    monkeypatch.setattr(dataset_source, "PitSelector", RecordingPitSelector)
    monkeypatch.setattr(dataset_source, "bar_observations", lambda *_args: ())
    feature = ResearchDatasetManifest.model_construct(point_in_time=object())
    observations = dataset_source._dataset_observations(catalog, feature, BTC, *v.DAY_WINDOW)

    assert observations == ()
    assert seen == [catalog.builder.canonical_scratch_directory]


def test_a_v3_round_with_the_cache_reads_the_same(w: World) -> None:
    chain = _chain(w)
    plain = _run(_catalog(w, chain), chain.v3_hashes)
    cached = _run(_catalog(w, chain, manifest_cache=VerifiedManifestCache()), chain.v3_hashes)
    assert cached.summary == plain.summary
    assert _segment(cached).prices == _segment(plain).prices


def test_v3_membership_and_lineage_tables_read_the_evidence_streams(w: World) -> None:
    chain = _chain(w)
    v2_catalog, v3_catalog = _catalog(w), _catalog(w, chain)
    verifier = chain.machinery.verifier
    v2_price = load_any_manifest(w.builder(), chain.v2_hashes[1])
    v3_price = load_any_manifest(w.builder(), chain.v3_hashes[1], verifier)
    for catalog, manifest in ((v2_catalog, v2_price), (v3_catalog, v3_price)):
        dataset_source._require_member(catalog, manifest, BTC, "research pair")
    assert dataset_source._lineage_tables(v3_catalog, v3_price) == (
        dataset_source._lineage_tables(v2_catalog, v2_price)
    )


# ---------------------------------------------------------------------------------------------
# v2 unchanged; v3 needs its verifier; forms never mix


def test_a_v2_round_with_an_evidence_verifier_is_exactly_the_v2_round(w: World) -> None:
    chain = _chain(w)
    plain = _run(_catalog(w), chain.v2_hashes)
    with_verifier = _run(_catalog(w, chain), chain.v2_hashes)
    assert with_verifier.summary == plain.summary
    assert _segment(with_verifier).prices == _segment(plain).prices
    assert _segment(with_verifier).observations == _segment(plain).observations


def test_a_v3_round_without_the_evidence_verifier_is_refused(w: World) -> None:
    chain = _chain(w)
    with pytest.raises(ManifestFormError):
        _run(_catalog(w), chain.v3_hashes)


def test_a_round_mixing_v2_and_v3_manifests_is_refused(w: World) -> None:
    chain = _chain(w)
    with pytest.raises(ManifestPairError, match="never one chain"):
        _run(_catalog(w, chain), (chain.v2_hashes[0], chain.v3_hashes[1]))
