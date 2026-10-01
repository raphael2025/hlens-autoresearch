"""v3 (ADR-0077) bar datasets over the dataset-test ``World`` (C1-CONSUMERS test support).

Real v3 builds, nothing faked: the real upstream cursors (``dataset_evidence_sources``: B-UNIV's
``UniverseSpanCursor``, B-PIT's ``PitSelector.iter_bounded``, and -- schema 2.5+, ADR-0094 -- the
bounded Quality source over the real v3 report manifests in
``quality.data_quality_report_manifests``: ``QualityReporterV3`` canonical partition reports and
the ``ListingHistoryQualityReporterV2`` listing report, written by ``report``), the real
``IcebergChunkWriter`` over ``research.dataset_selection_chunks``, the real
``StreamingEvidenceVerifier`` and its ``DatasetEvidenceManifestStore`` over
``research.dataset_evidence_manifests`` (every persist and load re-derives the dataset). Evidence
objects and sorted runs go to the world's real object store. Every rule / run size is the arbitrary
small value of ``test_dataset_v3_sources`` (DQ-9 OPEN; not a capacity choice): five bars make three
chunks and every stream spills into several leaves.

A spec shared by a v2 and a v3 build of the same world is taken **before** either build and binds
neither form's own tables (``OWN_TABLES``): each build then writes only its own tables, so both
datasets bind exactly the same upstream snapshots. ``report`` writes both the legacy reports the
v2 build consumes and the v3 report manifests the v3 build consumes, after the last listing / bar
ingest, so every report binds the heads the shared spec then pins.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import StorageAdapter
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
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
from infrastructure.dataset.quality import BoundedQualityEvidence, BoundedQualitySourceParams
from infrastructure.dataset.sources import dataset_evidence_sources
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.store import RevisionCatalog
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.dataset.test_dataset_v3_sources import (
    PIT_PARAMS,
    RULE,
    UNIVERSE_PARAMS,
)
from tests.infrastructure.dataset.test_quality_v3_dataset_integration import (
    _canonical_reporter,
    _quality_params,
)
from tests.infrastructure.quality.test_listing_report_v2 import _reporter as _listing_reporter
from tests.infrastructure.revision.rest_store_support import DAY, utc

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
#: The subjects of ``report``: the dataset tests' symbols and the one bar day.
SYMBOLS: Final = ("BTCUSDT", "ETHUSDT")
DAYS: Final = (DAY,)
_SCRATCH_IDS: Final = itertools.count()


def scratch(w: World, name: str) -> LocalFileStorageAdapter:
    """A fresh scratch store under the world's directory, isolated from the evidence store."""
    root = Path(w.h.tmp_path) / f"v3-{name}-{next(_SCRATCH_IDS)}"
    return LocalFileStorageAdapter(
        (root / "warehouse").as_uri(),
        (root / "staging").as_uri(),
    )


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
    # The bounded Quality join's run scratch; a plain local store, so it needs no closing.
    quality_params = _quality_params(scratch(w, "quality-join"))

    def quality_factory(
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        pit: PointInTimeSpec,
        data_type: str,
        *,
        view: PinnedCatalogView,
        canonical_scratch_directory: Path,
        params: BoundedQualitySourceParams,
    ) -> BoundedQualityEvidence:
        return BoundedQualityEvidence(
            adapter,
            storage,
            pit,
            data_type,
            view=view,
            canonical_scratch_directory=canonical_scratch_directory,
            params=params,
        )

    def sources(request: DatasetEvidenceRequest) -> DatasetEvidenceSources:
        return dataset_evidence_sources(
            catalog,
            storage,
            request,
            canonical_scratch_directory=w.h.canonical_scratch_directory,
            market_data_base_url=ds.ORIGIN,
            pit_params=PIT_PARAMS,
            universe_params=UNIVERSE_PARAMS,
            quality_factory=quality_factory,
            quality_params=quality_params,
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


def quality_v3_reports(
    w: World,
    data_type: str = "klines_1m",
    symbols: tuple[str, ...] = SYMBOLS,
    days: tuple[date, ...] = DAYS,
) -> None:
    """The real v3 report manifests a 2.5+ v3 build consumes (ADR-0094), at the current heads.

    One ``QualityReporterV3`` canonical partition report per symbol and day, and one listing
    history report over the current Listing / exchangeInfo heads, all committed to
    ``quality.data_quality_report_manifests``; every size is the small one of the
    ``test_quality_v3_dataset_integration`` reporters.
    """
    with scratch(w, "canonical-quality") as canonical_scratch:
        canonical = _canonical_reporter(
            w.h.adapter,
            w.h.storage,
            canonical_scratch,
            lambda: ds.K_Q,
            w.h.canonical_scratch_directory,
        )
        for symbol in symbols:
            for day in days:
                canonical.report(data_type, symbol, day)
    listing_snapshot = w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table)
    raw_snapshot = w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table)
    assert listing_snapshot is not None and raw_snapshot is not None
    with scratch(w, "listing-quality") as listing_scratch:
        listing: Any = _listing_reporter(
            w.x,
            listing_scratch,
            lambda: ds.K_Q,
            adapter=w.h.adapter,
            evidence=w.h.storage,
        )
        listing.report(
            {
                CANONICAL_INSTRUMENT_LISTINGS.table: listing_snapshot,
                BINANCE_SPOT_EXCHANGE_INFO.table: raw_snapshot,
            }
        )
    assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) is not None


def report(w: World, data_type: str = "klines_1m") -> None:
    """Both forms' reports of the current heads: the legacy reports (v2) and the v3 manifests."""
    w.report(data_type, SYMBOLS, DAYS)
    quality_v3_reports(w, data_type)


def ingest(w: World) -> None:
    """BTCUSDT's five 1m bars of 2023-11-14 22:14 .. 22:18, listings and the bar reports."""
    w.listed()
    w.bars(count=BARS)
    report(w)


def spec_of(
    w: World,
    *,
    with_assumption: bool = True,
    interval: tuple[datetime, datetime] | None = None,
) -> PointInTimeSpec:
    """A spec of the current heads binding neither dataset form's own tables."""
    spec = w.spec(skip=OWN_TABLES, interval=interval)
    return assumed(spec) if with_assumption else spec
