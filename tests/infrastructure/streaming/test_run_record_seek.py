"""Bounded ordinal lookup over the existing content-addressed sorted-run format."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from core.contracts.storage import ObjectRef, StorageAdapter
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import (
    RunIntegrityError,
    RunLimits,
    RunRef,
    RunWriteError,
    _read_run_record_at_ordinal,
    write_sorted_run,
)


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


def _storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def test_ordinal_lookup_seeks_across_multilevel_pages_and_closes_readers(
    tmp_path: Path,
) -> None:
    storage = _TrackingStorage(_storage(tmp_path))
    rows = [{"revision_id": f"revision-{index:04d}", "ordinal": index} for index in range(129)]
    limits = RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2)
    ref = write_sorted_run(storage, rows, limits)
    assert ref.depth >= 5

    first = _read_run_record_at_ordinal(storage, ref, 0, max_object_bytes=limits.leaf_max_bytes)
    middle = _read_run_record_at_ordinal(storage, ref, 64, max_object_bytes=limits.leaf_max_bytes)
    last = _read_run_record_at_ordinal(storage, ref, 128, max_object_bytes=limits.leaf_max_bytes)

    assert (first, middle, last) == (rows[0], rows[64], rows[128])
    assert storage.opened == storage.closed
    assert storage.opened <= 3 * (ref.depth + 1)
    with pytest.raises(IndexError):
        _read_run_record_at_ordinal(storage, ref, ref.record_count, max_object_bytes=4096)
    with pytest.raises(RunWriteError, match="ordinal"):
        _read_run_record_at_ordinal(storage, ref, -1, max_object_bytes=4096)


def test_ordinal_lookup_fails_closed_on_inconsistent_reference_or_object_bound(
    tmp_path: Path,
) -> None:
    storage = _TrackingStorage(_storage(tmp_path))
    limits = RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2)
    ref = write_sorted_run(
        storage,
        [{"revision_id": f"revision-{index:04d}", "ordinal": index} for index in range(8)],
        limits,
    )
    wrong_count = dataclasses.replace(ref, record_count=ref.record_count + 1)
    with pytest.raises(RunIntegrityError, match="index header"):
        _read_run_record_at_ordinal(storage, wrong_count, 0, max_object_bytes=4096)

    with pytest.raises(RunIntegrityError, match="byte bound"):
        _read_run_record_at_ordinal(storage, ref, 0, max_object_bytes=1)
    assert storage.opened == storage.closed


def test_ordinal_lookup_rejects_non_integer_bounds(tmp_path: Path) -> None:
    storage = _TrackingStorage(_storage(tmp_path))
    ref: RunRef = write_sorted_run(
        storage,
        [{"revision_id": "revision-0000", "ordinal": 0}],
        RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2),
    )
    with pytest.raises(RunWriteError, match="max_object_bytes"):
        _read_run_record_at_ordinal(storage, ref, 0, max_object_bytes=True)
