from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest

from core.contracts.storage import ObjectRef, StorageAdapter
from infrastructure.dataset.builder import DatasetQualityError
from infrastructure.dataset.quality import BoundedQualityEvidence, _reject_local_storage_aliases
from infrastructure.quality.report_streams import (
    QualityReportStreamIntegrityError,
    QualityReportStreamLimits,
    QualityReportStreamRef,
    QualityReportStreamWriter,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits


def _report_source(
    evidence: StorageAdapter,
    scratch: StorageAdapter,
    ref: QualityReportStreamRef,
    *,
    stream_storage: Any = None,
) -> BoundedQualityEvidence:
    source = cast(Any, object.__new__(BoundedQualityEvidence))
    source._active_report_id = "report-1"
    source._active_manifest = {
        "evidence_gaps": {
            "format_id": ref.format,
            "record_count": ref.record_count,
            "leaf_count": ref.leaf_count,
            "depth": ref.depth,
            "root_key": ref.root_key,
            "root_sha256": ref.root_sha256,
            "root_size": ref.root_size,
        }
    }
    source._storage = evidence if stream_storage is None else stream_storage
    source._scratch = scratch
    source._stream_limits = QualityReportStreamLimits(
        leaf_max_records=1, leaf_max_bytes=4096, fanout=2
    )
    source._run_capacity = 3
    source._merge_fanout = 2
    source._run_limits = RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2)
    source._max_run_object_bytes = 8192
    source._claims = None
    return cast(BoundedQualityEvidence, source)


def _gap_root(storage: StorageAdapter, count: int) -> QualityReportStreamRef:
    writer = QualityReportStreamWriter(
        storage,
        "evidence_gaps",
        limits=QualityReportStreamLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2),
    )
    for ordinal in range(count):
        writer.append(
            {
                "quality_report_id": "report-1",
                "table": "raw.table",
                "revision_id": f"revision-{ordinal:05}",
                "gap": "missing archive evidence",
            }
        )
    return writer.finish()


class _CountingStorage:
    def __init__(self, inner: StorageAdapter) -> None:
        self.inner = inner
        self.opens = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def open_read(self, ref: ObjectRef) -> Any:
        self.opens += 1
        return self.inner.open_read(ref)


class _CorruptFinalLeaf(_CountingStorage):
    @contextmanager
    def open_read(self, ref: ObjectRef) -> Iterator[Any]:
        self.opens += 1
        if self.opens == 3:
            yield BytesIO(b"tampered-after-first-match\n")
        else:
            with self.inner.open_read(ref) as stream:
                yield stream


def test_distinct_local_adapters_cannot_alias_quality_scratch_roots(tmp_path: Path) -> None:
    evidence = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    scratch = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    try:
        with pytest.raises(DatasetQualityError, match="roots must not overlap"):
            _reject_local_storage_aliases(evidence, scratch)
    finally:
        evidence.close()
        scratch.close()


def test_disjoint_local_quality_scratch_roots_are_allowed(tmp_path: Path) -> None:
    evidence = LocalFileStorageAdapter(
        (tmp_path / "evidence" / "warehouse").as_uri(),
        (tmp_path / "evidence" / "staging").as_uri(),
    )
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch" / "warehouse").as_uri(),
        (tmp_path / "scratch" / "staging").as_uri(),
    )
    try:
        _reject_local_storage_aliases(evidence, scratch)
    finally:
        evidence.close()
        scratch.close()


def test_gap_join_scans_many_claims_in_one_verified_stream_pass(tmp_path: Path) -> None:
    evidence = LocalFileStorageAdapter(
        (tmp_path / "evidence-warehouse").as_uri(), (tmp_path / "evidence-stage").as_uri()
    )
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    try:
        count = 96
        ref = _gap_root(evidence, count)
        counted = _CountingStorage(evidence)
        source = _report_source(evidence, scratch, ref, stream_storage=counted)
        for ordinal in reversed(range(count)):
            source.claim_gap(
                "report-1",
                "raw.table",
                f"revision-{ordinal:05}",
                "missing archive evidence",
            )
        source.finish_reports()
        assert counted.opens < count * 4
        source.close()
    finally:
        evidence.close()
        scratch.close()


def test_gap_join_authenticates_stream_tail_after_matching_first_claim(tmp_path: Path) -> None:
    evidence = LocalFileStorageAdapter(
        (tmp_path / "evidence-warehouse").as_uri(), (tmp_path / "evidence-stage").as_uri()
    )
    scratch = LocalFileStorageAdapter(
        (tmp_path / "scratch-warehouse").as_uri(), (tmp_path / "scratch-stage").as_uri()
    )
    try:
        ref = _gap_root(evidence, 2)
        corrupt = _CorruptFinalLeaf(evidence)
        source = _report_source(evidence, scratch, ref, stream_storage=corrupt)
        source.claim_gap("report-1", "raw.table", "revision-00000", "missing archive evidence")
        with pytest.raises(QualityReportStreamIntegrityError):
            source.finish_reports()
        source.close()
    finally:
        evidence.close()
        scratch.close()
