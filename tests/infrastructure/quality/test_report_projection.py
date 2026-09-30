"""Canonical-partition v3 event/revision JSONL projection and RunSet lifecycle tests."""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure.quality.report_projection import (
    CANONICAL_PARTITION_V3_RULE_HASH,
    CanonicalPartitionProjectionError,
    CanonicalPartitionV3EvidenceGapProjector,
    CanonicalPartitionV3Projector,
    ProjectedCanonicalEvidenceGaps,
)
from infrastructure.quality.report_streams import (
    QualityReportStreamLimits,
    QualityReportStreamWriter,
    iter_quality_report_stream,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorageAdapter:
    return LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )


def limits() -> RunLimits:
    return RunLimits(leaf_max_records=8, leaf_max_bytes=4096, fanout=3)


def projector(
    storage: StorageAdapter,
    *,
    capacity: int = 3,
    merge_fanout: int = 2,
    max_event_record_bytes: int = 4096,
    max_revision_record_bytes: int = 4096,
) -> CanonicalPartitionV3Projector:
    return CanonicalPartitionV3Projector(
        storage,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits(),
        max_event_record_bytes=max_event_record_bytes,
        max_revision_record_bytes=max_revision_record_bytes,
    )


def gap_projector(
    storage: StorageAdapter,
    *,
    capacity: int = 3,
    merge_fanout: int = 2,
    max_gap_record_bytes: int = 4096,
) -> CanonicalPartitionV3EvidenceGapProjector:
    return CanonicalPartitionV3EvidenceGapProjector(
        storage,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits(),
        max_gap_record_bytes=max_gap_record_bytes,
    )


def project_one(
    storage: StorageAdapter,
    *,
    revision_ids: Any,
    event_type: str = "bar_1m_gap",
    table: str | None = "canonical.bars_1m",
    observation_key: str | None = "binance:spot:bar:BTC-USDT:1m:1700000000000",
    event_start: datetime | None = datetime(2023, 11, 14, tzinfo=UTC),
    event_end: datetime | None = datetime(2023, 11, 14, 0, 1, tzinfo=UTC),
    detail: str = "gap",
) -> dict[str, Any]:
    instance = projector(storage)
    with instance.project_event(
        event_type=event_type,
        table=table,
        observation_key=observation_key,
        revision_ids=revision_ids,
        event_start=event_start,
        event_end=event_end,
        detail=detail,
    ) as projected:
        revisions = list(projected.revision_records)
        record = dict(projected.event_record)
    assert instance.next_event_ordinal == 1
    assert instance.next_revision_ordinal == len(revisions)
    return record


def test_empty_revision_ids_are_supported_with_zero_range(storage: LocalFileStorageAdapter) -> None:
    instance = projector(storage)
    with instance.project_event(
        event_type="report_inputs",
        table=None,
        observation_key=None,
        revision_ids=(),
        event_start=None,
        event_end=None,
        detail="pinned input snapshots",
    ) as projected:
        assert list(projected.revision_records) == []
        assert projected.event_record["revision_first_ordinal"] == 0
        assert projected.event_record["revision_count"] == 0
        assert projected.event_record["event_start"] is None
        assert projected.event_record["event_end"] is None
    assert instance.next_event_ordinal == 1
    assert instance.next_revision_ordinal == 0


def test_revisions_are_sorted_deduplicated_and_bound_to_event_ordinal(
    storage: LocalFileStorageAdapter,
) -> None:
    instance = projector(storage, capacity=2)
    with instance.project_event(
        event_type="competing_heads",
        table="canonical.trades",
        observation_key="key-a",
        revision_ids=(item for item in ["rev-c", "rev-a", "rev-b", "rev-a"]),
        event_start=None,
        event_end=None,
        detail="conflicting heads",
    ) as first:
        assert first.event_record["revision_first_ordinal"] == 0
        assert first.event_record["revision_count"] == 3
        assert list(first.revision_records) == [
            {"event_ordinal": 0, "revision_id": "rev-a"},
            {"event_ordinal": 0, "revision_id": "rev-b"},
            {"event_ordinal": 0, "revision_id": "rev-c"},
        ]
    with instance.project_event(
        event_type="evidence_gaps",
        table="canonical.trades",
        observation_key="key-b",
        revision_ids=["rev-y", "rev-x"],
        event_start=None,
        event_end=None,
        detail="missing evidence",
    ) as second:
        assert second.event_record["revision_first_ordinal"] == 3
        assert second.event_record["revision_count"] == 2
        assert list(second.revision_records) == [
            {"event_ordinal": 1, "revision_id": "rev-x"},
            {"event_ordinal": 1, "revision_id": "rev-y"},
        ]
    assert instance.next_event_ordinal == 2
    assert instance.next_revision_ordinal == 5


def test_projected_records_are_read_only_and_stream_writer_serializes_them(
    storage: LocalFileStorageAdapter,
) -> None:
    event_writer = QualityReportStreamWriter(
        storage, "events", limits=QualityReportStreamLimits(4, 4096, 2)
    )
    revision_writer = QualityReportStreamWriter(
        storage, "event_revisions", limits=QualityReportStreamLimits(4, 4096, 2)
    )
    with projector(storage).project_event(
        event_type="competing_heads",
        table="canonical.trades",
        observation_key="key",
        revision_ids=["rev-b", "rev-a"],
        event_start=None,
        event_end=None,
        detail="competing heads",
    ) as projected:
        with pytest.raises(TypeError):
            projected.event_record["detail"] = "tampered"  # type: ignore[index]
        event_writer.append(projected.event_record)
        revisions = list(projected.revision_records)
        with pytest.raises(TypeError):
            revisions[0]["revision_id"] = "tampered"  # type: ignore[index]
        for revision in revisions:
            revision_writer.append(revision)

    event_ref = event_writer.finish()
    revision_ref = revision_writer.finish()
    with iter_quality_report_stream(
        storage, event_ref, limits=QualityReportStreamLimits(4, 4096, 2)
    ) as records:
        events = list(records)
    with iter_quality_report_stream(
        storage, revision_ref, limits=QualityReportStreamLimits(4, 4096, 2)
    ) as records:
        revisions = list(records)
    assert events[0]["detail"] == "competing heads"
    assert [record["revision_id"] for record in revisions] == ["rev-a", "rev-b"]


def test_evidence_gaps_empty_stream_has_zero_records_and_round_trips(
    storage: LocalFileStorageAdapter,
) -> None:
    gaps = gap_projector(storage)
    writer = QualityReportStreamWriter(
        storage, "evidence_gaps", limits=QualityReportStreamLimits(3, 4096, 2)
    )
    with gaps.project_gaps(quality_report_id="report-empty", gaps=iter(())) as projected:
        assert projected.record_count == 0
        assert not projected.complete
        assert list(projected.records) == []
    assert projected.complete
    assert gaps.next_gap_ordinal == 0
    with pytest.raises(CanonicalPartitionProjectionError, match="one-shot"):
        with gaps.project_gaps(quality_report_id="report-empty-again", gaps=[]):
            pytest.fail("evidence-gap projector allowed a second stream")
    ref = writer.finish()
    assert ref.record_count == 0
    with iter_quality_report_stream(
        storage, ref, limits=QualityReportStreamLimits(3, 4096, 2)
    ) as records:
        assert list(records) == []


def test_evidence_gaps_sort_by_table_then_revision_with_unicode_identity(
    storage: LocalFileStorageAdapter,
) -> None:
    source = [
        ("canonical.trades", "rev-z", "gap-z"),
        ("canonical.bars_1m", "rev-é", "accent"),
        ("canonical.bars_1m", "rev-e\u0301", "combining"),
        ("canonical.bars_1m", "rev-a", "first"),
        ("canonical.trades", "rev-a", "trade-first"),
    ]
    instance = gap_projector(storage, capacity=2)
    with instance.project_gaps(quality_report_id="report-unicode", gaps=iter(source)) as projected:
        assert projected.record_count == len(source)
        records = list(projected.records)
    assert records == [
        {
            "quality_report_id": "report-unicode",
            "table": "canonical.bars_1m",
            "revision_id": "rev-a",
            "gap": "first",
        },
        {
            "quality_report_id": "report-unicode",
            "table": "canonical.bars_1m",
            "revision_id": "rev-e\u0301",
            "gap": "combining",
        },
        {
            "quality_report_id": "report-unicode",
            "table": "canonical.bars_1m",
            "revision_id": "rev-é",
            "gap": "accent",
        },
        {
            "quality_report_id": "report-unicode",
            "table": "canonical.trades",
            "revision_id": "rev-a",
            "gap": "trade-first",
        },
        {
            "quality_report_id": "report-unicode",
            "table": "canonical.trades",
            "revision_id": "rev-z",
            "gap": "gap-z",
        },
    ]
    assert instance.next_gap_ordinal == len(source)


@pytest.mark.parametrize("different_gap", [False, True])
def test_evidence_gap_duplicate_identity_fails_closed(
    storage: LocalFileStorageAdapter, different_gap: bool
) -> None:
    instance = gap_projector(storage, capacity=1)
    gaps = [
        ("canonical.trades", "rev-duplicate", "missing source"),
        (
            "canonical.trades",
            "rev-duplicate",
            "different reason" if different_gap else "missing source",
        ),
    ]
    with pytest.raises(CanonicalPartitionProjectionError, match="duplicate evidence-gap identity"):
        with instance.project_gaps(quality_report_id="report-dup", gaps=iter(gaps)):
            pytest.fail("duplicate gap identity was accepted")
    assert instance.next_gap_ordinal == 0


def test_evidence_gap_record_limit_accepts_exact_line_and_rejects_one_byte_less(
    storage: LocalFileStorageAdapter,
) -> None:
    report_id = "report-é"
    gap_row = {
        "quality_report_id": report_id,
        "table": "canonical.bars_1m",
        "revision_id": "rev-é",
        "gap": "missing public source 🕒",
    }
    line_size = len(canonical_json(gap_row).encode("utf-8")) + 1
    with gap_projector(storage, max_gap_record_bytes=line_size).project_gaps(
        quality_report_id=report_id,
        gaps=[("canonical.bars_1m", "rev-é", "missing public source 🕒")],
    ) as projected:
        assert list(projected.records) == [gap_row]
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with gap_projector(storage, max_gap_record_bytes=line_size - 1).project_gaps(
            quality_report_id=report_id,
            gaps=[("canonical.bars_1m", "rev-é", "missing public source 🕒")],
        ):
            pytest.fail("oversized evidence gap row was accepted")


def test_evidence_gap_runset_envelope_limit_is_checked_before_storage_write(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_id = "report-envelope"
    input_gap = ("canonical.trades", "rev-a", "gap")
    projected_row = {
        "quality_report_id": report_id,
        "table": input_gap[0],
        "revision_id": input_gap[1],
        "gap": input_gap[2],
    }
    scratch_line_size = len(canonical_json({"$obj": projected_row}).encode("utf-8")) + 1
    actual_stage = storage.stage
    staged = 0

    def tracked(*args: Any, **kwargs: Any) -> Any:
        nonlocal staged
        staged += 1
        return actual_stage(*args, **kwargs)

    monkeypatch.setattr(storage, "stage", tracked)
    exact_limits = RunLimits(
        leaf_max_records=2,
        leaf_max_bytes=512 + scratch_line_size,
        fanout=2,
    )
    with CanonicalPartitionV3EvidenceGapProjector(
        storage,
        capacity=1,
        merge_fanout=2,
        limits=exact_limits,
        max_gap_record_bytes=4096,
    ).project_gaps(quality_report_id=report_id, gaps=[input_gap]) as projected:
        assert list(projected.records) == [projected_row]
    assert staged > 0

    before_reject = staged
    too_small_limits = RunLimits(
        leaf_max_records=2,
        leaf_max_bytes=512 + scratch_line_size - 1,
        fanout=2,
    )
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with CanonicalPartitionV3EvidenceGapProjector(
            storage,
            capacity=1,
            merge_fanout=2,
            limits=too_small_limits,
            max_gap_record_bytes=4096,
        ).project_gaps(quality_report_id=report_id, gaps=[input_gap]):
            pytest.fail("oversized scratch envelope was accepted")
    assert staged == before_reject


def test_evidence_gap_high_cardinality_is_ordered_across_multiple_run_levels(
    storage: LocalFileStorageAdapter,
) -> None:
    count = 257
    source = (
        ("canonical.trades" if index % 2 else "canonical.bars_1m", f"rev-{index:04d}", "gap")
        for index in range(count - 1, -1, -1)
    )
    instance = gap_projector(storage, capacity=5, merge_fanout=2)
    with instance.project_gaps(quality_report_id="report-large", gaps=source) as projected:
        assert projected.record_count == count
        previous: tuple[str, str] | None = None
        seen = 0
        for record in projected.records:
            key = (record["table"], record["revision_id"])
            assert previous is None or previous < key
            previous = key
            seen += 1
        assert seen == count
    assert projected.complete
    assert instance.next_gap_ordinal == count


def test_evidence_gap_oversized_text_is_rejected_before_storage_write(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_stage = storage.stage
    staged = 0

    def tracked(*args: Any, **kwargs: Any) -> Any:
        nonlocal staged
        staged += 1
        return actual_stage(*args, **kwargs)

    monkeypatch.setattr(storage, "stage", tracked)
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with gap_projector(storage, max_gap_record_bytes=256).project_gaps(
            quality_report_id="report-large-gap",
            gaps=[("canonical.trades", "rev-a", "x" * 1_000_000)],
        ):
            pytest.fail("oversized gap text was accepted")
    assert staged == 0


def test_evidence_gap_projection_round_trips_read_only_records_through_stream_writer(
    storage: LocalFileStorageAdapter,
) -> None:
    writer = QualityReportStreamWriter(
        storage, "evidence_gaps", limits=QualityReportStreamLimits(2, 4096, 2)
    )
    gaps = gap_projector(storage)
    with gaps.project_gaps(
        quality_report_id="report-roundtrip",
        gaps=[("canonical.trades", "rev-b", "missing source"), ("raw.trades", "rev-a", "no proof")],
    ) as projected:
        assert projected.record_count == 2
        with pytest.raises(FrozenInstanceError):
            projected.record_count = 99  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            projected.records = iter(())  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            projected.complete = True  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            projected._complete_getter = lambda: True  # type: ignore[misc]
        records = list(projected.records)
        with pytest.raises(TypeError):
            records[0]["gap"] = "tampered"  # type: ignore[index]
        for record in records:
            assert set(record) == {"quality_report_id", "table", "revision_id", "gap"}
            writer.append(record)
    ref = writer.finish()
    with iter_quality_report_stream(
        storage, ref, limits=QualityReportStreamLimits(2, 4096, 2)
    ) as decoded_records:
        round_trip = list(decoded_records)
    assert round_trip == [dict(record) for record in records_for_gap_stream()]


def test_evidence_gap_early_close_releases_readers_and_does_not_advance_count(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_open = storage.open_read
    opened: list[Any] = []

    def tracked(ref: Any) -> Any:
        handle = actual_open(ref)
        opened.append(handle)
        return handle

    monkeypatch.setattr(storage, "open_read", tracked)
    instance = gap_projector(storage, capacity=2)
    projected_gaps: ProjectedCanonicalEvidenceGaps | None = None
    with pytest.raises(
        CanonicalPartitionProjectionError, match="evidence-gap records were not fully consumed"
    ):
        with instance.project_gaps(
            quality_report_id="report-early",
            gaps=[("canonical.trades", f"rev-{index:03d}", "gap") for index in range(12)],
        ) as projected:
            projected_gaps = projected
            next(projected.records)
    assert opened
    assert all(handle.closed for handle in opened)
    assert instance.next_gap_ordinal == 0
    assert projected_gaps is not None and not projected_gaps.complete


def test_evidence_gap_projector_rejects_active_reentry_and_is_one_shot(
    storage: LocalFileStorageAdapter,
) -> None:
    instance = gap_projector(storage)
    source = [("canonical.trades", "rev-a", "gap")]
    with instance.project_gaps(quality_report_id="report-once", gaps=source) as projected:
        with pytest.raises(CanonicalPartitionProjectionError, match="reentrant"):
            with instance.project_gaps(quality_report_id="report-reentrant", gaps=[]):
                pytest.fail("active evidence-gap projector allowed reentry")
        assert list(projected.records) == [
            {
                "quality_report_id": "report-once",
                "table": "canonical.trades",
                "revision_id": "rev-a",
                "gap": "gap",
            }
        ]
    assert projected.complete
    assert instance.next_gap_ordinal == 1
    with pytest.raises(CanonicalPartitionProjectionError, match="one-shot"):
        with instance.project_gaps(quality_report_id="report-twice", gaps=[]):
            pytest.fail("completed evidence-gap projector allowed reuse")


def test_evidence_gap_exception_close_preserves_original_error_and_count(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_open = storage.open_read
    opened: list[Any] = []

    def tracked(ref: Any) -> Any:
        handle = actual_open(ref)
        opened.append(handle)
        return handle

    monkeypatch.setattr(storage, "open_read", tracked)
    instance = gap_projector(storage, capacity=2)
    with pytest.raises(RuntimeError, match="caller aborted"):
        with instance.project_gaps(
            quality_report_id="report-abort",
            gaps=[("canonical.trades", f"rev-{index:03d}", "gap") for index in range(12)],
        ) as projected:
            next(projected.records)
            raise RuntimeError("caller aborted")
    assert opened
    assert all(handle.closed for handle in opened)
    assert instance.next_gap_ordinal == 0


@pytest.mark.parametrize("limit", [0, True])
def test_evidence_gap_record_limit_requires_positive_integer(
    storage: LocalFileStorageAdapter, limit: int | bool
) -> None:
    with pytest.raises(CanonicalPartitionProjectionError, match="max_gap_record_bytes"):
        CanonicalPartitionV3EvidenceGapProjector(
            storage,
            capacity=2,
            merge_fanout=2,
            limits=limits(),
            max_gap_record_bytes=limit,
        )


def records_for_gap_stream() -> list[dict[str, str]]:
    return [
        {
            "quality_report_id": "report-roundtrip",
            "table": "canonical.trades",
            "revision_id": "rev-b",
            "gap": "missing source",
        },
        {
            "quality_report_id": "report-roundtrip",
            "table": "raw.trades",
            "revision_id": "rev-a",
            "gap": "no proof",
        },
    ]


def test_event_id_is_stable_for_different_input_orders(storage: LocalFileStorageAdapter) -> None:
    first = project_one(storage, revision_ids=["rev-c", "rev-a", "rev-b", "rev-a"])
    second = project_one(storage, revision_ids=["rev-b", "rev-c", "rev-a"])
    assert first == second


def test_high_cardinality_event_streams_without_materializing_revision_ids(
    storage: LocalFileStorageAdapter,
) -> None:
    count = 1_200
    ids = (f"rev-{index:05d}" for index in range(count - 1, -1, -1) for _ in (0, 1))
    instance = projector(storage, capacity=13, merge_fanout=3)
    with instance.project_event(
        event_type="competing_heads",
        table="canonical.trades",
        observation_key="key-high-cardinality",
        revision_ids=ids,
        event_start=None,
        event_end=None,
        detail="many heads",
    ) as projected:
        assert projected.event_record["revision_count"] == count
        for expected_index, actual in enumerate(projected.revision_records):
            assert actual == {
                "event_ordinal": 0,
                "revision_id": f"rev-{expected_index:05d}",
            }
        assert expected_index == count - 1
    assert instance.next_revision_ordinal == count


def test_digest_uses_the_versioned_domain_and_length_prefixed_fields(
    storage: LocalFileStorageAdapter,
) -> None:
    revision_ids = ["rev-A", "rev-é", "rev-😀"]
    event = project_one(storage, revision_ids=revision_ids)
    fields = {key: value for key, value in event.items() if key != "event_id"}
    identity = {
        "rule_id": "hlens.quality.canonical-partition",
        "rule_version": "3.0.0",
        "rule_hash": CANONICAL_PARTITION_V3_RULE_HASH,
        "event_fields": fields,
    }
    encoded = canonical_json(identity).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"hlens.quality.canonical-partition@3.0.0/event-id/v1\x00")
    for value in [encoded, *(revision_id.encode("utf-8") for revision_id in sorted(revision_ids))]:
        digest.update(len(value).to_bytes(8, "big", signed=False))
        digest.update(value)
    assert event["event_id"] == f"qevt3-{digest.hexdigest()}"


def test_canonical_json_streaming_matches_full_encoder_for_escaped_unicode(
    storage: LocalFileStorageAdapter,
) -> None:
    event = project_one(storage, revision_ids=['rev-"é'], detail='quote " slash \\ newline\n é')
    fields = {key: value for key, value in event.items() if key != "event_id"}
    identity = {
        "rule_id": "hlens.quality.canonical-partition",
        "rule_version": "3.0.0",
        "rule_hash": CANONICAL_PARTITION_V3_RULE_HASH,
        "event_fields": fields,
    }
    encoded = canonical_json(identity).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"hlens.quality.canonical-partition@3.0.0/event-id/v1\x00")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    revision_id = 'rev-"é'
    revision_bytes = revision_id.encode("utf-8")
    digest.update(len(revision_bytes).to_bytes(8, "big"))
    digest.update(revision_bytes)
    assert event["event_id"] == f"qevt3-{digest.hexdigest()}"


def test_event_record_byte_cap_accepts_exact_line_and_rejects_one_byte_less(
    storage: LocalFileStorageAdapter,
) -> None:
    expected = project_one(storage, revision_ids=["rev-a"], detail="é")
    line_size = len(canonical_json(expected).encode("utf-8")) + 1
    with projector(storage, max_event_record_bytes=line_size).project_event(
        event_type="bar_1m_gap",
        table="canonical.bars_1m",
        observation_key="binance:spot:bar:BTC-USDT:1m:1700000000000",
        revision_ids=["rev-a"],
        event_start=datetime(2023, 11, 14, tzinfo=UTC),
        event_end=datetime(2023, 11, 14, 0, 1, tzinfo=UTC),
        detail="é",
    ) as projected:
        assert dict(projected.event_record) == expected
        list(projected.revision_records)
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with projector(storage, max_event_record_bytes=line_size - 1).project_event(
            event_type="bar_1m_gap",
            table="canonical.bars_1m",
            observation_key="binance:spot:bar:BTC-USDT:1m:1700000000000",
            revision_ids=["rev-a"],
            event_start=datetime(2023, 11, 14, tzinfo=UTC),
            event_end=datetime(2023, 11, 14, 0, 1, tzinfo=UTC),
            detail="é",
        ):
            pytest.fail("oversized event line was accepted")


def test_revision_record_byte_cap_counts_multibyte_utf8_and_rejects_one_byte_less(
    storage: LocalFileStorageAdapter,
) -> None:
    revision_id = "é"
    line_size = len(canonical_json({"event_ordinal": 0, "revision_id": revision_id}).encode()) + 1
    with projector(storage, max_revision_record_bytes=line_size).project_event(
        event_type="competing_heads",
        table="canonical.trades",
        observation_key="key",
        revision_ids=[revision_id],
        event_start=None,
        event_end=None,
        detail="heads",
    ) as projected:
        assert list(projected.revision_records) == [
            {"event_ordinal": 0, "revision_id": revision_id}
        ]
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with projector(storage, max_revision_record_bytes=line_size - 1).project_event(
            event_type="competing_heads",
            table="canonical.trades",
            observation_key="key",
            revision_ids=[revision_id],
            event_start=None,
            event_end=None,
            detail="heads",
        ):
            pytest.fail("oversized revision line was accepted")


def test_large_detail_is_rejected_before_storage_writes(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = storage.stage
    staged = 0

    def tracked(*args: Any, **kwargs: Any) -> Any:
        nonlocal staged
        staged += 1
        return stage(*args, **kwargs)

    monkeypatch.setattr(storage, "stage", tracked)
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with projector(storage, max_event_record_bytes=1024).project_event(
            event_type="bar_1m_gap",
            table="canonical.bars_1m",
            observation_key="key",
            revision_ids=[],
            event_start=None,
            event_end=None,
            detail="d" * 1_000_000,
        ):
            pytest.fail("oversized detail was accepted")
    assert staged == 0


def test_oversized_revision_id_is_rejected_before_storage_writes(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = storage.stage
    staged = 0

    def tracked(*args: Any, **kwargs: Any) -> Any:
        nonlocal staged
        staged += 1
        return stage(*args, **kwargs)

    monkeypatch.setattr(storage, "stage", tracked)
    with pytest.raises(CanonicalPartitionProjectionError, match="byte limit"):
        with projector(storage, max_revision_record_bytes=64).project_event(
            event_type="competing_heads",
            table="canonical.trades",
            observation_key="key",
            revision_ids=["é" * 100],
            event_start=None,
            event_end=None,
            detail="heads",
        ):
            pytest.fail("oversized revision ID was accepted")
    assert staged == 0


def test_texts_reject_unpaired_surrogates_without_replacement(
    storage: LocalFileStorageAdapter,
) -> None:
    with pytest.raises(CanonicalPartitionProjectionError, match="valid UTF-8"):
        project_one(storage, revision_ids=[], detail="bad-\ud800-text")
    with pytest.raises(CanonicalPartitionProjectionError, match="valid UTF-8"):
        project_one(storage, revision_ids=["bad-\ud800-id"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "other"),
        ("table", "canonical.other"),
        ("observation_key", "other-key"),
        ("event_start", datetime(2023, 11, 14, 0, 1, tzinfo=UTC)),
        ("event_end", datetime(2023, 11, 14, 0, 2, tzinfo=UTC)),
        ("detail", "different detail"),
    ],
)
def test_event_id_changes_when_any_fixed_text_or_time_field_changes(
    storage: LocalFileStorageAdapter, field: str, value: Any
) -> None:
    kwargs: dict[str, Any] = {"revision_ids": ["rev-a", "rev-b"]}
    original = project_one(storage, **kwargs)
    kwargs[field] = value
    changed = project_one(storage, **kwargs)
    assert original["event_id"] != changed["event_id"]


def test_event_id_changes_when_revision_identity_changes_but_count_does_not(
    storage: LocalFileStorageAdapter,
) -> None:
    first = project_one(storage, revision_ids=["rev-a", "rev-b"])
    changed = project_one(storage, revision_ids=["rev-a", "rev-c"])
    assert first["revision_count"] == changed["revision_count"] == 2
    assert first["event_id"] != changed["event_id"]


def test_event_id_changes_when_the_revision_count_changes(storage: LocalFileStorageAdapter) -> None:
    first = project_one(storage, revision_ids=["rev-a"])
    changed = project_one(storage, revision_ids=["rev-a", "rev-b"])
    assert first["revision_count"] == 1
    assert changed["revision_count"] == 2
    assert first["event_id"] != changed["event_id"]


def test_revision_first_ordinal_is_part_of_event_identity(storage: LocalFileStorageAdapter) -> None:
    first = projector(storage)
    with first.project_event(
        event_type="report_inputs",
        table=None,
        observation_key=None,
        revision_ids=["prior-revision"],
        event_start=None,
        event_end=None,
        detail="prefix",
    ) as projected:
        list(projected.revision_records)
    with first.project_event(
        event_type="bar_1m_gap",
        table="canonical.bars_1m",
        observation_key="key",
        revision_ids=["rev-a"],
        event_start=None,
        event_end=None,
        detail="gap",
    ) as projected:
        later = dict(projected.event_record)
        list(projected.revision_records)
    early = project_one(storage, revision_ids=["rev-a"])
    assert later["revision_first_ordinal"] == 1
    assert early["revision_first_ordinal"] == 0
    assert later["event_id"] != early["event_id"]


def test_naive_or_non_utc_times_are_rejected(storage: LocalFileStorageAdapter) -> None:
    instance = projector(storage)
    for invalid in (
        datetime(2023, 11, 14),
        datetime(2023, 11, 14, tzinfo=timezone(timedelta(hours=3))),
    ):
        with pytest.raises(CanonicalPartitionProjectionError, match="timezone-aware|UTC offset"):
            with instance.project_event(
                event_type="bar_1m_gap",
                table="canonical.bars_1m",
                observation_key="key",
                revision_ids=[],
                event_start=invalid,
                event_end=None,
                detail="gap",
            ):
                pytest.fail("invalid time was accepted")


def test_required_text_fields_and_ordinal_parameters_are_strict(
    storage: LocalFileStorageAdapter,
) -> None:
    with pytest.raises(CanonicalPartitionProjectionError, match="event_type"):
        project_one(storage, revision_ids=[], event_type=" ")
    with pytest.raises(CanonicalPartitionProjectionError, match="detail"):
        project_one(storage, revision_ids=[], detail="")
    with pytest.raises(CanonicalPartitionProjectionError, match="revision_id"):
        project_one(storage, revision_ids=[""])
    for capacity, merge_fanout in ((True, 2), (2, True), (0, 2), (2, 1)):
        with pytest.raises(CanonicalPartitionProjectionError):
            CanonicalPartitionV3Projector(
                storage,
                capacity=capacity,
                merge_fanout=merge_fanout,
                limits=limits(),
                max_event_record_bytes=4096,
                max_revision_record_bytes=4096,
            )
    for event_limit, revision_limit in ((0, 4096), (4096, 0), (True, 4096), (4096, False)):
        with pytest.raises(CanonicalPartitionProjectionError):
            projector(
                storage,
                max_event_record_bytes=event_limit,
                max_revision_record_bytes=revision_limit,
            )


def test_early_close_releases_run_readers_and_prevents_ordinal_gap(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_open = storage.open_read
    opened: list[Any] = []

    def tracked(ref: Any) -> Any:
        handle = actual_open(ref)
        opened.append(handle)
        return handle

    monkeypatch.setattr(storage, "open_read", tracked)
    instance = projector(storage, capacity=2)
    with pytest.raises(RuntimeError, match="stop early"):
        with instance.project_event(
            event_type="competing_heads",
            table="canonical.trades",
            observation_key="key",
            revision_ids=[f"rev-{index:03d}" for index in range(12)],
            event_start=None,
            event_end=None,
            detail="many heads",
        ) as projected:
            next(projected.revision_records)
            raise RuntimeError("stop early")
    assert opened
    assert all(handle.closed for handle in opened)
    assert instance.next_event_ordinal == 0
    assert instance.next_revision_ordinal == 0
    with pytest.raises(CanonicalPartitionProjectionError, match="failed after an incomplete event"):
        with instance.project_event(
            event_type="another",
            table="canonical.trades",
            observation_key="key-2",
            revision_ids=[],
            event_start=None,
            event_end=None,
            detail="must not continue",
        ):
            pytest.fail("failed projector accepted another event")


def test_normal_exit_without_consuming_revisions_raises_integrity_error(
    storage: LocalFileStorageAdapter,
) -> None:
    instance = projector(storage)
    with pytest.raises(
        CanonicalPartitionProjectionError, match="revision records were not fully consumed"
    ):
        with instance.project_event(
            event_type="competing_heads",
            table="canonical.trades",
            observation_key="key",
            revision_ids=["rev-a", "rev-b"],
            event_start=None,
            event_end=None,
            detail="heads not read",
        ):
            pass
    assert instance.next_event_ordinal == 0
    assert instance.next_revision_ordinal == 0
