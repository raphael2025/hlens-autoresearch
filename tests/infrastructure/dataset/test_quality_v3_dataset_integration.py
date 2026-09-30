from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetQualityReportRef,
    EvidenceStream,
)
from core.domain.base import Contract
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_EXCHANGE_INFO,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_RESPONSES,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
)
from infrastructure.dataset.builder import (
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetQualityError,
    dataset_evidence_rule,
)
from infrastructure.dataset.quality import (
    BoundedQualityEvidence,
    ListingReporterFactory,
    QualityReporterFactory,
)
from infrastructure.dataset.sources import dataset_evidence_sources
from infrastructure.pit.selector import PitRunParams
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality.listing_report_v2 import ListingHistoryQualityReporterV2
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
) -> QualityReporterV3:
    run_limits = RunLimits(leaf_max_records=2, leaf_max_bytes=1 << 16, fanout=2)
    return QualityReporterV3(
        adapter,
        evidence,
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
    scratch: StorageAdapter,
    canonical_scratch: StorageAdapter,
    listing_scratch: StorageAdapter,
    evidence: StorageAdapter,
) -> BoundedQualityEvidence:
    def canonical_factory(adapter: PinnedCatalogView) -> QualityReporterV3:
        return _canonical_reporter(
            adapter,
            evidence,
            canonical_scratch,
            lambda: (_ for _ in ()).throw(AssertionError("existing_only read called clock")),
        )

    def listing_factory(adapter: PinnedCatalogView) -> ListingHistoryQualityReporterV2:
        from tests.infrastructure.quality.test_listing_report_v2 import _reporter

        return _reporter(
            world.x,
            listing_scratch,  # type: ignore[arg-type]
            lambda: (_ for _ in ()).throw(AssertionError("existing_only read called clock")),
            adapter=adapter,
            evidence=evidence,
        )

    return BoundedQualityEvidence(
        world.h.adapter,
        evidence,
        pit,
        "agg_trades",
        canonical_reporter=cast(QualityReporterFactory, canonical_factory),
        listing_reporter=cast(ListingReporterFactory, listing_factory),
        stream_limits=QualityReportStreamLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
        scratch_storage=scratch,
        run_capacity=2,
        merge_fanout=2,
        run_limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
        max_run_object_bytes=8192,
    )


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
            w.h.adapter, w.h.storage, canonical_scratch, lambda: ds.K_Q
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
            aliased_reporter = type(
                "AliasedReporter",
                (),
                {"_storage": w.h.storage, "_scratch": alias},
            )()
            alias_source = BoundedQualityEvidence(
                w.h.adapter,
                w.h.storage,
                pit,
                "agg_trades",
                canonical_reporter=cast(QualityReporterFactory, lambda _view: aliased_reporter),
                listing_reporter=cast(ListingReporterFactory, lambda _view: aliased_reporter),
                stream_limits=QualityReportStreamLimits(
                    leaf_max_records=2, leaf_max_bytes=8192, fanout=2
                ),
                scratch_storage=join_scratch,
                run_capacity=2,
                merge_fanout=2,
                run_limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
                max_run_object_bytes=8192,
            )
            with pytest.raises(DatasetQualityError, match="roots must not overlap"):
                alias_source.listing_report()
            alias_source.close()
        finally:
            alias.close()
        w.listed(ds.ETH_HALT, utc(2023, 11, 18))
        assert w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table) != pinned_listing
        assert w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table) != pinned_raw
        bounded_pins: list[tuple[str, str | None]] = []
        bounded_scans: list[tuple[str, str | None]] = []
        original_pin_at = w.h.adapter.pin_bounded_metadata_at
        original_scan = w.h.adapter.scan_bounded_batches

        def record_pin_at(table: str, snapshot_id: str | None, **kwargs: Any) -> Any:
            bounded_pins.append((table, snapshot_id))
            return original_pin_at(table, snapshot_id, **kwargs)

        def record_scan(bounded: Any, *, snapshot_id: str, **kwargs: Any) -> Any:
            bounded_scans.append((bounded.name, snapshot_id))
            return original_scan(bounded, snapshot_id=snapshot_id, **kwargs)

        def forbid_current_pin(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Dataset Quality replay used a current-head metadata pin")

        monkeypatch.setattr(w.h.adapter, "pin_bounded_metadata_at", record_pin_at)
        monkeypatch.setattr(w.h.adapter, "scan_bounded_batches", record_scan)
        monkeypatch.setattr(w.h.adapter, "pin_bounded_metadata", forbid_current_pin)
        from infrastructure.catalog.bounded_metadata import BoundedMetadataLimits
        from tests.infrastructure.quality.test_listing_report_v2 import _metadata_limits

        limits = _metadata_limits()
        assert isinstance(limits, BoundedMetadataLimits)
        bounded_listing = w.h.adapter.pin_bounded_metadata_at(
            CANONICAL_INSTRUMENT_LISTINGS.table,
            pinned_listing,
            storage=w.h.storage,
            limits=limits,
        )
        historical_view = PinnedCatalogView(
            w.h.adapter,
            pit.snapshot_bindings,
            bounded_metadata={CANONICAL_INSTRUMENT_LISTINGS.table: bounded_listing},
        )
        with monkeypatch.context() as scan_guard:

            def forbid_catalog_reload(*_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("historical bounded scan reloaded the current catalog pointer")

            scan_guard.setattr(w.h.adapter, "_load", forbid_catalog_reload)
            scan_guard.setattr(w.h.adapter, "load_table", forbid_catalog_reload)
            columns = ("symbol", "status")
            reader = cast(
                Any,
                historical_view.scan_column_batches(
                    CANONICAL_INSTRUMENT_LISTINGS.table,
                    columns=columns,
                    snapshot_id=pinned_listing,
                ),
            )
            try:
                streamed = pa.Table.from_batches(list(reader), schema=reader.schema)
            finally:
                close = getattr(reader, "close", None)
                if callable(close):
                    close()
            limited = historical_view.scan_columns(
                CANONICAL_INSTRUMENT_LISTINGS.table,
                columns=columns,
                limit=2,
                snapshot_id=pinned_listing,
            )
            assert limited.equals(streamed.slice(0, 2))
        manifest_head = w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table)
        evidence = _WriteCounter(w.h.storage)
        source = _source(w, pit, join_scratch, canonical_scratch, listing_scratch, evidence)
        writes_before = (evidence.stages, evidence.publishes)
        request = DatasetEvidenceRequest(FIRST_SLICE_UNIVERSE, pit, "agg_trades", START, END)
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
                market_data_base_url=ds.ORIGIN,
                pit_params=PitRunParams(
                    row_batch_rows=1,
                    edge_batch_rows=1,
                    merge_fanout=2,
                    key_history_buffer=1,
                    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=65536, fanout=2),
                ),
                universe_params=ds.UNIVERSE_RUN_PARAMS,
                quality=source,
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
        assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) == manifest_head
        assert DATA_QUALITY_REPORT_MANIFESTS.table in pit.snapshot_bindings
        assert (evidence.stages, evidence.publishes) == writes_before
        assert (CANONICAL_INSTRUMENT_LISTINGS.table, pinned_listing) in bounded_pins
        assert (BINANCE_SPOT_EXCHANGE_INFO.table, pinned_raw) in bounded_pins
        assert (CANONICAL_INSTRUMENT_LISTINGS.table, pinned_listing) in bounded_scans
        assert (BINANCE_SPOT_EXCHANGE_INFO.table, pinned_raw) in bounded_scans
    finally:
        canonical_scratch.close()
        listing_scratch.close()
        join_scratch.close()
