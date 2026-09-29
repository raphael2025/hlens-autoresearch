"""v3 (ADR-0077) bar datasets over the dataset-test ``World`` (C1-CONSUMERS test support).

Real v3 builds, nothing faked: the real upstream cursors (``dataset_evidence_sources``: B-UNIV's
``UniverseSpanCursor``, B-PIT's ``PitSelector.iter_bounded``, pinned quality reports), the real
``IcebergChunkWriter`` over ``research.dataset_selection_chunks``, the real
``StreamingEvidenceVerifier`` and its ``DatasetEvidenceManifestStore`` over
``research.dataset_evidence_manifests`` (every persist and load re-derives the dataset). Evidence
objects and sorted runs go to the world's real object store. Every rule / run size is the arbitrary
small value of ``test_dataset_v3_sources`` (DQ-9 OPEN; not a capacity choice): five bars make three
chunks and every stream spills into several leaves.

A spec shared by a v2 and a v3 build of the same world is taken **before** either build and binds
neither form's own tables (``OWN_TABLES``): each build then writes only its own tables, so both
datasets bind exactly the same upstream snapshots.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import StorageAdapter
from infrastructure.catalog.phase1_tables import (
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
    DATASET_SELECTIONS,
)
from infrastructure.dataset.builder import (
    DatasetBuilder,
    DatasetBuildSummary,
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
    dataset_evidence_rule,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.sources import dataset_evidence_sources
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.dataset.test_dataset_v3_sources import (
    PIT_PARAMS,
    RULE,
    UNIVERSE_PARAMS,
)
from tests.infrastructure.revision.rest_store_support import utc

#: Both forms' own tables: never an upstream binding of a shared spec.
OWN_TABLES: Final = (
    DATASET_SELECTIONS.table,
    DATASET_SELECTION_CHUNKS.table,
    DATASET_EVIDENCE_MANIFESTS.table,
)
DAY_WINDOW: Final = (utc(2023, 11, 14), utc(2023, 11, 15))
#: A feature interval of the bar tests: from the window's start to the price view.
INTERVAL: Final = (utc(2023, 11, 14), ds.SIM)
BARS: Final = 5


@dataclass
class V3:
    """The v3 build and verification machinery of one catalog."""

    adapter: RevisionCatalog
    storage: StorageAdapter
    evidence_builder: DatasetEvidenceBuilder
    chunks: IcebergChunkWriter
    verifier: StreamingEvidenceVerifier
    sources: Callable[[DatasetEvidenceRequest], DatasetEvidenceSources]

    def build(
        self,
        spec: PointInTimeSpec,
        *,
        data_type: str = "klines_1m",
        window: tuple[datetime, datetime] = DAY_WINDOW,
    ) -> DatasetBuildSummary:
        request = DatasetEvidenceRequest(
            universe=FIRST_SLICE_UNIVERSE,
            pit=spec,
            data_type=data_type,
            start=window[0],
            end=window[1],
        )
        return self.evidence_builder.build(
            request,
            sources=self.sources(request),
            chunks=self.chunks,
            manifests=self.verifier.store(),
        )


def v3(w: World, adapter: RevisionCatalog | None = None) -> V3:
    """The v3 machinery over ``adapter`` (default: the world's own catalog object)."""
    catalog = w.h.adapter if adapter is None else adapter
    storage = w.h.storage

    def sources(request: DatasetEvidenceRequest) -> DatasetEvidenceSources:
        return dataset_evidence_sources(
            catalog,
            storage,
            request,
            canonical_scratch_directory=w.h.canonical_scratch_directory,
            market_data_base_url=ds.ORIGIN,
            pit_params=PIT_PARAMS,
            universe_params=UNIVERSE_PARAMS,
        )

    evidence_builder = DatasetEvidenceBuilder(catalog, storage, rule=dataset_evidence_rule(**RULE))
    chunks = IcebergChunkWriter(catalog, DATASET_SELECTION_CHUNKS)
    verifier = StreamingEvidenceVerifier(
        catalog, builder=evidence_builder, chunks=chunks, sources=sources
    )
    return V3(catalog, storage, evidence_builder, chunks, verifier, sources)


def builder_over(w: World, adapter: RevisionCatalog) -> DatasetBuilder:
    """The v2 ``DatasetBuilder`` of the world's dataset table over ``adapter``."""
    return DatasetBuilder(
        adapter,
        w.h.storage,
        canonical_scratch_directory=w.h.canonical_scratch_directory,
        market_data_base_url=ds.ORIGIN,
        dataset_table=DATASET_SELECTIONS,
    )


def assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


def ingest(w: World) -> None:
    """BTCUSDT's five 1m bars of 2023-11-14 22:14 .. 22:18, listings and the bar reports."""
    w.listed()
    w.bars(count=BARS)
    w.report("klines_1m")


def spec_of(
    w: World,
    *,
    with_assumption: bool = True,
    interval: tuple[datetime, datetime] | None = None,
) -> PointInTimeSpec:
    """A spec of the current heads binding neither dataset form's own tables."""
    spec = w.spec(skip=OWN_TABLES, interval=interval)
    return assumed(spec) if with_assumption else spec
