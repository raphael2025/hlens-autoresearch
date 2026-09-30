"""Shared sorted-run context and iterable contracts."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from core.contracts.storage import (
    ObjectRef,
    PublishResult,
    StagedObject,
    StageRequest,
    StorageAdapter,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming import runs as runs_module
from infrastructure.streaming.runs import RunLimits, RunRef, merge_sorted_runs, write_sorted_run

_LIMITS = RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2)


def _storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


class _ObservedStorage:
    def __init__(self, storage: StorageAdapter) -> None:
        self._storage = storage
        self.opened = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def lookup(self, key: str) -> ObjectRef | None:
        return self._storage.lookup(key)

    def open_read(self, ref: ObjectRef) -> Any:
        self.opened += 1
        return self._storage.open_read(ref)


class _WriteCountingStorage(_ObservedStorage):
    def __init__(self, storage: StorageAdapter) -> None:
        super().__init__(storage)
        self.staged = 0
        self.published = 0

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> StagedObject:
        self.staged += 1
        return self._storage.stage(request, content)

    def publish(self, staged: StagedObject) -> PublishResult:
        self.published += 1
        return self._storage.publish(staged)


def test_merge_folds_lazy_run_refs_online_without_materializing_the_iterable(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path)
    observed = _ObservedStorage(storage)
    rows = [{"ordinal": ordinal} for ordinal in range(17)]

    def refs() -> Iterator[RunRef]:
        for index, row in enumerate(reversed(rows)):
            if index == 2:
                # With fanout=2, the first pair must already have been folded before the
                # caller asks this one-shot source for another reference.
                assert observed.opened > 0
            yield write_sorted_run(storage, [row], _LIMITS)

    merge = merge_sorted_runs(
        cast(StorageAdapter, observed),
        refs(),
        key=lambda row: row["ordinal"],
        merge_fanout=2,
        limits=_LIMITS,
    )
    assert observed.opened == 0  # constructing the context does not consume the source
    with merge as records:
        assert list(records) == rows
    assert observed.opened > 0
    storage.close()


def test_exactly_fanout_refs_merge_without_writing_an_intermediate_run(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = [{"ordinal": ordinal} for ordinal in range(2)]
    refs = [write_sorted_run(storage, [row], _LIMITS) for row in reversed(rows)]
    observed = _WriteCountingStorage(storage)

    with merge_sorted_runs(
        cast(StorageAdapter, observed),
        iter(refs),
        key=lambda row: row["ordinal"],
        merge_fanout=2,
        limits=_LIMITS,
    ) as records:
        assert list(records) == rows

    assert observed.staged == 0
    assert observed.published == 0
    assert observed.opened > 0
    storage.close()


def test_exact_fanout_prefetch_is_discarded_when_context_closes_early(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    refs = [write_sorted_run(storage, [{"ordinal": ordinal}], _LIMITS) for ordinal in range(2)]
    context = merge_sorted_runs(
        storage,
        iter(refs),
        key=lambda row: row["ordinal"],
        merge_fanout=2,
        limits=_LIMITS,
    )
    with context as records:
        pass
    with pytest.raises(StopIteration):
        next(records)
    storage.close()


def test_merge_iterator_closes_before_its_reader_stack(tmp_path: Path, monkeypatch: Any) -> None:
    events: list[str] = []
    row = {"ordinal": 0}
    ref = RunRef(
        root=runs_module.RunObjectRef(key="unused", sha256="0" * 64, size=1),
        record_count=1,
        leaf_count=1,
        depth=1,
    )

    @contextmanager
    def fake_iter_run(
        _storage: StorageAdapter, _ref: RunRef
    ) -> Iterator[Iterator[Mapping[str, Any]]]:
        yield iter((row,))
        events.append("reader stack closed")

    class _MergeIterator:
        def __iter__(self) -> _MergeIterator:
            return self

        def __next__(self) -> Mapping[str, Any]:
            return row

        def close(self) -> None:
            events.append("merge iterator closed")

    monkeypatch.setattr(runs_module, "iter_run", fake_iter_run)
    monkeypatch.setattr(heapq, "merge", lambda *_args, **_kwargs: _MergeIterator())

    context = merge_sorted_runs(
        cast(StorageAdapter, object()),
        (ref,),
        key=lambda value: value["ordinal"],
        merge_fanout=2,
        limits=_LIMITS,
    )
    with context as records:
        assert next(records) is row
    assert events == ["merge iterator closed", "reader stack closed"]
