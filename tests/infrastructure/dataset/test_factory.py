"""``open_dataset_pipeline`` / ``bind_profile`` (ADR-0101 D3 / D4): wiring, isolation, lifecycle."""

from __future__ import annotations

import gc
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from core.contracts.universe import ResearchDatasetEvidenceManifest
from infrastructure.catalog import PHASE1_REGISTRY
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_EVIDENCE_MANIFESTS
from infrastructure.dataset import factory
from infrastructure.dataset.builder import DatasetEvidenceRequest, DatasetQualityError
from infrastructure.dataset.factory import (
    QUALITY_SCRATCH_DIRNAME,
    bind_profile,
    bounded_quality_params,
    build_pipeline,
    market_data_origin,
    open_dataset_pipeline,
    open_quality_scratch,
)
from infrastructure.dataset.pinning import pin_dataset_pit_spec
from infrastructure.dataset.profile import DatasetBuildProfile
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.quality.identity_registry import CanonicalV3IdentityRegistry
from infrastructure.quality.scratch import local_storage_roots_overlap
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, START, World
from tests.infrastructure.dataset.entry_support import (
    make_profile,
    patch_catalog,
    profile_document,
    settings_for,
    v3_ready,
)


def _request(w: World) -> DatasetEvidenceRequest:
    pit = pin_dataset_pit_spec(
        w.h.adapter,
        name="test.factory",
        version="1.0.0",
        simulation_time=ds.SIM,
        knowledge_cutoff=ds.SIM,
        listing_assumption=False,
    )
    return DatasetEvidenceRequest(FIRST_SLICE_UNIVERSE, pit, "agg_trades", START, END)


# ------------------------------------------------------------------------- pure wiring


def test_quality_params_come_from_the_profile_over_the_given_scratch(
    tmp_path: Path,
) -> None:
    _, profile = make_profile(tmp_path)
    scratch = open_quality_scratch(tmp_path / "scratch")
    try:
        params = bounded_quality_params(profile, scratch)
    finally:
        scratch.close()
    quality = profile.quality
    assert params.scratch_storage is scratch
    assert params.stream_limits == quality.stream_limits
    assert params.run_limits == quality.run_limits
    assert (params.run_capacity, params.merge_fanout) == (
        quality.run_capacity,
        quality.merge_fanout,
    )
    assert params.max_run_object_bytes == quality.max_run_object_bytes
    assert params.identity_registry == CanonicalV3IdentityRegistry(
        max_identity_bytes=quality.max_identity_bytes
    )


def test_quality_scratch_is_a_separate_adapter_under_the_canonical_scratch_root(
    w: World,
) -> None:
    settings = settings_for(w)
    scratch = open_quality_scratch(settings.canonical_scratch_path)
    try:
        root = settings.canonical_scratch_path / QUALITY_SCRATCH_DIRNAME
        assert QUALITY_SCRATCH_DIRNAME == "dataset-quality"
        assert Path(scratch.warehouse_uri.removeprefix("file://")) == root / "warehouse"
        assert Path(scratch.staging_uri.removeprefix("file://")) == root / "staging"
        assert (root / "warehouse").is_dir() and (root / "staging").is_dir()
        assert scratch is not w.h.storage
        assert not local_storage_roots_overlap(w.h.storage, scratch)
    finally:
        scratch.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://data-api.binance.vision", "https://data-api.binance.vision"),
        ("https://market.test:8443", "https://market.test:8443"),
    ],
)
def test_market_data_origin_drops_the_rendered_root_slash(
    w: World, configured: str, expected: str
) -> None:
    from pydantic import AnyHttpUrl

    settings = settings_for(w).model_copy(
        update={"binance_market_data_base_url": AnyHttpUrl(configured)}
    )
    assert str(settings.binance_market_data_base_url).endswith("/")
    assert market_data_origin(settings) == expected


def test_default_settings_origin_is_accepted_by_the_listing_identity() -> None:
    from infrastructure.revision.exchange_info_identity import _check_origin

    settings = Settings(_env_file=None, catalog_uri="postgresql://u@h/d")  # type: ignore[call-arg, arg-type]
    assert _check_origin(market_data_origin(settings)) == "https://data-api.binance.vision"


# ------------------------------------------------------------------------- lifecycle


@contextmanager
def _watched_scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[LocalFileStorageAdapter]]:
    seen: list[LocalFileStorageAdapter] = []
    real = factory.open_quality_scratch

    def watching(path: Path) -> LocalFileStorageAdapter:
        scratch = real(path)
        seen.append(scratch)
        return scratch

    monkeypatch.setattr(factory, "open_quality_scratch", watching)
    yield seen


def test_open_pipeline_opens_catalog_storage_and_scratch_and_closes_them(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    opened_catalogs = patch_catalog(monkeypatch, w)
    with _watched_scratch(monkeypatch) as scratches:
        with open_dataset_pipeline(settings_for(w), profile) as opened:
            assert opened.adapter is w.h.adapter and opened.profile is profile
            assert isinstance(opened.storage, LocalFileStorageAdapter)
            assert opened.storage is not w.h.storage  # its own adapter, opened from Settings
            [scratch] = scratches
            assert not opened.storage.closed and not scratch.closed
            assert opened.pipeline.builder.rule == profile.rule
            storage = opened.storage
        assert storage.closed and scratch.closed
    assert opened_catalogs == ["hlens"]


def test_open_pipeline_closes_everything_when_the_body_fails(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)
    with _watched_scratch(monkeypatch) as scratches:
        with pytest.raises(RuntimeError, match="boom"):
            with open_dataset_pipeline(settings_for(w), profile) as opened:
                storage = opened.storage
                raise RuntimeError("boom")
        assert isinstance(storage, LocalFileStorageAdapter)
        assert storage.closed and scratches[0].closed


def test_open_pipeline_passes_the_phase1_registry_and_the_settings_to_the_catalog(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    calls: list[tuple[Any, Any]] = []

    def opener(settings: Any, registry: Any) -> Any:
        from contextlib import nullcontext

        calls.append((settings, registry))
        return nullcontext(w.h.adapter)

    monkeypatch.setattr(factory, "open_postgres_catalog_adapter", opener)
    settings = settings_for(w)
    with open_dataset_pipeline(settings, profile):
        pass
    assert calls == [(settings, PHASE1_REGISTRY)]


def test_a_catalog_that_does_not_open_leaves_nothing_open(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)

    def failing(settings: Any, registry: Any) -> Any:
        raise OSError("no catalog")

    monkeypatch.setattr(factory, "open_postgres_catalog_adapter", failing)
    with pytest.raises(OSError, match="no catalog"):
        with open_dataset_pipeline(settings_for(w), profile):
            pytest.fail("must not open")


# ------------------------------------------------------------------------- the real pipeline


@pytest.fixture
def ready(w: World, tmp_path: Path) -> World:
    v3_ready(w, tmp_path)
    return w


def test_the_pipeline_builds_replays_and_reloads_a_v3_dataset(
    ready: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = ready
    _, profile = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)
    with open_dataset_pipeline(settings_for(w), profile) as opened:
        request = _request(w)
        summary = opened.pipeline.build(request)
        assert summary.row_count > 0 and summary.chunk_count >= 1
        assert not summary.manifest_replayed
        assert opened.pipeline.load_manifest(summary.manifest_hash) == summary.manifest
        assert isinstance(summary.manifest, ResearchDatasetEvidenceManifest)
        assert summary.manifest.rule.rule_hash == profile.rule.rule_hash
        again = opened.pipeline.build(request)
        assert again.replayed and again.manifest_hash == summary.manifest_hash
    assert w.h.head(DATASET_EVIDENCE_MANIFESTS.table) is not None
    quality_root = w.h.canonical_scratch_directory / QUALITY_SCRATCH_DIRNAME
    assert (quality_root / "warehouse").is_dir() and (quality_root / "staging").is_dir()


def test_quality_scratch_must_not_alias_the_evidence_storage(ready: World, tmp_path: Path) -> None:
    _, profile = make_profile(tmp_path)
    alias = LocalFileStorageAdapter(ready.h.storage.warehouse_uri, ready.h.storage.staging_uri)
    try:
        pipeline = build_pipeline(
            ready.h.adapter,
            ready.h.storage,
            alias,
            profile=profile,
            canonical_scratch_directory=ready.h.canonical_scratch_directory,
            market_data_base_url=ds.ORIGIN,
        )
        with pytest.raises(DatasetQualityError, match="roots must not overlap"):
            pipeline.build(_request(ready))
    finally:
        alias.close()


# ------------------------------------------------------------------------- bind_profile


def _built(
    w: World, profile: DatasetBuildProfile, monkeypatch: pytest.MonkeyPatch
) -> ResearchDatasetEvidenceManifest:
    patch_catalog(monkeypatch, w)
    with open_dataset_pipeline(settings_for(w), profile) as opened:
        return opened.pipeline.build(_request(w)).manifest


def test_bound_factory_returns_a_verifier_that_proves_what_the_pipeline_built(
    ready: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    manifest = _built(ready, profile, monkeypatch)
    verifier = bind_profile(profile, settings=settings_for(ready))(ready.h.adapter, ready.h.storage)
    assert isinstance(verifier, StreamingEvidenceVerifier)
    assert verifier.adapter is ready.h.adapter
    assert verifier.builder.rule == profile.rule
    verifier.verify_evidence_manifest(manifest, manifested=True)


def test_a_verifier_bound_to_another_rule_rejects_the_manifest(
    ready: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    manifest = _built(ready, profile, monkeypatch)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    _, other = make_profile(other_dir, rule={**profile_document()["rule"], "chunk_rows": 3})
    assert other.rule.rule_hash != profile.rule.rule_hash
    verifier = bind_profile(other, settings=settings_for(ready))(ready.h.adapter, ready.h.storage)
    with pytest.raises(CatalogIntegrityError, match="not the verifier's rule"):
        verifier.verify_evidence_manifest(manifest, manifested=True)


def test_bound_factory_reads_settings_from_the_environment_when_called(
    ready: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    manifest = _built(ready, profile, monkeypatch)
    settings = settings_for(ready)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("HLENS_CATALOG_URI", "postgresql://u:p@db.invalid/d")
    monkeypatch.setenv("HLENS_WAREHOUSE_URI", settings.warehouse_uri)
    monkeypatch.setenv("HLENS_STAGING_URI", settings.staging_uri)
    monkeypatch.setenv("HLENS_CANONICAL_SCRATCH_URI", settings.canonical_scratch_uri)
    monkeypatch.setenv("HLENS_BINANCE_MARKET_DATA_BASE_URL", ds.ORIGIN)
    factory_ = bind_profile(profile)  # nothing is read yet
    verifier = factory_(ready.h.adapter, ready.h.storage)
    verifier.verify_evidence_manifest(manifest, manifested=True)


def test_the_verifiers_quality_scratch_is_closed_when_it_is_collected(
    ready: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile = make_profile(tmp_path)
    with _watched_scratch(monkeypatch) as scratches:
        verifier = bind_profile(profile, settings=settings_for(ready))(
            ready.h.adapter, ready.h.storage
        )
        [scratch] = scratches
        assert not scratch.closed
        del verifier
        gc.collect()
        assert scratch.closed
