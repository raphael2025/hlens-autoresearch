"""``VerifiedManifestCache.load_any``: v3 evidence manifests next to v2 ones (ADR-0077 §8.2).

A v3 proof (``StreamingEvidenceVerifier`` via ``ManifestStore.load_any``) is keyed by the builder,
the evidence verifier object and the content hash, bound to the manifest's dataset (chunk)
snapshot, and reused only while every table the streaming verifier reads at its head is unchanged.
A v2 hash through ``load_any`` is the v2 entry itself. Verifications are counted by wrapping the
real ``verify_evidence_manifest`` (it still runs). Same SQLite ``World`` as the other bar tests;
real v3 builds (``v3_support``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from core.contracts.universe import ResearchDatasetEvidenceManifest
from infrastructure.bars.dataset import backtest_bars_from_dataset
from infrastructure.bars.verified import (
    VerifiedManifestCache,
    load_verified_any,
    load_verified_manifest,
    verification_head_tables,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset.builder import DatasetBuildSummary, DatasetBuilt
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.feature.dataset import DatasetBindingError, load_any_manifest
from tests.infrastructure.bars import v3_support as v
from tests.infrastructure.bars.test_manifest_cache import (
    _another_manifest_row,
    _copy_head_row,
    _Recording,
)
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path / "w") as opened:
        yield opened


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> list[StreamingEvidenceVerifier]:
    """The verifier of every real ``verify_evidence_manifest`` call, in order."""
    calls: list[StreamingEvidenceVerifier] = []
    real = StreamingEvidenceVerifier.verify_evidence_manifest

    def counting(
        self: StreamingEvidenceVerifier,
        manifest: ResearchDatasetEvidenceManifest,
        *,
        manifested: bool,
    ) -> None:
        calls.append(self)
        real(self, manifest, manifested=manifested)

    monkeypatch.setattr(StreamingEvidenceVerifier, "verify_evidence_manifest", counting)
    return calls


class _World:
    """One world with a v2 and a v3 price dataset of the same spec, plus a spare interval spec."""

    def __init__(self, w: World) -> None:
        v.ingest(w)
        self.spec = v.spec_of(w)
        self.interval_spec = v.spec_of(w, interval=v.INTERVAL)
        self.machinery = v.v3(w)
        self.built: DatasetBuilt = rt.build(
            w, self.spec, data_type="klines_1m", window=v.DAY_WINDOW
        )
        self.summary: DatasetBuildSummary = self.machinery.build(self.spec)


def _world(w: World, verified: list[StreamingEvidenceVerifier]) -> _World:
    made = _World(w)
    verified.clear()  # the build's own persist verification is not a load
    return made


def _bars(w: World, builder: Any, manifest_hash: str, verifier: Any, cache: Any) -> Any:
    return backtest_bars_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=builder,
        manifest_content_hash=manifest_hash,
        manifest_cache=cache,
        evidence_verifier=verifier,
    )


# ---------------------------------------------------------------------------------------------
# hits


def test_a_v3_hit_returns_the_same_manifest_without_a_second_verification(
    w: World, verified: list[StreamingEvidenceVerifier]
) -> None:
    world = _world(w, verified)
    genuine, verifier = world.summary.manifest_hash, world.machinery.verifier
    builder, cache = w.builder(), VerifiedManifestCache()
    first = cache.load_any(builder, genuine, verifier)
    assert first == world.summary.manifest and verified == [verifier]
    assert cache.load_any(builder, genuine, verifier) is first
    assert load_verified_any(builder, genuine, cache, verifier) is first
    assert len(verified) == 1 and len(cache) == 1
    stats = cache.stats
    assert (stats.hits, stats.misses, stats.stored, stats.uncacheable) == (2, 1, 1, 0)
    # the bar entry point reuses the proof: same bars, still one verification
    with_cache = _bars(w, builder, genuine, verifier, cache)
    assert len(verified) == 1
    assert with_cache == _bars(w, builder, genuine, verifier, None)
    assert len(verified) == 2  # no cache: the load verifies


def test_a_v2_hash_through_load_any_is_the_v2_entry(
    w: World, verified: list[StreamingEvidenceVerifier]
) -> None:
    world = _world(w, verified)
    v2_hash = world.built.manifest.content_hash()
    builder, cache = w.builder(), VerifiedManifestCache()
    first = cache.load_any(builder, v2_hash, world.machinery.verifier)
    assert first == world.built.manifest
    assert cache.load(builder, v2_hash) is first  # the same v2 entry, found by the v2 key
    assert load_verified_manifest(builder, v2_hash, cache) is first
    assert load_verified_any(builder, v2_hash, cache, None) is first
    assert verified == [] and len(cache) == 1  # the streaming verifier never ran


def test_v2_and_v3_proofs_live_side_by_side(
    w: World, verified: list[StreamingEvidenceVerifier]
) -> None:
    world = _world(w, verified)
    verifier = world.machinery.verifier
    builder, cache = w.builder(), VerifiedManifestCache()
    v2 = cache.load_any(builder, world.built.manifest.content_hash(), verifier)
    v3 = cache.load_any(builder, world.summary.manifest_hash, verifier)
    assert len(cache) == 2 and len(verified) == 1
    assert cache.load_any(builder, world.built.manifest.content_hash(), verifier) is v2
    assert cache.load_any(builder, world.summary.manifest_hash, verifier) is v3
    assert len(verified) == 1


# ---------------------------------------------------------------------------------------------
# misses


def _another_v3_manifest(w: World, world: _World) -> None:
    """A second, genuine v3 dataset: the v3 manifest table and the chunk table move on."""
    world.machinery.build(world.interval_spec)


MOVES: dict[str, Callable[[World, _World], None]] = {
    "v2-manifests": lambda w, world: _another_manifest_row(w, world.built),
    "v3-manifests": _another_v3_manifest,
    "chunk-table": lambda w, _: _copy_head_row(w, DATASET_SELECTION_CHUNKS, "moved-chunks"),
}


@pytest.mark.parametrize("move", sorted(MOVES))
def test_a_new_head_of_a_table_the_streaming_verifier_reads_misses(
    w: World, verified: list[StreamingEvidenceVerifier], move: str
) -> None:
    world = _world(w, verified)
    genuine, verifier = world.summary.manifest_hash, world.machinery.verifier
    builder, cache = w.builder(), VerifiedManifestCache()
    assert cache.load_any(builder, genuine, verifier) == world.summary.manifest
    MOVES[move](w, world)
    verified.clear()  # a second build verifies its own manifest on persist
    again = cache.load_any(builder, genuine, verifier)  # a miss: the full verified load
    assert again == world.summary.manifest and verified == [verifier]
    assert cache.load_any(builder, genuine, verifier) is again  # re-proven at the new head
    assert len(verified) == 1
    assert (cache.stats.misses, cache.stats.stored) == (2, 2)


def test_another_verifier_or_builder_object_misses(
    w: World, verified: list[StreamingEvidenceVerifier]
) -> None:
    world = _world(w, verified)
    genuine = world.summary.manifest_hash
    first, cache = w.builder(), VerifiedManifestCache()
    cache.load_any(first, genuine, world.machinery.verifier)
    other = v.v3(w).verifier  # the same catalog, another verifier object
    assert cache.load_any(first, genuine, other) == world.summary.manifest
    second = w.builder()
    assert cache.load_any(second, genuine, other) == world.summary.manifest
    assert verified == [world.machinery.verifier, other, other]
    assert len(cache) == 3


# ---------------------------------------------------------------------------------------------
# refusals stay refusals


def test_a_v3_hash_forged_into_the_v2_table_is_refused_with_the_cache_on(
    w: World, verified: list[StreamingEvidenceVerifier]
) -> None:
    world = _world(w, verified)
    genuine, verifier = world.summary.manifest_hash, world.machinery.verifier
    builder, cache = w.builder(), VerifiedManifestCache()
    cache.load_any(builder, genuine, verifier)
    row = dict(rt.manifest_row(world.built.manifest), manifest_content_hash=genuine)
    w.h.forge_rows(DATASET_MANIFESTS, [row], batch_id="forged-v2-under-v3")
    for _ in range(2):
        with pytest.raises(CatalogIntegrityError, match="both"):
            cache.load_any(builder, genuine, verifier)
        with pytest.raises(CatalogIntegrityError, match="both"):
            _bars(w, builder, genuine, verifier, cache)
    assert cache.stats.hits == 0 and cache.stats.stored == 1


def test_a_verifier_of_another_catalog_is_uncacheable_and_refused(
    w: World, tmp_path: Path, verified: list[StreamingEvidenceVerifier]
) -> None:
    world = _world(w, verified)
    cache = VerifiedManifestCache()
    with ds.sqlite_world(tmp_path / "other") as other:
        foreign = v.v3(other).verifier
        with pytest.raises(DatasetBindingError, match="another catalog"):
            cache.load_any(w.builder(), world.summary.manifest_hash, foreign)
    assert len(cache) == 0 and cache.stats.uncacheable == 1 and verified == []


def test_a_v3_verification_that_fails_is_not_stored(
    w: World, verified: list[StreamingEvidenceVerifier], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(w, verified)
    genuine, verifier = world.summary.manifest_hash, world.machinery.verifier
    builder, cache = w.builder(), VerifiedManifestCache()

    def refuses(self: Any, manifest: Any, *, manifested: bool) -> None:
        raise CatalogIntegrityError("does not prove")

    with monkeypatch.context() as patched:
        patched.setattr(StreamingEvidenceVerifier, "verify_evidence_manifest", refuses)
        with pytest.raises(CatalogIntegrityError, match="does not prove"):
            cache.load_any(builder, genuine, verifier)
    assert len(cache) == 0
    assert cache.load_any(builder, genuine, verifier) == world.summary.manifest
    assert len(cache) == 1


# ---------------------------------------------------------------------------------------------
# the key covers every head the streaming verifier reads


def test_the_streaming_verifier_reads_no_other_table_at_its_head(w: World) -> None:
    """If this fails, a v3 verified load gained an unpinned read: ``verification_head_tables``
    (the v3 proof's heads) must grow with it, or the cache could serve a stale proof."""
    world = _World(w)
    recording = _Recording(w.h.adapter)
    machinery = v.v3(w, recording)
    builder = v.builder_over(w, recording)
    manifest = load_any_manifest(builder, world.summary.manifest_hash, machinery.verifier)
    assert manifest == world.summary.manifest
    reads = recording.head_reads
    assert DATASET_EVIDENCE_MANIFESTS.table in reads  # the store's own row read is seen
    assert reads <= set(verification_head_tables(world.summary.manifest.dataset.table))
