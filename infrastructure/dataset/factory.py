"""Open the v3 Dataset pipeline and its verifier factory (ADR-0101 D3 / D4).

``open_dataset_pipeline`` is the context manager every production entry (the CLI of this package,
a future worker job) uses: it opens the PostgreSQL catalog, the evidence warehouse and the
Quality scratch, hands back the wired ``DatasetBuildPipeline`` and closes everything in reverse.
``bind_profile`` returns the ``factory(adapter, storage) -> StreamingEvidenceVerifier`` that
``research.operations`` loads through ``--authority-evidence-verifier``.

The Quality scratch (D4) is ``<settings.canonical_scratch_path>/dataset-quality``, wrapped in its
own ``LocalFileStorageAdapter``: ``Settings`` already keeps the Canonical scratch root disjoint
from the warehouse and staging roots, so the scratch never aliases the evidence storage that the
Quality join checks it against. Every limit comes from the profile; ``Settings`` is not changed.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.contracts.storage import StorageAdapter
from infrastructure.catalog import PHASE1_REGISTRY
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import DATASET_SELECTION_CHUNKS
from infrastructure.dataset.builder import (
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.pipeline import DatasetBuildPipeline
from infrastructure.dataset.profile import DatasetBuildProfile
from infrastructure.dataset.quality import BoundedQualityEvidence, BoundedQualitySourceParams
from infrastructure.dataset.sources import dataset_evidence_sources
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.quality.identity_registry import CanonicalV3IdentityRegistry
from infrastructure.revision.store import RevisionCatalog
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter

__all__ = [
    "QUALITY_SCRATCH_DIRNAME",
    "OpenedDatasetPipeline",
    "bind_profile",
    "market_data_origin",
    "build_pipeline",
    "bounded_quality_params",
    "open_dataset_pipeline",
    "open_quality_scratch",
]

#: The Quality scratch directory under the Canonical scratch root (ADR-0101 D4).
QUALITY_SCRATCH_DIRNAME: Final = "dataset-quality"


@dataclass(frozen=True, slots=True)
class OpenedDatasetPipeline:
    """The open resources of one entry run; valid only inside ``open_dataset_pipeline``."""

    adapter: RevisionCatalog
    storage: StorageAdapter
    pipeline: DatasetBuildPipeline
    profile: DatasetBuildProfile


def market_data_origin(settings: Settings) -> str:
    """The configured market-data origin as the listing readers require it: no trailing slash.

    ``AnyHttpUrl`` renders ``https://host`` as ``https://host/``; the Listing / exchangeInfo
    identities accept exactly ``https://host[:port]`` (the one concession is that bare root path).
    """
    return str(settings.binance_market_data_base_url).removesuffix("/")


def open_quality_scratch(canonical_scratch_path: Path) -> LocalFileStorageAdapter:
    """The isolated Quality scratch adapter under the Canonical scratch root (created on demand)."""
    root = canonical_scratch_path / QUALITY_SCRATCH_DIRNAME
    return LocalFileStorageAdapter((root / "warehouse").as_uri(), (root / "staging").as_uri())


def bounded_quality_params(
    profile: DatasetBuildProfile, scratch: StorageAdapter
) -> BoundedQualitySourceParams:
    """The Dataset Quality join's bounds, all from ``profile``, over an isolated ``scratch``."""
    quality = profile.quality
    return BoundedQualitySourceParams(
        scratch_storage=scratch,
        stream_limits=quality.stream_limits,
        run_limits=quality.run_limits,
        run_capacity=quality.run_capacity,
        merge_fanout=quality.merge_fanout,
        max_run_object_bytes=quality.max_run_object_bytes,
        identity_registry=CanonicalV3IdentityRegistry(
            max_identity_bytes=quality.max_identity_bytes
        ),
    )


def build_pipeline(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    quality_scratch: StorageAdapter,
    *,
    profile: DatasetBuildProfile,
    canonical_scratch_directory: Path,
    market_data_base_url: str,
) -> DatasetBuildPipeline:
    """The v3 composition root over already-open resources (no resource is owned by the result)."""
    return DatasetBuildPipeline(
        adapter,
        storage,
        rule=profile.rule,
        canonical_scratch_directory=canonical_scratch_directory,
        market_data_base_url=market_data_base_url,
        pit_params=profile.pit,
        universe_params=profile.universe,
        quality_factory=BoundedQualityEvidence,
        quality_params=bounded_quality_params(profile, quality_scratch),
    )


@contextmanager
def open_dataset_pipeline(
    settings: Settings, profile: DatasetBuildProfile
) -> Iterator[OpenedDatasetPipeline]:
    """Open catalog, evidence storage and Quality scratch; close them all on exit.

    The two Dataset tables must already exist in the catalog (the Catalog composition root
    creates them); nothing here creates a table. Open failures propagate as the underlying
    ``CatalogError`` / ``OSError`` / ``ValueError``.
    """
    with ExitStack() as stack:
        storage = stack.enter_context(LocalFileStorageAdapter.from_settings(settings))
        adapter = stack.enter_context(open_postgres_catalog_adapter(settings, PHASE1_REGISTRY))
        scratch = stack.enter_context(open_quality_scratch(settings.canonical_scratch_path))
        pipeline = build_pipeline(
            adapter,
            storage,
            scratch,
            profile=profile,
            canonical_scratch_directory=settings.canonical_scratch_path,
            market_data_base_url=market_data_origin(settings),
        )
        yield OpenedDatasetPipeline(adapter, storage, pipeline, profile)


def bind_profile(
    profile: DatasetBuildProfile, *, settings: Settings | None = None
) -> Callable[[RevisionCatalog, StorageAdapter], StreamingEvidenceVerifier]:
    """The verifier factory ``research.operations`` loads: ``factory(adapter, storage)``.

    The returned verifier re-derives v3 manifests with this profile's rule and bounds, exactly as
    ``DatasetBuildPipeline`` does. ``settings`` default to the environment (read when the factory
    is called, not when it is bound); its Quality scratch adapter is closed when the verifier is
    garbage collected.
    """

    def factory(adapter: RevisionCatalog, storage: StorageAdapter) -> StreamingEvidenceVerifier:
        resolved = Settings() if settings is None else settings  # type: ignore[call-arg]
        scratch = open_quality_scratch(resolved.canonical_scratch_path)
        params = bounded_quality_params(profile, scratch)
        canonical_scratch = resolved.canonical_scratch_path
        origin = market_data_origin(resolved)

        def sources(request: DatasetEvidenceRequest) -> DatasetEvidenceSources:
            return dataset_evidence_sources(
                adapter,
                storage,
                request,
                canonical_scratch_directory=canonical_scratch,
                market_data_base_url=origin,
                pit_params=profile.pit,
                universe_params=profile.universe,
                quality_factory=BoundedQualityEvidence,
                quality_params=params,
            )

        verifier = StreamingEvidenceVerifier(
            adapter,
            builder=DatasetEvidenceBuilder(adapter, storage, rule=profile.rule),
            chunks=IcebergChunkWriter(adapter, DATASET_SELECTION_CHUNKS),
            sources=sources,
        )
        weakref.finalize(verifier, scratch.close)
        return verifier

    return factory
