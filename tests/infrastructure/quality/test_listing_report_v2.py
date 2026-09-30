"""Tests for the additive bounded listing-history@2.0.0 quality reporter."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import CommitRequest
from core.contracts.storage import (
    ObjectRef,
    PublishResult,
    StagedObject,
    StageRequest,
    StorageAdapter,
)
from infrastructure.canonical import listing_rules as lr
from infrastructure.catalog.bounded_metadata import BoundedMetadataLimits
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
)
from infrastructure.quality.listing_report_v2 import (
    LISTING_HISTORY_V2_RULE_HASH,
    LISTING_HISTORY_V2_RULE_ID,
    LISTING_HISTORY_V2_RULE_VERSION,
    ListingHistoryQualityReportError,
    ListingHistoryQualityReporterV2,
    ListingHistoryQualityReportMissing,
)
from infrastructure.quality.report_streams import (
    QualityReportStreamIntegrityError,
    QualityReportStreamLimits,
    QualityReportStreamRef,
    iter_quality_report_stream,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.content_key_tree import KeyTreeParams
from infrastructure.streaming.runs import RunLimits
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import T1, T2, Harness, StepClock

_LISTINGS = CANONICAL_INSTRUMENT_LISTINGS.table
_RAW = BINANCE_SPOT_EXCHANGE_INFO.table
_LATER = xs.KNOWLEDGE + timedelta(days=2)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _scratch(h: Harness, name: str = "scratch") -> LocalFileStorageAdapter:
    return LocalFileStorageAdapter(
        (h.tmp_path / f"{name}-warehouse").as_uri(),
        (h.tmp_path / f"{name}-stage").as_uri(),
    )


def _metadata_limits() -> BoundedMetadataLimits:
    return BoundedMetadataLimits(
        max_metadata_bytes=16 * 1024 * 1024,
        max_item_bytes=256 * 1024,
        max_retained_json_bytes=2 * 1024 * 1024,
        read_chunk_bytes=16 * 1024,
        max_small_array_items=512,
        max_map_items=512,
        max_snapshots=5000,
        run_capacity=64,
        run_limits=RunLimits(leaf_max_records=32, leaf_max_bytes=1024 * 1024, fanout=8),
        run_merge_fanout=8,
        key_tree_params=KeyTreeParams(page_max_bytes=1024 * 1024, leaf_max_records=64, fanout=8),
    )


def _reporter(
    h: Harness,
    scratch: LocalFileStorageAdapter,
    clock: Any,
    *,
    adapter: Any | None = None,
    evidence: Any | None = None,
    capacity: int = 2,
) -> ListingHistoryQualityReporterV2:
    run_limits = RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2)
    return ListingHistoryQualityReporterV2(
        h.adapter if adapter is None else adapter,
        h.storage if evidence is None else evidence,
        scratch_storage=scratch,
        market_data_base_url=xs.ORIGIN,
        clock=clock,
        metadata_limits=_metadata_limits(),
        capacity=capacity,
        merge_fanout=2,
        run_limits=run_limits,
        stream_limits=QualityReportStreamLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
        max_record_bytes=16384,
        max_run_object_bytes=32768,
        prefix_leaf_max_records=2,
        prefix_fanout=2,
        prefix_max_node_bytes=16384,
        prefix_max_record_bytes=4096,
        row_chunk_capacity=2,
        max_hash_chunk_bytes=64,
        max_event_record_bytes=4096,
        max_revision_record_bytes=1024,
        max_gap_record_bytes=4096,
        max_manifest_record_bytes=32768,
        max_identity_bytes=8192,
        retries=2,
    )


def _derive(h: Harness) -> None:
    deriver = h.deriver()
    try:
        deriver.derive()
    finally:
        deriver.close()


def _bindings(h: Harness) -> dict[str, str]:
    listing, raw = h.head(_LISTINGS), h.head(_RAW)
    assert listing is not None and raw is not None
    return {_LISTINGS: listing, _RAW: raw}


def _ref(name: str, manifest_ref: Mapping[str, Any]) -> QualityReportStreamRef:
    return QualityReportStreamRef(
        name,
        manifest_ref["format_id"],
        manifest_ref["record_count"],
        manifest_ref["leaf_count"],
        manifest_ref["depth"],
        manifest_ref["root_key"],
        manifest_ref["root_sha256"],
        manifest_ref["root_size"],
    )


class _CountingStorage:
    def __init__(
        self, inner: StorageAdapter, *, missing_key: str | None = None, tamper: bool = False
    ) -> None:
        self.inner = inner
        self.stages = 0
        self.publishes = 0
        self.missing_key = missing_key
        self.tamper = tamper

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        self.stages += 1
        return self.inner.stage(request, content)

    def publish(self, staged: StagedObject) -> PublishResult:
        self.publishes += 1
        return self.inner.publish(staged)

    def lookup(self, key: str) -> ObjectRef | None:
        if self.missing_key == key:
            return None
        return self.inner.lookup(key)

    def open_read(self, ref: ObjectRef) -> Any:
        reader = self.inner.open_read(ref)
        if not self.tamper or not ref.key.startswith("quality/report-evidence/v1/"):
            return reader
        with reader:
            return BytesIO(reader.read() + b" tampered")


def test_rule_identity_exact_bindings_and_pit_replay_when_heads_advance(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.observe("listing-v2-old", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    _derive(h)
    old = _bindings(h)
    h.observe("listing-v2-new", {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}, T2)
    _derive(h)
    assert old[_LISTINGS] != h.head(_LISTINGS)
    assert old[_RAW] != h.head(_RAW)

    seen_raw_scans: list[str | None] = []
    seen_pins: list[tuple[str, str | None]] = []
    original_batches = h.adapter.scan_column_batches
    original_pin_at = h.adapter.pin_bounded_metadata_at
    original_load = h.adapter.load_table
    original_scan = h.adapter.scan_columns

    def tracked_batches(table: str, **kwargs: Any) -> Any:
        if table == _RAW:
            seen_raw_scans.append(kwargs.get("snapshot_id"))
        return original_batches(table, **kwargs)

    def tracked_pin_at(table: str, snapshot_id: str | None, **kwargs: Any) -> Any:
        seen_pins.append((table, snapshot_id))
        return original_pin_at(table, snapshot_id, **kwargs)

    def no_current_data_head(table: str) -> Any:
        if table in {_LISTINGS, _RAW}:
            raise AssertionError("reporter loaded a current Listing or Raw head")
        return original_load(table)

    def no_eager_input_scan(table: str, **kwargs: Any) -> Any:
        if table in {_LISTINGS, _RAW}:
            raise AssertionError("reporter used an eager Listing or Raw scan")
        return original_scan(table, **kwargs)

    def forbidden_history(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("reporter used the legacy snapshot history API")

    monkeypatch.setattr(h.adapter, "scan_column_batches", tracked_batches)
    monkeypatch.setattr(h.adapter, "pin_bounded_metadata_at", tracked_pin_at)
    monkeypatch.setattr(h.adapter, "load_table", no_current_data_head)
    monkeypatch.setattr(h.adapter, "scan_columns", no_eager_input_scan)
    monkeypatch.setattr(h.adapter, "get_snapshot", forbidden_history)
    monkeypatch.setattr(h.adapter, "history", forbidden_history)
    scratch = _scratch(h)
    clock = StepClock(start=_LATER)
    reporter = _reporter(h, scratch, clock)
    result = reporter.report(old)
    assert not result.reused and clock.readings == 1
    assert result.report_id.startswith(f"{LISTING_HISTORY_V2_RULE_ID}@2.0.0.{_LISTINGS}.")
    assert result.manifest["quality_rule_id"] == LISTING_HISTORY_V2_RULE_ID
    assert result.manifest["quality_rule_version"] == LISTING_HISTORY_V2_RULE_VERSION
    assert result.manifest["quality_rule_hash"] == LISTING_HISTORY_V2_RULE_HASH
    assert result.manifest["snapshot_bindings"] == [
        {"table": table, "snapshot_id": old[table]} for table in sorted(old)
    ]
    assert seen_raw_scans == [old[_RAW]]
    assert seen_pins == [(_LISTINGS, old[_LISTINGS]), (_RAW, old[_RAW])]


def test_streams_are_complete_ordered_and_preserve_multiple_findings_and_gaps(
    h: Harness,
) -> None:
    h.observe(
        "listing-v2-findings-1",
        {"BTCUSDT": "UNKNOWN_STATUS", "ETHUSDT": "HALT"},
        T1,
    )
    h.observe(
        "listing-v2-findings-2",
        {"BTCUSDT": None, "ETHUSDT": "TRADING"},
        T2,
    )
    _derive(h)
    scratch = _scratch(h)
    result = _reporter(h, scratch, StepClock(start=_LATER)).report(_bindings(h))
    with iter_quality_report_stream(
        h.storage,
        _ref("events", result.manifest["events"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as reader:
        events = list(reader)
    with iter_quality_report_stream(
        h.storage,
        _ref("event_revisions", result.manifest["event_revisions"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as reader:
        revisions = list(reader)
    with iter_quality_report_stream(
        h.storage,
        _ref("evidence_gaps", result.manifest["evidence_gaps"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as reader:
        gaps = list(reader)

    assert events[0]["event_type"] == "report_inputs"
    assert events[-1]["event_type"] == "evidence_gaps"
    assert {event["event_type"] for event in events} >= {
        lr.FINDING_STATUS_UNKNOWN,
        lr.FINDING_SYMBOL_MISSING,
        lr.FINDING_SUSPENDED_BEFORE_TRADING,
    }
    assert [row["event_ordinal"] for row in revisions] == sorted(
        row["event_ordinal"] for row in revisions
    )
    assert [(row["table"], row["revision_id"]) for row in gaps] == sorted(
        (_LISTINGS, row["revision_id"])
        for row in h.rows(_LISTINGS)
        if row["availability_evidence_gap"] is not None
    )
    assert all(row["quality_report_id"] == result.report_id for row in gaps)


def test_revision_finding_larger_than_run_capacity_is_fully_streamed(h: Harness) -> None:
    h.observe("listing-v2-tie-seed", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    _derive(h)
    tie_time = T2
    for index in range(7):
        h.observe(
            f"listing-v2-tie-{index}",
            {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"},
            tie_time,
            server_time=10 + index,
        )
    scratch = _scratch(h)
    result = _reporter(h, scratch, StepClock(start=_LATER), capacity=2).report(_bindings(h))
    with iter_quality_report_stream(
        h.storage,
        _ref("events", result.manifest["events"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as event_reader:
        tied = [row for row in event_reader if row["event_type"] == lr.FINDING_OBSERVATION_TIE]
    with iter_quality_report_stream(
        h.storage,
        _ref("event_revisions", result.manifest["event_revisions"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as revision_reader:
        revisions = list(revision_reader)
    assert len(tied) == 2
    assert all(event["revision_count"] == 7 for event in tied)
    assert len(revisions) >= 14


def test_existing_only_uses_no_clock_or_evidence_writes_and_fails_if_missing(
    h: Harness,
) -> None:
    h.observe("listing-v2-replay", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    _derive(h)
    scratch = _scratch(h)
    evidence = _CountingStorage(h.storage)
    missing_clock = StepClock(start=_LATER)
    reporter = _reporter(h, scratch, missing_clock, evidence=evidence)
    with pytest.raises(ListingHistoryQualityReportMissing):
        reporter.report(_bindings(h), existing_only=True)
    assert missing_clock.readings == 0
    assert (evidence.stages, evidence.publishes) == (0, 0)

    first = reporter.report(_bindings(h))
    before = (evidence.stages, evidence.publishes)
    catalog_head = h.head(DATA_QUALITY_REPORT_MANIFESTS.table)
    replay_clock = StepClock(start=_LATER + timedelta(days=1))
    replay = _reporter(h, scratch, replay_clock, evidence=evidence).report(
        _bindings(h), existing_only=True
    )
    assert replay.reused and replay.manifest == first.manifest
    assert replay_clock.readings == 0
    assert (evidence.stages, evidence.publishes) == before
    assert h.head(DATA_QUALITY_REPORT_MANIFESTS.table) == catalog_head


def test_existing_only_rejects_missing_or_tampered_stream_roots(h: Harness) -> None:
    h.observe("listing-v2-root", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    _derive(h)
    scratch = _scratch(h)
    result = _reporter(h, scratch, StepClock(start=_LATER)).report(_bindings(h))
    missing = _CountingStorage(h.storage, missing_key=result.manifest["events"]["root_key"])
    with pytest.raises(QualityReportStreamIntegrityError, match="missing or misidentified"):
        _reporter(h, scratch, StepClock(start=_LATER), evidence=missing).report(
            _bindings(h), existing_only=True
        )
    tampered = _CountingStorage(h.storage, tamper=True)
    with pytest.raises(QualityReportStreamIntegrityError, match="bytes do not match"):
        _reporter(h, scratch, StepClock(start=_LATER), evidence=tampered).report(
            _bindings(h), existing_only=True
        )

    class TamperedManifest:
        def __getattr__(self, name: str) -> Any:
            return getattr(h.adapter, name)

        def scan_columns(self, table: str, **kwargs: Any) -> pa.Table:
            rows = h.adapter.scan_columns(table, **kwargs)
            if table != DATA_QUALITY_REPORT_MANIFESTS.table or rows.num_rows == 0:
                return rows
            values = rows.to_pylist()
            values[0]["quality_rule_hash"] = "0" * 64
            return pa.Table.from_pylist(values, schema=rows.schema)

    with pytest.raises(CatalogIntegrityError, match="quality_rule_hash"):
        _reporter(h, scratch, StepClock(start=_LATER), adapter=TamperedManifest()).report(
            _bindings(h), existing_only=True
        )


def test_duplicate_manifest_and_failed_commit_are_fail_closed_with_only_orphans(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.observe("listing-v2-duplicate", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    _derive(h)
    scratch = _scratch(h)
    first_bindings = _bindings(h)
    result = _reporter(h, scratch, StepClock(start=_LATER)).report(first_bindings)
    h.observe("listing-v2-orphan", {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}, T2)
    _derive(h)

    class RejectManifestCommit:
        def __getattr__(self, name: str) -> Any:
            return getattr(h.adapter, name)

        def commit_batch(self, request: CommitRequest, table: pa.Table) -> Any:
            if request.table == DATA_QUALITY_REPORT_MANIFESTS.table:
                raise RuntimeError("injected manifest commit failure")
            return h.adapter.commit_batch(request, table)

    evidence = _CountingStorage(h.storage)
    orphan_clock = StepClock(start=_LATER + timedelta(days=3))
    with pytest.raises(RuntimeError, match="injected manifest commit failure"):
        _reporter(
            h,
            _scratch(h, "orphan"),
            orphan_clock,
            adapter=RejectManifestCommit(),
            evidence=evidence,
        ).report(_bindings(h))
    assert orphan_clock.readings == 1
    assert evidence.stages > 0 and evidence.publishes > 0

    row = dict(result.manifest)
    batch = pa.Table.from_pylist([row], schema=DATA_QUALITY_REPORT_MANIFESTS.arrow_schema)
    h.adapter.commit_batch(
        CommitRequest(
            table=DATA_QUALITY_REPORT_MANIFESTS.table,
            batch_id="forged-duplicate-listing-manifest",
            batch_fingerprint=DATA_QUALITY_REPORT_MANIFESTS.fingerprint_rule.fingerprint(batch),
            row_count=1,
            expected_parent_snapshot_id=h.head(DATA_QUALITY_REPORT_MANIFESTS.table),
        ),
        batch,
    )
    with pytest.raises(CatalogIntegrityError, match="committed twice"):
        _reporter(h, scratch, StepClock(start=_LATER)).report(first_bindings, existing_only=True)


def test_report_clock_cannot_precede_bounded_raw_and_listing_knowledge_floor(h: Harness) -> None:
    h.observe("listing-v2-floor", {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}, T1)
    _derive(h)
    bound = _bindings(h)
    seen: list[str | None] = []
    original = h.adapter.scan_column_batches

    def tracked(table: str, **kwargs: Any) -> Any:
        if table == _RAW:
            seen.append(kwargs.get("snapshot_id"))
        return original(table, **kwargs)

    h.adapter.scan_column_batches = tracked  # type: ignore[method-assign]
    too_early = StepClock(start=xs.KNOWLEDGE - timedelta(seconds=1))
    with pytest.raises(
        ListingHistoryQualityReportError, match="clock precedes a Raw or Listing row"
    ):
        _reporter(h, _scratch(h), too_early).report(bound)
    assert too_early.readings == 1
    assert seen == [bound[_RAW]]
