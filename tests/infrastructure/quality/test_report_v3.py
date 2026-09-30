"""Direct integration checks for the additive ADR-0093 reporter path."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pyiceberg.expressions import AlwaysTrue, EqualTo

from core.contracts.storage import ObjectRef, PublishResult, StagedObject, StageRequest
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalUnitIncomplete
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_RESPONSES,
    DATA_QUALITY_REPORT_MANIFESTS,
)
from infrastructure.pit.selector import PIT_BINDING, REQUIRED_BINDINGS, PitRunParams
from infrastructure.quality.report_projection import CANONICAL_PARTITION_V3_RULE_HASH
from infrastructure.quality.report_streams import (
    QualityReportStreamLimits,
    iter_quality_report_stream,
)
from infrastructure.quality.report_v3 import QualityReporterV3, QualityReportV3Error
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, RunSetBuilder
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    RestHarness,
    StepClock,
    sqlite_harness,
    utc,
)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[RestHarness]:
    with sqlite_harness(tmp_path) as opened:
        yield opened


def _populate(
    harness: RestHarness, data_type: str = "klines_1m", *, reconcile: bool = True
) -> None:
    if data_type == "klines_1m":
        items = ss.kline_items(2)
        archive_lines = ss.archive_kline_lines(items)
        raw_tables = (c.ARCHIVE_KLINES, c.REST_KLINES)
    else:
        items = [
            *ss.agg_items(1),
            *ss.agg_items(1, first_id=102, first_ms=ss.T0 + 1),
        ]
        archive_lines = ss.archive_agg_lines(items)
        raw_tables = (c.ARCHIVE_AGGS, c.REST_AGGS)
    archive = c.ingest_archive(harness, data_type, archive_lines, knowledge=utc(2023, 12, 1))
    [response] = c.ingest_rest(harness, data_type, items, knowledge=utc(2023, 12, 5))
    c.normalizer(harness, clock=StepClock(start=utc(2023, 12, 6))).normalize_unit(
        raw_tables[0].table, archive
    )
    c.normalizer(harness, clock=StepClock(start=utc(2023, 12, 7))).normalize_unit(
        raw_tables[1].table, response
    )
    if reconcile:
        harness.reconciler(clock=StepClock(start=utc(2023, 12, 10))).reconcile(
            data_type, SYMBOL, DAY
        )


def _identity_hashes() -> dict[str, str]:
    hashes = {"quality": CANONICAL_PARTITION_V3_RULE_HASH, "pit": PIT_BINDING.policy_hash}
    for binding in (
        *REQUIRED_BINDINGS["availability_bindings"],
        *REQUIRED_BINDINGS["precedence_bindings"],
        *REQUIRED_BINDINGS["parser_bindings"],
        DELIVERY_CHANNEL_BINDING,
        rules.PRECEDENCE_MAP_BINDING,
    ):
        hashes[f"{binding.policy_id}@{binding.version}"] = binding.policy_hash
    return hashes


def _reporter(
    h: RestHarness,
    scratch: LocalFileStorageAdapter,
    clock: StepClock,
    evidence_storage: Any | None = None,
    data_type: str = "klines_1m",
) -> QualityReporterV3:
    run_limits = RunLimits(leaf_max_records=2, leaf_max_bytes=1 << 16, fanout=2)
    if data_type == "klines_1m":
        canonical_table = rules.CANONICAL_TABLES["klines_1m"].table
        raw_tables = (c.ARCHIVE_KLINES.table, c.REST_KLINES.table)
    else:
        canonical_table = rules.CANONICAL_TABLES["agg_trades"].table
        raw_tables = (c.ARCHIVE_AGGS.table, c.REST_AGGS.table)
    snapshot_tables = (
        canonical_table,
        *raw_tables,
        c.ARCHIVES.table,
        BINANCE_SPOT_REST_RESPONSES.table,
        BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
    )
    return QualityReporterV3(
        h.adapter,
        h.storage if evidence_storage is None else evidence_storage,
        scratch_storage=scratch,
        clock=clock,
        identity_rule_hashes=_identity_hashes(),
        max_identity_rule_hashes=16,
        allowed_snapshot_tables=snapshot_tables,
        required_snapshot_tables=(
            canonical_table,
            *raw_tables,
            c.ARCHIVES.table,
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


class _CountingStorage:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.stages = 0
        self.publishes = 0

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        self.stages += 1
        return self.inner.stage(request, content)

    def publish(self, staged: StagedObject) -> PublishResult:
        self.publishes += 1
        return self.inner.publish(staged)

    def lookup(self, key: str) -> ObjectRef | None:
        return self.inner.lookup(key)

    def open_read(self, ref: ObjectRef) -> Any:
        return self.inner.open_read(ref)


def test_new_report_publishes_three_streams_before_manifest_and_existing_only_replays(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    clock = StepClock(start=utc(2023, 12, 20))
    evidence = _CountingStorage(harness.storage)
    reporter = _reporter(harness, scratch, clock, evidence)
    first = reporter.report("klines_1m", SYMBOL, DAY)
    assert not first.reused and clock.calls == 1
    assert first.manifest["events"]["record_count"] >= 1
    assert first.manifest["event_revisions"]["record_count"] == 0
    assert first.manifest["evidence_gaps"]["record_count"] == len(harness.rows(c.BARS))
    with iter_quality_report_stream(
        evidence,
        reporter._quality_ref("events", first.manifest["events"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as rows:
        event_rows = list(rows)
    bar_gaps = [row for row in event_rows if row["event_type"] == "bar_1m_gap"]
    assert len(bar_gaps) == 2
    assert [(row["event_start"], row["event_end"]) for row in bar_gaps] == [
        ("2023-11-14T00:00:00+00:00", "2023-11-14T22:14:00+00:00"),
        ("2023-11-14T22:16:00+00:00", "2023-11-15T00:00:00+00:00"),
    ]
    for name in ("events", "event_revisions", "evidence_gaps"):
        ref = reporter._quality_ref(name, first.manifest[name])
        with iter_quality_report_stream(
            harness.storage, ref, limits=QualityReportStreamLimits(2, 8192, 2)
        ) as rows:
            assert len(list(rows)) == first.manifest[name]["record_count"]

    catalog_head = harness.head("quality.data_quality_report_manifests")
    evidence_writes_before = (evidence.stages, evidence.publishes)
    scratch_writes_before = len(list((tmp_path / "scratch-warehouse").rglob("*.jsonl")))
    existing_clock = StepClock(start=utc(2023, 12, 21))
    replay = _reporter(harness, scratch, existing_clock, evidence).report(
        "klines_1m", SYMBOL, DAY, existing_only=True
    )
    assert replay.reused and replay.manifest == first.manifest
    assert existing_clock.calls == 0
    assert harness.head("quality.data_quality_report_manifests") == catalog_head
    assert (evidence.stages, evidence.publishes) == evidence_writes_before
    assert len(list((tmp_path / "scratch-warehouse").rglob("*.jsonl"))) > scratch_writes_before


def test_input_row_cap_rejects_long_text_before_json_materialization() -> None:
    from infrastructure.quality import report_v3

    original = report_v3.json.dumps

    def bounded_encoding(value: Any, **kwargs: Any) -> str:
        if isinstance(value, str):
            assert len(value) <= 128
        return original(value, **kwargs)

    report_v3.json.dumps = bounded_encoding  # type: ignore[assignment]
    try:
        with pytest.raises(report_v3.QualityReportV3Error, match="max_input_record_bytes"):
            report_v3._row_size({"field": "x" * 100_000}, 128)
    finally:
        report_v3.json.dumps = original


@pytest.mark.parametrize(
    ("row", "size"),
    [({"field": "x"}, 14), ({"字段": "雪"}, 17)],
)
def test_input_row_size_counts_exact_jsonl_bytes(row: dict[str, str], size: int) -> None:
    from infrastructure.quality.report_v3 import _row_size

    _row_size(row, size)
    with pytest.raises(QualityReportV3Error, match="max_input_record_bytes"):
        _row_size(row, size - 1)


def test_existing_only_missing_manifest_reads_no_clock_or_evidence_storage(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    evidence = _CountingStorage(harness.storage)
    clock = StepClock(start=utc(2023, 12, 20))
    with pytest.raises(QualityReportV3Error, match="no committed v3 quality report"):
        _reporter(harness, scratch, clock, evidence).report(
            "klines_1m", SYMBOL, DAY, existing_only=True
        )
    assert clock.calls == 0
    assert (evidence.stages, evidence.publishes) == (0, 0)


def test_report_fails_closed_on_deleted_normalized_canonical_row(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    row = harness.rows(c.BARS)[0]
    harness.delete_rows(c.BARS, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    clock = StepClock(start=utc(2023, 12, 20))
    with pytest.raises(CatalogIntegrityError, match="normalized units"):
        _reporter(harness, scratch, clock).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0
    assert not harness.rows(DATA_QUALITY_REPORT_MANIFESTS)


def test_report_clock_must_follow_day_revisions_and_edges(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    clock = StepClock(start=utc(2023, 12, 8))
    with pytest.raises(
        QualityReportV3Error, match="precedes a Canonical revision or precedence edge"
    ):
        _reporter(harness, scratch, clock).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 1
    assert not harness.rows(DATA_QUALITY_REPORT_MANIFESTS)


def test_report_rejects_shared_evidence_and_scratch_adapter(harness: RestHarness) -> None:
    with pytest.raises(ValueError, match="distinct adapter"):
        _reporter(harness, harness.storage, StepClock(start=utc(2023, 12, 20)))  # type: ignore[arg-type]


def test_report_fails_closed_when_canonical_has_no_snapshot(
    harness: RestHarness, tmp_path: Path
) -> None:
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    clock = StepClock(start=utc(2023, 12, 20))
    with pytest.raises(QualityReportV3Error, match="has no pinned snapshot"):
        _reporter(harness, scratch, clock).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0
    assert not harness.rows(DATA_QUALITY_REPORT_MANIFESTS)


def test_valid_empty_canonical_snapshot_emits_full_day_bar_gap(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    for table in (
        c.BARS,
        c.ARCHIVE_KLINES,
        c.REST_KLINES,
        c.ARCHIVES,
        BINANCE_SPOT_REST_RESPONSES,
        BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    ):
        harness.delete_rows(table, AlwaysTrue())
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    reporter = _reporter(harness, scratch, StepClock(start=utc(2023, 12, 20)))
    result = reporter.report("klines_1m", SYMBOL, DAY)
    with iter_quality_report_stream(
        harness.storage,
        reporter._quality_ref("events", result.manifest["events"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as stream:
        gaps = [row for row in stream if row["event_type"] == "bar_1m_gap"]
    assert len(gaps) == 1
    assert gaps[0]["event_start"] == "2023-11-14T00:00:00+00:00"
    assert gaps[0]["event_end"] == "2023-11-15T00:00:00+00:00"


def test_incomplete_rest_response_page_fails_reporter_gate(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    [element] = [row for row in harness.rows(c.REST_KLINES) if row["response_revision_id"]][:1]
    harness.delete_rows(c.REST_KLINES, EqualTo("revision_id", element["revision_id"]))  # type: ignore[call-arg, arg-type]
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    reporter = _reporter(harness, scratch, StepClock(start=utc(2023, 12, 20)))
    bindings = reporter._pin(
        (
            rules.CANONICAL_TABLES["klines_1m"].table,
            c.ARCHIVE_KLINES.table,
            c.REST_KLINES.table,
            c.ARCHIVES.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
        )
    )
    with pytest.raises(CanonicalUnitIncomplete, match="stopped half-way"):
        reporter._check_rest_pages("klines_1m", SYMBOL, DAY, bindings)


def test_rest_gate_accepts_multiple_responses_with_elements_held_by_first_page(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    c.ingest_rest(
        harness,
        "klines_1m",
        ss.kline_items(2),
        knowledge=utc(2023, 12, 8),
        request_id="req-rest-second-page",
    )
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    reporter = _reporter(harness, scratch, StepClock(start=utc(2023, 12, 20)))
    bindings = reporter._pin(
        (
            rules.CANONICAL_TABLES["klines_1m"].table,
            c.ARCHIVE_KLINES.table,
            c.REST_KLINES.table,
            c.ARCHIVES.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
        )
    )
    reporter._check_rest_pages("klines_1m", SYMBOL, DAY, bindings)


def test_report_fails_closed_when_pit_and_canonical_key_coverage_differ(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness)
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    reporter = _reporter(harness, scratch, StepClock(start=utc(2023, 12, 20)))
    canonical = rules.CANONICAL_TABLES["klines_1m"].table
    bindings = reporter._pin(
        (
            canonical,
            c.ARCHIVE_KLINES.table,
            c.REST_KLINES.table,
            c.ARCHIVES.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
        )
    )
    gap_builder = RunSetBuilder(
        scratch,
        key=lambda row: (row["table"], row["revision_id"]),
        capacity=2,
        merge_fanout=2,
        limits=RunLimits(leaf_max_records=2, leaf_max_bytes=1 << 16, fanout=2),
    )
    with pytest.raises(CatalogIntegrityError, match="PIT key coverage"):
        reporter._canonical_runs(
            "klines_1m",
            SYMBOL,
            utc(2023, 11, 14),
            utc(2023, 11, 15),
            bindings,
            pit_key_root=None,
            gap_builder=gap_builder,
        )


def test_manifest_commit_failure_leaves_streams_without_manifest(
    harness: RestHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _populate(harness)
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    evidence = _CountingStorage(harness.storage)
    reporter = _reporter(harness, scratch, StepClock(start=utc(2023, 12, 20)), evidence)
    commit = harness.adapter.commit_batch

    def fail_manifest(request: Any, batch: Any) -> Any:
        if request.table == DATA_QUALITY_REPORT_MANIFESTS.table:
            raise RuntimeError("injected manifest commit failure")
        return commit(request, batch)

    monkeypatch.setattr(harness.adapter, "commit_batch", fail_manifest)
    with pytest.raises(RuntimeError, match="injected manifest commit failure"):
        reporter.report("klines_1m", SYMBOL, DAY)
    assert evidence.stages > 0 and evidence.publishes > 0
    assert not harness.rows(DATA_QUALITY_REPORT_MANIFESTS)


def test_aggregate_trade_discontinuity_is_projected_without_materializing_all_ids(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness, "agg_trades")
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    reporter = _reporter(
        harness, scratch, StepClock(start=utc(2023, 12, 20)), data_type="agg_trades"
    )
    result = reporter.report("agg_trades", SYMBOL, DAY)
    with iter_quality_report_stream(
        harness.storage,
        reporter._quality_ref("events", result.manifest["events"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as stream:
        events = list(stream)
    [event] = [item for item in events if item["event_type"] == "agg_trade_id_discontinuity"]
    assert event["detail"] == "aggregate trade ids jump from 100 to 102 (1 id(s) absent)"


def test_conflict_heads_are_streamed_to_event_revisions(
    harness: RestHarness, tmp_path: Path
) -> None:
    _populate(harness, reconcile=False)
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    reporter = _reporter(harness, scratch, StepClock(start=utc(2023, 12, 20)))
    result = reporter.report("klines_1m", SYMBOL, DAY)
    with iter_quality_report_stream(
        harness.storage,
        reporter._quality_ref("events", result.manifest["events"]),
        limits=QualityReportStreamLimits(2, 8192, 2),
    ) as stream:
        events = list(stream)
    conflicts = [item for item in events if item["event_type"] == "competing_heads"]
    assert len(conflicts) == 2
    assert sum(item["revision_count"] for item in conflicts) == 4
    assert result.manifest["event_revisions"]["record_count"] == 4
