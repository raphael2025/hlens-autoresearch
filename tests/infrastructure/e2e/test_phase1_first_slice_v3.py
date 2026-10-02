"""Phase 1 G1 end-to-end acceptance (roadmap #20) on the v3 production entry (ADR-0101 §7 / D5).

The same small BTCUSDT / ETHUSDT fixture and walk as ``test_phase1_first_slice.py`` (archive ->
Raw -> Canonical -> PIT -> Research Dataset + manifest -> Representation), with the dataset step on
the bounded v3 path that new datasets take since ADR-0077 DQ-10:

- the Quality step writes the v3 report manifests the v3 build joins: one ``QualityReporterV3``
  canonical-partition report per covered partition, built by the production profile mapping
  (``infrastructure.quality.report_cli.reporter_from_profile``), and the listing-history report.
  The main walk also writes the legacy reports first: from contract 2.6.0 (ADR-0109) a v3 build
  binds the legacy table only because it then has a snapshot. The v3-only variant writes no legacy
  report at all (a new catalog), and a spec that leaves an existing legacy snapshot unbound is
  refused;
- the dataset step is ``open_dataset_pipeline(settings, profile)`` -> ``DatasetBuildPipeline.build``
  over a ``pin_dataset_pit_spec`` spec (the ADR-0101 entry wiring; the catalog opener serves the
  world's catalog, so no DSN is connected);
- the manifest's claims are read back from its evidence streams, the manifest is re-verified on
  load, and a rebuild in a fresh process (reopened catalog) replays it bit for bit;
- the Representation step binds an E4 / F4 feature run to the persisted v3 manifest through its
  streaming verifier (``feature_request_from_derived_bars(..., evidence_verifier=...)``).

``test_phase1_first_slice.py`` stays as the v2 replay / compatibility case (seeded pre-cutoff v2
dataset). Every rule / run number is the arbitrary small value of the entry tests' profile
(DQ-9 OPEN; not a capacity choice). The PostgreSQL variant skips without
``HLENS_TEST_CATALOG_URI``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.feature import FeatureRequest
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetQualityReportRef,
    DatasetQualitySubject,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
    SelectedRevisionLineage,
    UniverseMember,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Kind, Ref, canonical_json
from infrastructure.canonical.resample import resample_bars
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
    DATA_QUALITY_REPORTS,
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
    DATASET_SELECTIONS,
)
from infrastructure.dataset.builder import DatasetEvidenceRequest, DatasetSpecError
from infrastructure.dataset.factory import bind_profile, open_dataset_pipeline
from infrastructure.dataset.pinning import pin_dataset_pit_spec
from infrastructure.dataset.profile import DatasetBuildProfile
from infrastructure.feature.dataset import DatasetBindingError, feature_request_from_derived_bars
from infrastructure.feature.observations import derived_bar_observations
from infrastructure.feature.runner import run_feature
from infrastructure.pit.selector import PitSelector
from infrastructure.quality.report_cli import reporter_from_profile
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from plugins.features import BarLogReturnProvider
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.entry_support import make_profile, patch_catalog, settings_for
from tests.infrastructure.e2e.first_slice_support import (
    BTC,
    DAY_END,
    DAY_START,
    ETH,
    KLINE_COUNT,
    ingest_bars_for,
    ingest_trades_for,
)
from tests.infrastructure.quality.test_listing_report_v2 import _reporter as listing_reporter
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.rest_store_support import DAY

FIVE_MINUTE_BAR: Ref = Ref(kind=Kind.REPRESENTATION, name="canonical_resample_5m", version="1.0.0")


def _scratch(tmp_path: Path, name: str) -> LocalFileStorageAdapter:
    root = tmp_path / f"scratch-{name}"
    return LocalFileStorageAdapter((root / "warehouse").as_uri(), (root / "staging").as_uri())


def _ingest(w: ds.World) -> None:
    """Steps 1-2: listings, then both symbols' aggTrades and 1m klines on every channel."""
    w.listed(ds.TRADING, ds.L1)
    w.trades()  # BTCUSDT aggTrades (archive + REST + normalize + reconcile)
    ingest_trades_for(w, ETH, tag="eth")
    ingest_bars_for(w, BTC, tag="btc", base="100")
    ingest_bars_for(w, ETH, tag="eth", base="200")


def _v3_quality(w: ds.World, tmp_path: Path, profile: DatasetBuildProfile) -> list[str]:
    """Step 3: the v3 report manifests of every covered klines partition and the listing."""
    report_ids: list[str] = []
    with _scratch(tmp_path, "canonical-quality") as scratch:
        reporter = reporter_from_profile(
            w.h.adapter,
            w.h.storage,
            scratch,
            profile,
            canonical_scratch_directory=w.h.canonical_scratch_directory,
            clock=lambda: ds.K_Q,
        )
        for symbol in (BTC, ETH):
            report_ids.append(reporter.report("klines_1m", symbol, DAY).report_id)
    listing_head = w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table)
    raw_head = w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table)
    assert listing_head is not None and raw_head is not None
    with _scratch(tmp_path, "listing-quality") as scratch:
        listing: Any = listing_reporter(
            w.x, scratch, lambda: ds.K_Q, adapter=w.h.adapter, evidence=w.h.storage
        )
        out = listing.report(
            {
                CANONICAL_INSTRUMENT_LISTINGS.table: listing_head,
                BINANCE_SPOT_EXCHANGE_INFO.table: raw_head,
            }
        )
        report_ids.append(out.report_id)
    assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) is not None
    return report_ids


def _request(w: ds.World) -> DatasetEvidenceRequest:
    pit = pin_dataset_pit_spec(
        w.h.adapter,
        name="hlens.e2e.first-slice",
        version="1.0.0",
        simulation_time=ds.SIM,
        knowledge_cutoff=ds.SIM,
        listing_assumption=False,
    )
    return DatasetEvidenceRequest(FIRST_SLICE_UNIVERSE, pit, "klines_1m", DAY_START, DAY_END)


def _stream(
    opened: Any, manifest: ResearchDatasetEvidenceManifest, stream: EvidenceStream
) -> list[Any]:
    with opened.pipeline.iter_evidence(manifest, stream) as records:
        return list(records)


def _walk_to_manifest(
    w: ds.World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    legacy_reports: bool = True,
) -> tuple[DatasetBuildProfile, DatasetEvidenceRequest, ResearchDatasetEvidenceManifest, str]:
    """Steps 1-5 and the restart replay; returns the persisted manifest and its snapshot.

    ``legacy_reports=False`` is a v3-only catalog: no legacy Quality report is ever written, so
    ``quality.data_quality_reports`` has no snapshot and the pinned spec cannot bind it (ADR-0109).
    """
    _, profile = make_profile(tmp_path)
    _ingest(w)

    def archive_rows(table: Any, symbol: str, raw_table: str) -> list[dict[str, Any]]:
        return [
            row
            for row in w.h.rows(table)
            if row["symbol"] == symbol and row["lineage_raw_table"] == raw_table
        ]

    btc_bars = archive_rows(c.BARS, "BTC-USDT", c.ARCHIVE_KLINES.table)
    eth_bars = archive_rows(c.BARS, "ETH-USDT", c.ARCHIVE_KLINES.table)
    assert len(btc_bars) == len(eth_bars) == KLINE_COUNT

    # From contract 2.6.0 (ADR-0109) the legacy report table is bound iff it has a snapshot when
    # the build runs; the main walk writes the legacy reports first, exactly as the v3 bar /
    # Quality integration supports do, the v3-only walk writes none.
    if legacy_reports:
        w.report("klines_1m", symbols=(BTC, ETH), listing=True)
    report_ids = _v3_quality(w, tmp_path, profile)
    assert (w.h.head(DATA_QUALITY_REPORTS.table) is not None) is legacy_reports
    assert len(report_ids) == 3  # BTC/DAY, ETH/DAY, the listing-history report

    # ---- step 5: the v3 dataset through the ADR-0101 entry wiring ----
    patch_catalog(monkeypatch, w)
    request = _request(w)
    assert DATASET_SELECTION_CHUNKS.table not in request.pit.snapshot_bindings
    assert (DATA_QUALITY_REPORTS.table in request.pit.snapshot_bindings) is legacy_reports
    with open_dataset_pipeline(settings_for(w), profile) as opened:
        summary = opened.pipeline.build(request)
        manifest = summary.manifest
        assert manifest.schema_version == CONTRACT_SCHEMA_VERSION == "2.6.0"
        assert not summary.replayed and not summary.manifest_replayed
        assert summary.row_count == manifest.row_count == 2 * KLINE_COUNT
        assert manifest.data_type == "klines_1m"
        assert manifest.universe_spec == FIRST_SLICE_UNIVERSE.binding()
        assert manifest.point_in_time == request.pit
        assert manifest.rule.rule_hash == profile.rule.rule_hash
        assert manifest.dataset.table == DATASET_SELECTION_CHUNKS.table
        assert manifest.dataset.snapshot_id == summary.dataset.snapshot_id
        assert (manifest.dataset.time_range_start, manifest.dataset.time_range_end) == (
            DAY_START,
            DAY_END,
        )

        members = _stream(opened, manifest, EvidenceStream.MEMBERS)
        assert all(isinstance(m, UniverseMember) for m in members)
        assert [ds.symbol_of(m) for m in members] == ["BTC-USDT", "ETH-USDT"]
        assert _stream(opened, manifest, EvidenceStream.EXCLUSIONS) == []

        lineage = _stream(opened, manifest, EvidenceStream.LINEAGE)
        assert all(isinstance(item, SelectedRevisionLineage) for item in lineage)
        assert {(i.canonical_table, i.raw_table, i.source_table) for i in lineage} == {
            (xs.LISTINGS.table, xs.EXCHANGE_INFO.table, xs.EXCHANGE_INFO.table),
            (c.BARS.table, c.ARCHIVE_KLINES.table, c.ARCHIVES.table),
        }

        reports = _stream(opened, manifest, EvidenceStream.QUALITY_REPORTS)
        assert all(isinstance(item, DatasetQualityReportRef) for item in reports)
        assert sorted(item.report_id for item in reports) == sorted(report_ids)
        assert [item.subject for item in reports][:1] == [DatasetQualitySubject.LISTING]
        assert {(item.symbol, item.day) for item in reports[1:]} == {(BTC, DAY), (ETH, DAY)}

        gaps = _stream(opened, manifest, EvidenceStream.EVIDENCE_GAPS)
        assert gaps and all(isinstance(gap, AvailabilityEvidenceGap) for gap in gaps)
        assert {gap.table for gap in gaps} <= {xs.LISTINGS.table, c.BARS.table}
        assert {gap.quality_report_id for gap in gaps} <= set(report_ids)

        # Persisted: one v3 manifest row, re-verified on load; no v2 table was written.
        assert len(w.h.rows(DATASET_EVIDENCE_MANIFESTS)) == 1
        assert opened.pipeline.load_manifest(summary.manifest_hash) == manifest
        assert w.h.head(DATASET_SELECTIONS.table) is None

    # A rebuild in a fresh process (a reopened catalog adapter) is bit-identical and replays.
    w.h.reopen()
    with open_dataset_pipeline(settings_for(w), profile) as reopened:
        again = reopened.pipeline.build(request)
    assert again.replayed and again.manifest_hash == summary.manifest_hash
    assert canonical_json(again.manifest.model_dump(mode="json")) == canonical_json(
        manifest.model_dump(mode="json")
    )
    assert again.dataset.snapshot_id == summary.dataset.snapshot_id
    assert len(w.h.rows(DATASET_EVIDENCE_MANIFESTS)) == 1
    return profile, request, manifest, summary.manifest_hash


def test_phase1_first_slice_archive_to_representation_v3(
    w: ds.World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, request, manifest, manifest_hash = _walk_to_manifest(w, tmp_path, monkeypatch)
    spec = request.pit

    # ---- step 6: E4 resample 5-minute bars and an F4 feature run bound to the v3 manifest ----
    verifier = bind_profile(profile, settings=settings_for(w))(w.h.adapter, w.h.storage)
    selection = PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    ).select(spec, "klines_1m", BTC, DAY_START, DAY_END)
    selection.require_no_conflict()
    bars5 = resample_bars(selection, 5, DAY_START, DAY_END)
    assert [bar.complete for bar in bars5] == [True, True, False]  # the 22:15 bucket is a gap
    observations = derived_bar_observations(bars5, selection, spec)
    assert len(observations) == 2  # only the complete buckets: nothing filled or interpolated

    feature = BarLogReturnProvider.spec(
        available_lag=timedelta(minutes=1), bar_input=FIVE_MINUTE_BAR
    )
    assert spec.simulation_time is not None
    eval_time = spec.simulation_time + feature.available_lag

    def dataset_request(items: Any) -> FeatureRequest:
        return feature_request_from_derived_bars(
            w.h.adapter,
            w.h.storage,
            builder=w.builder(),
            manifest_content_hash=manifest_hash,
            pit_spec=spec,
            minutes=5,
            observations=items,
            feature=feature,
            evaluation_times=(eval_time,),
            evidence_verifier=verifier,
        )

    request_first = dataset_request(observations)
    assert request_first.manifest_content_hash == manifest.content_hash() == manifest_hash
    for item in request_first.visible_at(eval_time, feature.available_lag):
        assert item.available_time + feature.available_lag <= eval_time
    result_first = run_feature(BarLogReturnProvider((feature,)), feature, request_first)
    [value] = result_first.values
    assert value.value is not None
    assert value.latest_input_available_time is not None
    assert value.latest_input_available_time + feature.available_lag <= value.evaluation_time

    # Derived bars that are not exactly resample_bars of the dataset never make a request.
    with pytest.raises(DatasetBindingError):
        dataset_request(observations[:1])

    # Stable result_hash across re-runs and across a fresh process (reopened catalog).
    assert run_feature(BarLogReturnProvider((feature,)), feature, request_first) == result_first
    w.h.reopen()
    verifier_again = bind_profile(profile, settings=settings_for(w))(w.h.adapter, w.h.storage)
    request_again = feature_request_from_derived_bars(
        w.h.adapter,
        w.h.storage,
        builder=w.builder(),
        manifest_content_hash=manifest_hash,
        pit_spec=spec,
        minutes=5,
        observations=observations,
        feature=feature,
        evaluation_times=(eval_time,),
        evidence_verifier=verifier_again,
    )
    assert request_again.model_dump_json() == request_first.model_dump_json()
    result_again = run_feature(BarLogReturnProvider((feature,)), feature, request_again)
    assert result_again.result_hash == result_first.result_hash


def test_phase1_first_slice_v3_on_a_catalog_without_legacy_quality_reports(
    w: ds.World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0109 acceptance (a): only v3 report manifests, the legacy table never written."""
    _, request, manifest, manifest_hash = _walk_to_manifest(
        w, tmp_path, monkeypatch, legacy_reports=False
    )
    assert w.h.head(DATA_QUALITY_REPORTS.table) is None
    bound = manifest.point_in_time.snapshot_bindings
    assert DATA_QUALITY_REPORTS.table not in bound
    assert bound[DATA_QUALITY_REPORT_MANIFESTS.table] == w.h.head(
        DATA_QUALITY_REPORT_MANIFESTS.table
    )
    assert manifest.schema_version == "2.6.0" and manifest.content_hash() == manifest_hash
    assert manifest.point_in_time == request.pit


def test_a_v3_build_refuses_a_spec_that_leaves_an_existing_legacy_snapshot_unbound(
    w: ds.World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0109 acceptance (b): the legacy table has a snapshot, the spec does not bind it."""
    _, profile = make_profile(tmp_path)
    _ingest(w)
    w.report("klines_1m", symbols=(BTC, ETH), listing=True)
    _v3_quality(w, tmp_path, profile)
    assert w.h.head(DATA_QUALITY_REPORTS.table) is not None
    patch_catalog(monkeypatch, w)
    pinned = _request(w)
    bound = dict(pinned.pit.snapshot_bindings)
    del bound[DATA_QUALITY_REPORTS.table]
    request = DatasetEvidenceRequest(
        pinned.universe,
        pinned.pit.model_copy(update={"snapshot_bindings": bound}),
        pinned.data_type,
        pinned.start,
        pinned.end,
    )
    with open_dataset_pipeline(settings_for(w), profile) as opened:
        with pytest.raises(DatasetSpecError, match=r"data_quality_reports.*ADR-0109"):
            opened.pipeline.build(request)
    assert w.h.head(DATASET_SELECTION_CHUNKS.table) is None  # nothing was committed
    assert w.h.rows(DATASET_EVIDENCE_MANIFESTS) == []


# =========================================================================================
# PostgreSQL variant (mirrors test_phase1_first_slice_postgres.py)
# =========================================================================================


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[ds.World]:
    with ds.postgres_world(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened


@pytest.mark.postgres
def test_first_slice_v3_dataset_end_to_end_and_restart_rebuild_on_postgres(
    pg: ds.World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, manifest, manifest_hash = _walk_to_manifest(pg, tmp_path / "walk", monkeypatch)
    assert manifest.content_hash() == manifest_hash
    assert pg.h.head(DATASET_EVIDENCE_MANIFESTS.table) is not None
    assert pg.h.head(DATASET_SELECTION_CHUNKS.table) == manifest.dataset.snapshot_id
