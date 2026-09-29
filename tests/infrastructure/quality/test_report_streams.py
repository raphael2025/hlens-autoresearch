"""ADR-0093 JSONL tree substrate: limits, content addressing, integrity and early close."""

from __future__ import annotations

import itertools
import json
import tracemalloc
from collections.abc import Iterator, Mapping
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from core.contracts.storage import ObjectRef, StorageAdapter
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
