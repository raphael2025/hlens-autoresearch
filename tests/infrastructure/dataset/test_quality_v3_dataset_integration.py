from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetQualityReportRef,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
)
from core.domain.base import Contract
from core.domain.specs import DatasetRef, Zone
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_EXCHANGE_INFO,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_RESPONSES,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset.builder import (
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetQualityError,
    DatasetSpecError,
    PinnedQualityEvidence,
    _EvidenceBuildSink,
    dataset_evidence_rule,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.manifests import (
    ManifestStore,
    evidence_manifest_batch_id,
    evidence_manifest_row,
)
from infrastructure.dataset.quality import (
    BoundedQualityEvidence,
    BoundedQualitySourceParams,
)
from infrastructure.dataset.sources import dataset_evidence_sources
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.selector import PitRunParams
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality.report_streams import (
    QualityReportStreamLimits,
    QualityReportStreamRef,
    iter_quality_report_stream,
)
from infrastructure.quality.report_v3 import QualityReporterV3
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, START, World
from tests.infrastructure.quality.test_report_v3 import _identity_hashes
from tests.infrastructure.revision.rest_store_support import DAY, utc

_CANONICAL = rules.CANONICAL_TABLES["agg_trades"].table
_RAW_TABLES = (
    c.ARCHIVE_AGGS.table,
    BINANCE_SPOT_REST_AGG_TRADES.table,
)


def _canonical_reporter(
    adapter: Any,
    evidence: StorageAdapter,
    scratch: StorageAdapter,
    clock: Any,
    canonical_scratch_directory: Path,
) -> QualityReporterV3:
    run_limits = RunLimits(leaf_max_records=2, leaf_max_bytes=1 << 16, fanout=2)
    return QualityReporterV3(
        adapter,
        evidence,
        canonical_scratch_directory=canonical_scratch_directory,
        scratch_storage=scratch,
        clock=clock,
        identity_rule_hashes=_identity_hashes(),
        max_identity_rule_hashes=16,
        allowed_snapshot_tables=(
            _CANONICAL,
            *_RAW_TABLES,
            BINANCE_SPOT_ARCHIVES.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
        ),
        required_snapshot_tables=(
            _CANONICAL,
            *_RAW_TABLES,
            BINANCE_SPOT_ARCHIVES.table,
            BINANCE_SPOT_REST_RESPONSES.table,
        ),
        pit_params=PitRunParams(
            row_batch_rows=2,
            edge_batch_rows=2,
            merge_fanout=2,
            key_history_buffer=2,
            limits=run_limits,
        ),
        run_capacity=2,
        merge_fanout=2,
        run_limits=run_limits,
        stream_limits=QualityReportStreamLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
        max_event_record_bytes=4096,
        max_revision_record_bytes=1024,
        max_gap_record_bytes=4096,
        max_input_record_bytes=8192,
        max_manifest_record_bytes=32768,
        max_identity_bytes=8192,
        retries=2,
    )


class _Capture:
    def __init__(self) -> None:
        self.evidence_records: dict[EvidenceStream, list[Contract]] = {}
        self.rows: list[Mapping[str, Any]] = []

    def evidence(self, stream: EvidenceStream, record: Contract) -> None:
        self.evidence_records.setdefault(stream, []).append(record)

    def row(self, row: Mapping[str, Any]) -> None:
        self.rows.append(dict(row))


def _scratch(tmp_path: Path, name: str) -> LocalFileStorageAdapter:
    return LocalFileStorageAdapter(
        (tmp_path / f"{name}-warehouse").as_uri(),
        (tmp_path / f"{name}-staging").as_uri(),
    )


def _source(
    world: World,
    pit: PointInTimeSpec,
    evidence: StorageAdapter,
    params: BoundedQualitySourceParams,
    view: PinnedCatalogView,
) -> BoundedQualityEvidence:
    return BoundedQualityEvidence(
        world.h.adapter,
        evidence,
        pit,
        "agg_trades",
        view=view,
        canonical_scratch_directory=world.h.canonical_scratch_directory,
        params=params,
    )


def _quality_params(scratch: StorageAdapter) -> BoundedQualitySourceParams:
    return BoundedQualitySourceParams(
        scratch_storage=scratch,
        stream_limits=QualityReportStreamLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
        run_limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
        run_capacity=2,
        merge_fanout=2,
        max_run_object_bytes=8192,
        identity_registry=SimpleNamespace(
            canonical_v3_hashes=_identity_hashes(),
            max_identity_rule_hashes=16,
            max_identity_bytes=8192,
        ),
    )


def test_bounded_quality_requires_the_dataset_manifest_snapshot_binding(
    w: World,
) -> None:
    pit = ds.v3_pit(skip=(DATA_QUALITY_REPORT_MANIFESTS.table,))
    with pytest.raises(
        DatasetQualityError, match="does not bind quality.data_quality_report_manifests"
    ):
        BoundedQualityEvidence(
            w.h.adapter,
            w.h.storage,
            pit,
            "agg_trades",
            view=cast(Any, None),
            params=cast(Any, None),
            canonical_scratch_directory=w.h.canonical_scratch_directory,
        )


def test_quality_factory_cannot_substitute_different_source_parameters(
    w: World, tmp_path: Path
) -> None:
    scratch = _scratch(tmp_path, "join")
    params = _quality_params(scratch)
    request = ds.v3_request()

    def mismatched_factory(
        adapter: Any,
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
            params=replace(params, run_capacity=params.run_capacity + 1),
        )

    try:
        with pytest.raises(DatasetSpecError, match="supplied pinned view and parameters"):
            dataset_evidence_sources(
                w.h.adapter,
                w.h.storage,
                request,
                canonical_scratch_directory=w.h.canonical_scratch_directory,
                market_data_base_url=ds.ORIGIN,
                pit_params=PitRunParams(
                    row_batch_rows=1,
                    edge_batch_rows=1,
                    merge_fanout=2,
                    key_history_buffer=1,
                    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
                ),
                universe_params=ds.UNIVERSE_RUN_PARAMS,
                quality_factory=mismatched_factory,
                quality_params=params,
            )
    finally:
        scratch.close()


def test_historical_24_evidence_manifest_replays_through_legacy_quality_source(
    w: World,
) -> None:
    """Seed a historical envelope row, then exercise the normal read/replay verifier path.

    The seed uses the same bounded sink and legacy source as a 2.4-era build, but does not call
    ``DatasetEvidenceBuilder.build`` inside an old-version write scope. Only the persisted
    historical manifest row is hand-seeded; ``load_any`` and its verifier are production paths.
    """
    from core.domain.base import contract_schema_version_scope
    from tests.infrastructure.dataset.test_dataset_v3_sources import (
        PIT_PARAMS,
        UNIVERSE_PARAMS,
        _point_world,
        _request,
    )
    from tests.infrastructure.dataset.test_dataset_v3_sources import _builder as dataset_builder

    spec = _point_world(w)
    request = _request(spec)
    assert DATA_QUALITY_REPORT_MANIFESTS.table not in request.pit.snapshot_bindings
    builder = dataset_builder(w.h.storage, w.h.adapter)
    chunks = IcebergChunkWriter(w.h.adapter, DATASET_SELECTION_CHUNKS)
    sources = dataset_evidence_sources(
        w.h.adapter,
        w.h.storage,
        request,
        canonical_scratch_directory=w.h.canonical_scratch_directory,
        market_data_base_url=ds.ORIGIN,
        pit_params=PIT_PARAMS,
        universe_params=UNIVERSE_PARAMS,
        schema_version="2.4.0",
    )
    selection_id = builder.selection_id(request)
    with contract_schema_version_scope("2.4.0"):
        sink = _EvidenceBuildSink(w.h.storage, builder.rule, selection_id, chunks, version="2.4.0")
        derived = builder.select(
            request,
            sources=sources,
            sink=sink,
            manifested=False,
            schema_version="2.4.0",
        )
        evidence, chunk_count, _replayed_chunks, snapshot_id = sink.finish()
        chunks.seal(selection_id, chunk_count)
        historical = ResearchDatasetEvidenceManifest(
            dataset=DatasetRef(
                zone=Zone.RESEARCH_DATASET,
                table=DATASET_SELECTION_CHUNKS.table,
                snapshot_id=snapshot_id,
                time_range_start=request.start,
                time_range_end=request.end,
            ),
            point_in_time=request.pit,
            universe_spec=request.universe.binding(),
            rule=builder.rule.binding(),
            data_type=request.data_type,
            selection_id=derived.selection_id,
            row_count=derived.row_count,
            chunk_rows=builder.rule.chunk_rows,
            chunk_count=chunk_count,
            evidence=evidence,
        )

    # This row represents the historic commit; it is not written through a new-write API.
    w.h.forge_rows(
        DATASET_EVIDENCE_MANIFESTS,
        [evidence_manifest_row(historical)],
        evidence_manifest_batch_id(historical.content_hash()),
    )
    quality_source_types: list[type[Any]] = []

    def sources_for(item: DatasetEvidenceRequest) -> Any:
        result = dataset_evidence_sources(
            w.h.adapter,
            w.h.storage,
            item,
            canonical_scratch_directory=w.h.canonical_scratch_directory,
            market_data_base_url=ds.ORIGIN,
            pit_params=PIT_PARAMS,
            universe_params=UNIVERSE_PARAMS,
        )
        quality_source_types.append(type(result.quality))
        return result

    verifier = StreamingEvidenceVerifier(
        w.h.adapter, builder=builder, chunks=chunks, sources=sources_for
    )
    loaded = ManifestStore(w.h.adapter, builder, evidence_verifier=verifier).load_any(
        historical.content_hash()
    )
    assert loaded == historical
    assert quality_source_types == [PinnedQualityEvidence]


class _WriteCounter:
    def __init__(self, inner: StorageAdapter) -> None:
        self.inner = inner
        self.stages = 0
        self.publishes = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def stage(self, *args: Any, **kwargs: Any) -> Any:
        self.stages += 1
        return self.inner.stage(*args, **kwargs)

    def publish(self, *args: Any, **kwargs: Any) -> Any:
        self.publishes += 1
        return self.inner.publish(*args, **kwargs)


def test_dataset_v3_consumes_exact_bound_canonical_and_listing_reports(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.listed()
    w.trades()
    w.report()
    canonical_scratch = _scratch(tmp_path, "canonical")
    listing_scratch = _scratch(tmp_path, "listing")
    join_scratch = _scratch(tmp_path, "join")
    try:
        create_canonical = _canonical_reporter(
            w.h.adapter,
            w.h.storage,
            canonical_scratch,
            lambda: ds.K_Q,
            w.h.canonical_scratch_directory,
        )
        canonical_reports = {
            symbol: create_canonical.report("agg_trades", symbol, DAY)
            for symbol in ("BTCUSDT", "ETHUSDT")
        }
        from tests.infrastructure.quality.test_listing_report_v2 import _reporter

        listing_snap = w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table)
        raw_snap = w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table)
        assert listing_snap is not None and raw_snap is not None
        listing_report = cast(
            Any,
            _reporter(
                w.x,
                listing_scratch,
                lambda: ds.K_Q,
                adapter=w.h.adapter,
                evidence=w.h.storage,
            ).report(
                {
                    CANONICAL_INSTRUMENT_LISTINGS.table: listing_snap,
                    BINANCE_SPOT_EXCHANGE_INFO.table: raw_snap,
                }
            ),
        )
        pit = w.spec()
        assert DATA_QUALITY_REPORT_MANIFESTS.table in pit.snapshot_bindings
        pinned_listing = pit.snapshot_bindings[CANONICAL_INSTRUMENT_LISTINGS.table]
        pinned_raw = pit.snapshot_bindings[BINANCE_SPOT_EXCHANGE_INFO.table]
        alias = LocalFileStorageAdapter(w.h.storage.warehouse_uri, w.h.storage.staging_uri)
        try:
            with pytest.raises(DatasetQualityError, match="roots must not overlap"):
                BoundedQualityEvidence(
                    w.h.adapter,
                    w.h.storage,
                    pit,
                    "agg_trades",
                    view=PinnedCatalogView(w.h.adapter, pit.snapshot_bindings),
                    canonical_scratch_directory=w.h.canonical_scratch_directory,
                    params=_quality_params(alias),
                )
        finally:
            alias.close()
        w.listed(ds.ETH_HALT, utc(2023, 11, 18))
        assert w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table) != pinned_listing
        assert w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table) != pinned_raw
        quality_head_before_advance = w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table)
        create_canonical.report("agg_trades", "BTCUSDT", DAY + timedelta(days=1))
        quality_head_after_advance = w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table)
        assert quality_head_after_advance != quality_head_before_advance
        assert (
            quality_head_after_advance != pit.snapshot_bindings[DATA_QUALITY_REPORT_MANIFESTS.table]
        )
        manifest_head = pit.snapshot_bindings[DATA_QUALITY_REPORT_MANIFESTS.table]
        evidence = _WriteCounter(w.h.storage)

        def quality_factory(
            adapter: Any,
            storage: StorageAdapter,
            request_pit: PointInTimeSpec,
            data_type: str,
            *,
            view: PinnedCatalogView,
            canonical_scratch_directory: Path,
            params: BoundedQualitySourceParams,
        ) -> BoundedQualityEvidence:
            assert canonical_scratch_directory == w.h.canonical_scratch_directory
            return _source(w, request_pit, evidence, params, view)

        writes_before = (evidence.stages, evidence.publishes)
        request = DatasetEvidenceRequest(FIRST_SLICE_UNIVERSE, pit, "agg_trades", START, END)
        with pytest.raises(DatasetSpecError, match="explicit bounded Quality source factory"):
            dataset_evidence_sources(
                w.h.adapter,
                w.h.storage,
                request,
                canonical_scratch_directory=w.h.canonical_scratch_directory,
                market_data_base_url=ds.ORIGIN,
                pit_params=PitRunParams(
                    row_batch_rows=1,
                    edge_batch_rows=1,
                    merge_fanout=2,
                    key_history_buffer=1,
                    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
                ),
                universe_params=ds.UNIVERSE_RUN_PARAMS,
            )
        builder = DatasetEvidenceBuilder(
            w.h.adapter,
            w.h.storage,
            rule=dataset_evidence_rule(
                chunk_rows=2, leaf_max_records=2, leaf_max_bytes=16384, fanout=2
            ),
        )
        capture = _Capture()
        result = builder.select(
            request,
            sources=dataset_evidence_sources(
                w.h.adapter,
                w.h.storage,
                request,
                canonical_scratch_directory=w.h.canonical_scratch_directory,
                market_data_base_url=ds.ORIGIN,
                pit_params=PitRunParams(
                    row_batch_rows=1,
                    edge_batch_rows=1,
                    merge_fanout=2,
                    key_history_buffer=1,
                    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
                ),
                universe_params=ds.UNIVERSE_RUN_PARAMS,
                quality_factory=quality_factory,
                quality_params=_quality_params(join_scratch),
            ),
            sink=capture,
            manifested=False,
        )
        report_refs = cast(
            list[DatasetQualityReportRef],
            capture.evidence_records[EvidenceStream.QUALITY_REPORTS],
        )
        assert all(isinstance(ref, DatasetQualityReportRef) for ref in report_refs)
        assert {ref.report_id for ref in report_refs} == {
            listing_report.report_id,
            *(report.report_id for report in canonical_reports.values()),
        }
        dataset_gaps = cast(
            list[AvailabilityEvidenceGap],
            capture.evidence_records[EvidenceStream.EVIDENCE_GAPS],
        )
        assert dataset_gaps
        assert all(isinstance(gap, AvailabilityEvidenceGap) for gap in dataset_gaps)
        stored_gap_keys: set[tuple[str, str, str, str]] = set()
        for report in (listing_report, *canonical_reports.values()):
            raw_ref = report.manifest["evidence_gaps"]
            stream_ref = QualityReportStreamRef(
                "evidence_gaps",
                raw_ref["format_id"],
                raw_ref["record_count"],
                raw_ref["leaf_count"],
                raw_ref["depth"],
                raw_ref["root_key"],
                raw_ref["root_sha256"],
                raw_ref["root_size"],
            )
            with iter_quality_report_stream(
                evidence,
                stream_ref,
                limits=QualityReportStreamLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
            ) as rows:
                stored_gap_keys.update(
                    (
                        row["quality_report_id"],
                        row["table"],
                        row["revision_id"],
                        row["gap"],
                    )
                    for row in rows
                )
        assert {
            (gap.quality_report_id, gap.table, gap.revision_id, gap.gap) for gap in dataset_gaps
        } <= stored_gap_keys
        assert result.row_count > 0
        assert DATA_QUALITY_REPORT_MANIFESTS.table in pit.snapshot_bindings
        assert (evidence.stages, evidence.publishes) == writes_before
        assert pit.snapshot_bindings[DATA_QUALITY_REPORT_MANIFESTS.table] == manifest_head

        def sources_for(item: DatasetEvidenceRequest) -> Any:
            return dataset_evidence_sources(
                w.h.adapter,
                w.h.storage,
                item,
                canonical_scratch_directory=w.h.canonical_scratch_directory,
                market_data_base_url=ds.ORIGIN,
                pit_params=PitRunParams(
                    row_batch_rows=1,
                    edge_batch_rows=1,
                    merge_fanout=2,
                    key_history_buffer=1,
                    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
                ),
                universe_params=ds.UNIVERSE_RUN_PARAMS,
                quality_factory=quality_factory,
                quality_params=_quality_params(join_scratch),
            )

        chunks = IcebergChunkWriter(w.h.adapter, DATASET_SELECTION_CHUNKS)
        quality_manifest_scans: list[str | None] = []
        original_scan_columns = w.h.adapter.scan_columns

        def track_quality_manifest_scan(
            table: str, *, snapshot_id: str | None = None, **kwargs: Any
        ) -> Any:
            if table == DATA_QUALITY_REPORT_MANIFESTS.table:
                quality_manifest_scans.append(snapshot_id)
            return original_scan_columns(table, snapshot_id=snapshot_id, **kwargs)

        monkeypatch.setattr(w.h.adapter, "scan_columns", track_quality_manifest_scan)
        verifier = StreamingEvidenceVerifier(
            w.h.adapter, builder=builder, chunks=chunks, sources=sources_for
        )
        summary = builder.build(
            request,
            sources=sources_for(request),
            chunks=chunks,
            manifests=verifier.store(),
        )
        build_quality_scans = tuple(quality_manifest_scans)
        assert build_quality_scans
        assert set(build_quality_scans) == {
            pit.snapshot_bindings[DATA_QUALITY_REPORT_MANIFESTS.table]
        }
        post_build_head = w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table)
        create_canonical.report("agg_trades", "BTCUSDT", DAY + timedelta(days=2))
        assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) != post_build_head
        replay_scan_start = len(quality_manifest_scans)
        replayed = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=verifier).load_any(
            summary.manifest_hash
        )
        assert replayed == summary.manifest
        replay_quality_scans = tuple(quality_manifest_scans[replay_scan_start:])
        assert replay_quality_scans
        assert set(replay_quality_scans) == {
            pit.snapshot_bindings[DATA_QUALITY_REPORT_MANIFESTS.table]
        }
        old_bindings = {
            table: snapshot
            for table, snapshot in pit.snapshot_bindings.items()
            if table != DATA_QUALITY_REPORT_MANIFESTS.table
        }
        old_pit = pit.model_copy(update={"snapshot_bindings": old_bindings})
        legacy_shape = summary.manifest.model_copy(
            update={
                "point_in_time": old_pit,
                "schema_version": "2.4.0",
                "evidence": tuple(
                    ref
                    for ref in summary.manifest.evidence
                    if ref.stream is not EvidenceStream.PIT_CONFLICTS
                ),
            }
        )
        legacy = ResearchDatasetEvidenceManifest.model_validate_json(legacy_shape.model_dump_json())
        assert DATA_QUALITY_REPORT_MANIFESTS.table not in legacy.point_in_time.snapshot_bindings
        legacy_sources = dataset_evidence_sources(
            w.h.adapter,
            w.h.storage,
            replace(request, pit=old_pit),
            canonical_scratch_directory=w.h.canonical_scratch_directory,
            market_data_base_url=ds.ORIGIN,
            pit_params=PitRunParams(
                row_batch_rows=1,
                edge_batch_rows=1,
                merge_fanout=2,
                key_history_buffer=1,
                limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
            ),
            universe_params=ds.UNIVERSE_RUN_PARAMS,
            schema_version="2.4.0",
        )
        assert isinstance(legacy_sources.quality, PinnedQualityEvidence)
        with pytest.raises(ValueError, match="data_quality_report_manifests"):
            ResearchDatasetEvidenceManifest.model_validate_json(
                summary.manifest.model_copy(update={"point_in_time": old_pit}).model_dump_json()
            )
    finally:
        canonical_scratch.close()
        listing_scratch.close()
        join_scratch.close()
