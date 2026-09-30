"""ADR-0093 JSONL tree substrate: limits, content addressing, integrity and early close."""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import tracemalloc
from collections.abc import Iterator, Mapping
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from core.contracts.storage import ObjectRef, PublishOutcome, StageRequest, StorageAdapter
from core.domain.base import canonical_json
from infrastructure.quality.report_streams import (
    QUALITY_REPORT_STREAM_FORMAT,
    QualityReportStreamError,
    QualityReportStreamIntegrityError,
    QualityReportStreamLimits,
    QualityReportStreamRef,
    QualityReportStreamTooLarge,
    QualityReportStreamWriter,
    iter_quality_report_stream,
)
from infrastructure.storage import LocalFileStorageAdapter


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorageAdapter:
    return LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )


def params(records: int = 3, size: int = 4096, fanout: int = 2) -> QualityReportStreamLimits:
    return QualityReportStreamLimits(records, size, fanout)


def write(
    storage: StorageAdapter,
    values: list[dict[str, Any]],
    limits: QualityReportStreamLimits,
) -> QualityReportStreamRef:
    writer = QualityReportStreamWriter(storage, "events", limits=limits)
    for value in values:
        writer.append(value)
    return writer.finish()


def test_high_cardinality_round_trip_is_ordered_and_tree_fanout_is_bounded(
    storage: LocalFileStorageAdapter,
) -> None:
    limits = params(records=2, fanout=2)
    values = [{"ordinal": index, "event_type": "finding"} for index in range(257)]
    ref = write(storage, values, limits)
    assert ref.format == QUALITY_REPORT_STREAM_FORMAT
    assert (ref.record_count, ref.leaf_count, ref.depth) == (257, 129, 8)
    root = storage.lookup(ref.root_key)
    assert root is not None
    with storage.open_read(root) as handle:
        lines = handle.read().splitlines()
    assert len(lines) - 1 <= limits.fanout
    with iter_quality_report_stream(storage, ref, limits=limits) as records:
        assert list(records) == values


def test_empty_stream_publishes_a_childless_root(storage: LocalFileStorageAdapter) -> None:
    limits = params()
    ref = write(storage, [], limits)
    assert (ref.record_count, ref.leaf_count, ref.depth) == (0, 0, 1)
    with iter_quality_report_stream(storage, ref, limits=limits) as records:
        assert list(records) == []


@pytest.mark.parametrize("stream", ["events", "event_revisions", "evidence_gaps"])
def test_each_adr0093_stream_name_is_supported(
    storage: LocalFileStorageAdapter, stream: str
) -> None:
    writer = QualityReportStreamWriter(storage, stream, limits=params())
    ref = writer.finish()
    assert ref.stream == stream
    with iter_quality_report_stream(storage, ref, limits=params()) as records:
        assert list(records) == []


def test_successful_finish_releases_the_index_frontier(
    storage: LocalFileStorageAdapter,
) -> None:
    writer = QualityReportStreamWriter(storage, "events", limits=params(records=1, fanout=2))
    for ordinal in range(5):
        writer.append({"ordinal": ordinal})
    assert any(writer._levels)
    writer.finish()
    assert writer._levels == []
    assert writer._leaf == []


def test_limits_are_all_explicit_and_positive() -> None:
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in inspect.signature(QualityReportStreamLimits).parameters.values()
    )
    with pytest.raises(TypeError):
        QualityReportStreamLimits()  # type: ignore[call-arg]
    for values in ((0, 1, 2), (1, 0, 2), (1, 1, 1), (True, 1, 2)):
        with pytest.raises(QualityReportStreamError):
            QualityReportStreamLimits(*values)


def test_record_projection_matches_canonical_json_for_nested_values(
    storage: LocalFileStorageAdapter,
) -> None:
    limits = params(size=1024)
    value = {
        "z": [None, True, False, -0.0, -17, 1.25e-7, "中文\b\f\t\r\n\x01\u2028"],
        "a": {"nested": "value"},
    }
    ref = write(storage, [value], limits)
    root = storage.lookup(ref.root_key)
    assert root is not None
    with storage.open_read(root) as handle:
        body = handle.read()
    leaf_key = json.loads(body.splitlines()[1])["key"]
    leaf_ref = storage.lookup(leaf_key)
    assert leaf_ref is not None
    with storage.open_read(leaf_ref) as handle:
        leaf_lines = handle.read().splitlines()
    assert leaf_lines[1] + b"\n" == (canonical_json(value) + "\n").encode("utf-8")
    with iter_quality_report_stream(storage, ref, limits=limits) as records:
        assert list(records) == [value]


def test_record_over_limit_fails_without_truncation(storage: LocalFileStorageAdapter) -> None:
    writer = QualityReportStreamWriter(storage, "events", limits=params(size=20))
    with pytest.raises(QualityReportStreamTooLarge):
        writer.append({"payload": "x" * 30})


def test_exact_record_and_leaf_byte_limits_are_inclusive(
    storage: LocalFileStorageAdapter,
) -> None:
    # Empty mappings have no key-index workspace, so their JSONL bytes can exercise the
    # inclusive byte boundary without a separately budgeted sorting index.
    values: list[dict[str, Any]] = [{}, {}]
    line_bytes = [len((canonical_json(value) + "\n").encode("utf-8")) for value in values]
    writer = QualityReportStreamWriter(
        storage,
        "events",
        limits=QualityReportStreamLimits(
            leaf_max_records=2, leaf_max_bytes=sum(line_bytes), fanout=2
        ),
    )
    for value in values:
        writer.append(value)
    ref = writer.finish()
    assert (ref.record_count, ref.leaf_count) == (2, 1)
    with iter_quality_report_stream(
        storage,
        ref,
        limits=QualityReportStreamLimits(
            leaf_max_records=2, leaf_max_bytes=sum(line_bytes), fanout=2
        ),
    ) as records:
        assert list(records) == values


def test_record_that_exceeds_exact_byte_limit_by_one_fails(
    storage: LocalFileStorageAdapter,
) -> None:
    value: dict[str, Any] = {}
    size = len((canonical_json(value) + "\n").encode("utf-8"))
    writer = QualityReportStreamWriter(
        storage, "events", limits=QualityReportStreamLimits(1, size - 1, 2)
    )
    with pytest.raises(QualityReportStreamTooLarge):
        writer.append(value)


def test_huge_string_is_rejected_with_bounded_temporary_allocation(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = QualityReportStreamWriter(storage, "events", limits=params(size=64))
    writer.append({"prior": 1})  # buffered state must be discarded when a later append fails
    large_value = "x" * (8 * 1024 * 1024)
    published: list[str] = []
    monkeypatch.setattr(storage, "stage", lambda *args, **kwargs: published.append("stage"))
    monkeypatch.setattr(storage, "publish", lambda *args, **kwargs: published.append("publish"))
    tracemalloc.start()
    try:
        with pytest.raises(QualityReportStreamTooLarge):
            writer.append({"payload": large_value})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024
    assert published == []
    assert writer.failed
    assert writer._leaf == []
    with pytest.raises(QualityReportStreamError, match="already finished|failed"):
        writer.finish()
    writer.close()
    assert writer._levels == []


def test_infinite_lazy_mapping_stops_at_byte_bound_without_reading_values(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InfiniteMapping(Mapping[str, Any]):
        def __init__(self) -> None:
            self.keys_yielded = 0
            self.value_reads = 0

        def __iter__(self) -> Iterator[str]:
            for index in itertools.count():
                self.keys_yielded += 1
                yield f"key-{index}"

        def __len__(self) -> int:
            raise AssertionError("the bounded encoder must not ask for mapping length")

        def __getitem__(self, key: str) -> Any:
            self.value_reads += 1
            return "value"

    record = InfiniteMapping()
    writer = QualityReportStreamWriter(storage, "events", limits=params(size=64))
    writer.append({"prior": 1})
    published: list[str] = []
    monkeypatch.setattr(storage, "stage", lambda *args, **kwargs: published.append("stage"))
    with pytest.raises(QualityReportStreamTooLarge):
        writer.append(record)
    assert record.keys_yielded < 20
    assert record.value_reads == 0
    assert published == []
    assert writer.failed
    writer.close()
    assert writer._leaf == [] and writer._levels == []


def test_tampered_root_bytes_fail_closed(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = write(storage, [{"ordinal": 1}], params())
    actual_open = storage.open_read

    def tampered(object_ref: ObjectRef) -> BinaryIO:
        with actual_open(object_ref) as handle:
            body = handle.read()
        return BytesIO(body + b" ") if object_ref.key == ref.root_key else BytesIO(body)

    monkeypatch.setattr(storage, "open_read", tampered)
    with iter_quality_report_stream(storage, ref, limits=params()) as records:
        with pytest.raises(QualityReportStreamIntegrityError, match="digest"):
            list(records)


def test_missing_child_object_fails_closed(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = write(storage, [{"ordinal": 0}], params(records=1))
    root = storage.lookup(ref.root_key)
    assert root is not None
    with storage.open_read(root) as handle:
        child_key = json.loads(handle.read().splitlines()[1])["key"]
    actual_lookup = storage.lookup

    def missing(key: str) -> ObjectRef | None:
        if key == child_key:
            return None
        return actual_lookup(key)

    monkeypatch.setattr(storage, "lookup", missing)
    with iter_quality_report_stream(storage, ref, limits=params(records=1)) as records:
        with pytest.raises(QualityReportStreamIntegrityError, match="missing or misidentified"):
            list(records)


def test_lookup_reference_identity_mismatch_fails_before_open_read(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = write(storage, [{"ordinal": 0}], params())
    actual_lookup = storage.lookup
    opened: list[str] = []
    actual_open = storage.open_read

    def mismatched(key: str) -> ObjectRef | None:
        found = actual_lookup(key)
        if key == ref.root_key and found is not None:
            return found.model_copy(update={"size": found.size + 1})
        return found

    def tracked(found: ObjectRef) -> BinaryIO:
        opened.append(found.key)
        return actual_open(found)

    monkeypatch.setattr(storage, "lookup", mismatched)
    monkeypatch.setattr(storage, "open_read", tracked)
    with pytest.raises(QualityReportStreamIntegrityError, match="missing or misidentified"):
        with iter_quality_report_stream(storage, ref, limits=params()) as records:
            list(records)
    assert opened == []


def test_malformed_but_content_addressed_root_fails_closed(
    storage: LocalFileStorageAdapter,
) -> None:
    ref = write(storage, [{"ordinal": 0}], params())
    malformed = (
        b'{"first_ordinal":0,"format":"wrong","level":1,"node":"index",'
        b'"record_count":1,"stream":"events"}\n'
    )
    digest = hashlib.sha256(malformed).hexdigest()
    key = f"quality/report-evidence/v1/{digest}.jsonl"
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=digest, expected_size=len(malformed)), [malformed]
    )
    root = storage.publish(staged).ref
    bad_ref = replace(ref, root_key=root.key, root_sha256=root.sha256, root_size=root.size)
    with pytest.raises(QualityReportStreamIntegrityError, match="identity or level mismatch"):
        with iter_quality_report_stream(storage, bad_ref, limits=params()) as records:
            list(records)


def test_root_summary_count_mismatch_fails_closed(storage: LocalFileStorageAdapter) -> None:
    ref = write(storage, [{"ordinal": 0}], params())
    bad_ref = replace(ref, record_count=2)
    with pytest.raises(QualityReportStreamIntegrityError, match="parent reference|counts disagree"):
        with iter_quality_report_stream(storage, bad_ref, limits=params()) as records:
            list(records)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stream", None),
        ("stream", []),
        ("format", None),
        ("format", []),
        ("root_key", None),
        ("root_key", []),
        ("root_sha256", None),
        ("root_sha256", []),
        ("record_count", True),
        ("leaf_count", 1.5),
        ("depth", "1"),
        ("root_size", -1),
    ],
)
def test_malformed_descriptor_fields_raise_integrity_error(
    storage: LocalFileStorageAdapter, field: str, value: Any
) -> None:
    ref = write(storage, [], params())
    malformed = replace(ref, **{field: value})
    with pytest.raises(QualityReportStreamIntegrityError):
        with iter_quality_report_stream(storage, malformed, limits=params()) as records:
            list(records)


def test_wrong_descriptor_runtime_object_raises_integrity_error(
    storage: LocalFileStorageAdapter,
) -> None:
    descriptor: Any = object()
    with pytest.raises(QualityReportStreamIntegrityError, match="invalid runtime type"):
        with iter_quality_report_stream(storage, descriptor, limits=params()) as records:
            list(records)


def test_replay_republishes_identical_objects_idempotently(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_publish = storage.publish
    outcomes: list[PublishOutcome] = []

    def tracked(staged: Any) -> Any:
        result = actual_publish(staged)
        outcomes.append(result.outcome)
        return result

    monkeypatch.setattr(storage, "publish", tracked)
    values = [{"ordinal": index} for index in range(5)]
    first = write(storage, values, params(records=2, fanout=2))
    first_publish_count = len(outcomes)
    second = write(storage, values, params(records=2, fanout=2))
    assert second == first
    assert first_publish_count > 0
    assert len(outcomes) == first_publish_count * 2
    assert outcomes[:first_publish_count] == [PublishOutcome.CREATED] * first_publish_count
    assert outcomes[first_publish_count:] == [PublishOutcome.ALREADY_PRESENT] * first_publish_count


def test_early_close_stops_before_later_leaf_reads(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = params(records=1, fanout=2)
    ref = write(storage, [{"ordinal": index} for index in range(9)], limits)
    actual_open = storage.open_read
    opened: list[str] = []

    def tracked(object_ref: ObjectRef) -> BinaryIO:
        opened.append(object_ref.key)
        return actual_open(object_ref)

    monkeypatch.setattr(storage, "open_read", tracked)
    with iter_quality_report_stream(storage, ref, limits=limits) as records:
        assert next(records) == {"ordinal": 0}
    assert len(opened) < ref.leaf_count + ref.depth
