"""Spilled per-key row index removes the bounded selector's key-sized Python dictionary."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import ObjectRef, StorageAdapter
from infrastructure.pit.runs import RunLimits, RunRef
from infrastructure.pit.selector import (
    PitRunParams,
    _evaluate,
    _index_entry,
    _IndexedAvailabilityView,
    _RevisionRecordView,
    _RunBackedKeyRows,
)
from infrastructure.revision.channel_reconcile import revision_record_from_row
from infrastructure.streaming.content_key_tree import ContentKeyTree, KeyTreeParams
from infrastructure.streaming.runs import write_sorted_run
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import FAR, _chain
from tests.infrastructure.revision.rest_store_support import RestHarness


class _TrackingStorage:
    def __init__(self, storage: StorageAdapter) -> None:
        self._storage = storage
        self.opened = 0
        self.closed = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def lookup(self, key: str) -> ObjectRef | None:
        return self._storage.lookup(key)

    def open_read(self, ref: ObjectRef) -> Any:
        self.opened += 1
        return _CountingHandle(self._storage.open_read(ref), self)


class _CountingHandle:
    def __init__(self, handle: Any, storage: _TrackingStorage) -> None:
        self._handle = handle
        self._storage = storage
        self._closed = False

    def __enter__(self) -> _CountingHandle:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        return bytes(self._handle.read(size))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._storage.closed += 1
            self._handle.close()


def _history(h: RestHarness, size: int = 257) -> tuple[dict[str, Any], ...]:
    _chain(h)
    base = h.rows(c.TRADES)[0]
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for index in range(size):
        revision_id = f"synthetic-{index:04d}"
        at = start + timedelta(seconds=((index * 7) % 17) + 1)
        row = dict(base)
        row.update(
            revision_id=revision_id,
            source_id=f"source-{index:04d}",
            payload_hash=hashlib.sha256(f"payload-{index}".encode()).hexdigest(),
            arrival_seq=index,
            source_revision_id=None,
            source_revision_time=None,
            supersedes=[],
            available_time=at,
            ingest_time=at,
            knowledge_time=FAR,
            source_time=None,
        )
        rows.append(row)
    return tuple(rows)


def _indexed_rows(
    storage: StorageAdapter, rows: tuple[dict[str, Any], ...], limits: RunLimits
) -> tuple[RunRef, ContentKeyTree, _RunBackedKeyRows]:
    run = write_sorted_run(storage, rows, limits)
    index = ContentKeyTree.build(
        storage,
        (
            ((row["revision_id"],), _index_entry(ordinal, row["available_time"]))
            for ordinal, row in enumerate(rows)
        ),
        params=KeyTreeParams(
            page_max_bytes=limits.leaf_max_bytes,
            leaf_max_records=8,
            fanout=2,
        ),
    )
    return (
        run,
        index,
        _RunBackedKeyRows(
            storage,
            run,
            index,
            max_object_bytes=limits.leaf_max_bytes,
        ),
    )


def test_spilled_key_index_seeks_rows_and_replays_multitime_selector(h: RestHarness) -> None:
    rows = tuple(sorted(_history(h), key=lambda row: row["revision_id"]))
    limits = RunLimits(leaf_max_records=8, leaf_max_bytes=1 << 16, fanout=2)
    storage = _TrackingStorage(cast(StorageAdapter, h.storage))
    run, index, indexed = _indexed_rows(storage, rows, limits)

    assert indexed[rows[0]["revision_id"]] == rows[0]
    assert indexed[rows[128]["revision_id"]] == rows[128]
    assert indexed[rows[-1]["revision_id"]] == rows[-1]
    with pytest.raises(KeyError):
        indexed["missing-revision"]
    assert index.root_ref is not None

    storage.opened = 0
    storage.closed = 0
    selected_row = indexed[rows[128]["revision_id"]]
    assert selected_row == rows[128]
    index_depth = (index.root_ref.level + 1) if index.root_ref is not None else 0
    assert storage.opened <= index_depth + run.depth + 1
    assert storage.opened == storage.closed

    records = _RevisionRecordView(indexed)
    expected_records = tuple(revision_record_from_row(row) for row in rows)
    assert tuple(records) == expected_records
    assert tuple(records) == expected_records

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=20)
    spec = cast(
        PointInTimeSpec,
        SimpleNamespace(
            knowledge_cutoff=FAR,
            simulation_time=None,
            simulation_start=start,
            simulation_end=end,
        ),
    )
    available = _IndexedAvailabilityView(index)
    params = PitRunParams(
        row_batch_rows=8,
        edge_batch_rows=8,
        merge_fanout=2,
        key_history_buffer=1,
        limits=limits,
    )
    before = storage.opened
    try:
        indexed_results = list(
            _evaluate(
                "one-key",
                records,
                (),
                spec,
                available,
                run_storage=storage,
                run_params=params,
            )
        )
    finally:
        available.close()
    indexed_read_count = storage.opened - before
    evaluation_passes = len(indexed_results) + 1  # one timeline scan plus each result instant
    run_page_bound = 2 * run.leaf_count + run.depth + 1
    index_leaf_bound = math.ceil(len(rows) / 8)
    index_page_bound = 2 * index_leaf_bound + index.root_ref.level + 1  # type: ignore[union-attr]
    read_bound = evaluation_passes * (run_page_bound + index_page_bound)
    materialized_results = list(
        _evaluate(
            "one-key",
            expected_records,
            (),
            spec,
            {row["revision_id"]: row["available_time"] for row in rows},
        )
    )

    assert indexed_results == materialized_results
    assert len(indexed_results) == 18
    assert indexed_results[0].status is PointInTimeStatus.ABSENT
    assert all(result.status is PointInTimeStatus.CONFLICT for result in indexed_results[1:])
    assert 0 < indexed_read_count <= read_bound
    print(
        f"257-row / 18-instant evaluation opened {indexed_read_count} pages; "
        f"structural upper bound {read_bound}"
    )
    assert storage.opened == storage.closed
