"""Production composition root for bounded v3 Dataset writes (ADR-0077).

Every resource limit is supplied by the caller. This module wires the already implemented
Universe, PIT, Quality, chunk, manifest and verifier components into one Dataset entry point;
it does not select DQ-9 capacities or create Iceberg tables.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path

from core.contracts.storage import StorageAdapter
from core.contracts.universe import EvidenceStream, ResearchDatasetEvidenceManifest
from core.domain.base import Contract
from infrastructure.catalog.phase1_tables import DATASET_SELECTION_CHUNKS
from infrastructure.dataset.builder import (
    DatasetBuildSummary,
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceRule,
    DatasetEvidenceSources,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.manifests import DatasetEvidenceManifestStore
from infrastructure.dataset.quality import (
    BoundedQualityEvidenceFactory,
    BoundedQualitySourceParams,
)
from infrastructure.dataset.sources import (
    UniverseRunParams,
    dataset_evidence_sources,
)
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.selector import PitRunParams
from infrastructure.revision.store import RevisionCatalog

__all__ = ["DatasetBuildPipeline"]


class DatasetBuildPipeline:
    """Build and verify v3 datasets through one fully configured production entry point.

    The caller must provide the accepted Dataset rule and every upstream run / Quality bound.
    ``canonical_scratch_directory`` is the explicit scratch owner used by Canonical replay;
    this pipeline never falls back to a system temporary directory. The two Dataset tables must
    already be registered in the Catalog, as table creation belongs to the Catalog composition
    root.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        rule: DatasetEvidenceRule,
        canonical_scratch_directory: Path,
        market_data_base_url: str,
        pit_params: PitRunParams,
        universe_params: UniverseRunParams,
        quality_factory: BoundedQualityEvidenceFactory,
        quality_params: BoundedQualitySourceParams,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._canonical_scratch_directory = canonical_scratch_directory
        self._market_data_base_url = market_data_base_url
        self._pit_params = pit_params
        self._universe_params = universe_params
        self._quality_factory = quality_factory
        self._quality_params = quality_params
        self._builder = DatasetEvidenceBuilder(adapter, storage, rule=rule)
        self._chunks = IcebergChunkWriter(adapter, DATASET_SELECTION_CHUNKS)
        self._verifier = StreamingEvidenceVerifier(
            adapter,
            builder=self._builder,
            chunks=self._chunks,
            sources=self._sources,
        )
        self._manifests = DatasetEvidenceManifestStore(adapter, self._verifier)

    @property
    def builder(self) -> DatasetEvidenceBuilder:
        """The bounded evidence builder used for writes, reads and manifest verification."""
        return self._builder

    @property
    def manifests(self) -> DatasetEvidenceManifestStore:
        """The v3-only manifest store, with streaming verification on persist and load."""
        return self._manifests

    def build(self, request: DatasetEvidenceRequest) -> DatasetBuildSummary:
        """Derive and commit one v3 Dataset; no v2 manifest is produced by this path.

        The sources are assembled at the version the build will run in: a rerun of a manifest
        recorded at an earlier contract version (ADR-0052 V7) gets that version's Quality
        source, exactly as the verifier re-derives it inside the recorded-version scope.
        """
        recorded = self._manifests.recorded_version(self._builder.selection_id(request))
        return self._builder.build(
            request,
            sources=self._sources(request, schema_version=recorded),
            chunks=self._chunks,
            manifests=self._manifests,
        )

    def load_manifest(self, content_hash: str) -> ResearchDatasetEvidenceManifest | None:
        """Load and fully re-verify one v3 manifest; return ``None`` when absent."""
        return self._manifests.load(content_hash)

    def iter_evidence(
        self, manifest: ResearchDatasetEvidenceManifest, stream: EvidenceStream
    ) -> AbstractContextManager[Iterator[Contract]]:
        """Open one authenticated evidence stream under this pipeline's registered rule."""
        return self._builder.iter_evidence(manifest, stream)

    def _sources(
        self, request: DatasetEvidenceRequest, *, schema_version: str | None = None
    ) -> DatasetEvidenceSources:
        return dataset_evidence_sources(
            self._adapter,
            self._storage,
            request,
            canonical_scratch_directory=self._canonical_scratch_directory,
            market_data_base_url=self._market_data_base_url,
            pit_params=self._pit_params,
            universe_params=self._universe_params,
            schema_version=schema_version,
            quality_factory=self._quality_factory,
            quality_params=self._quality_params,
        )
