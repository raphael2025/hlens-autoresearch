"""Bounded normalizer proof staging used only by PIT v3."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pytest

from core.contracts.storage import StageRequest
from infrastructure.canonical.normalizer import CanonicalNormalizer
from infrastructure.pit import selector as selector_module
from infrastructure.pit.runs import RunLimits, RunSetBuilder
from infrastructure.pit.selector import PitRunParams, PitSelector
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import END, K_A, K_E, START, _spec
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, StepClock

_PARAMS = PitRunParams(
    row_batch_rows=1,
    edge_batch_rows=1,
    merge_fanout=2,
    key_history_buffer=1,
    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=1 << 16, fanout=2),
)


class _CountingReader:
    def __init__(self, inner: Any, owner: _CountingStorage) -> None:
        self._inner = inner
        self._owner = owner
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        return self._inner.read(size)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.closed_readers += 1
            self._inner.close()

    def __enter__(self) -> _CountingReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _CountingStorage:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.stage_objects = 0
        self.stage_bytes = 0
        self.open_readers = 0
        self.closed_readers = 0
        self.lookups = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def stage(self, request: StageRequest, content: Iterable[bytes]) -> Any:
        self.stage_objects += 1

        def counted() -> Iterator[bytes]:
            for chunk in content:
                self.stage_bytes += len(chunk)
                yield chunk

        return self._inner.stage(request, counted())

    def open_read(self, ref: Any) -> _CountingReader:
        self.open_readers += 1
        return _CountingReader(self._inner.open_read(ref), self)

    def lookup(self, key: str) -> Any:
        self.lookups += 1
        return self._inner.lookup(key)


class _ObservedProofBuilder(RunSetBuilder):
    def __init__(self, storage: Any, **kwargs: Any) -> None:
        self.counting_storage = _CountingStorage(storage)
        self.add_count = 0
        self.max_buffered_rows = 0
        self.max_pending_refs = 0
        self.max_ref_levels = 0
        self.finish_calls = 0
        self.root = None
        super().__init__(self.counting_storage, **kwargs)

    def add(self, row: Mapping[str, Any]) -> None:
        super().add(row)
        self.add_count += 1
        self.max_pending_refs = max(
            self.max_pending_refs, sum(len(level) for level in self._refs._levels)
        )
        self.max_ref_levels = max(self.max_ref_levels, len(self._refs._levels))

    def _flush(self) -> None:
        self.max_buffered_rows = max(self.max_buffered_rows, len(self._rows))
        super()._flush()

    def finish(self) -> Any:
        self.finish_calls += 1
        self.root = super().finish()
        return self.root


def _observe_proof_builders(monkeypatch: pytest.MonkeyPatch) -> list[_ObservedProofBuilder]:
    observed: list[_ObservedProofBuilder] = []
    original = selector_module.RunSetBuilder

    class CapturingBuilder(original):
        def __new__(cls, storage: Any, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("key") is selector_module._proof_row_sort_key:
                instance = _ObservedProofBuilder(storage, **kwargs)
                observed.append(instance)
                return instance
            return super().__new__(cls)

        def __init__(self, storage: Any, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("key") is selector_module._proof_row_sort_key:
                return
            super().__init__(storage, *args, **kwargs)

    monkeypatch.setattr(selector_module, "RunSetBuilder", CapturingBuilder)
    return observed


def _multi_batch_chain(h: RestHarness, *, count: int = 5) -> None:
    items = ss.agg_items(count)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_A)
    with c.normalizer(h, clock=StepClock(start=K_A), microbatch_rows=2) as normalizer:
        normalizer.normalize_unit(c.ARCHIVE_AGGS.table, archive)
        normalizer.normalize_unit(c.REST_AGGS.table, response)
    h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)


def test_bounded_proofs_spill_and_close_readers_with_revision_order(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _multi_batch_chain(h)
    observed = _observe_proof_builders(monkeypatch)
    selector = PitSelector(h.adapter, h.storage)
    with selector.iter_bounded(
        _spec(h, cutoff=K_E), "agg_trades", SYMBOL, START, END, params=_PARAMS
    ) as records:
        list(records)

    assert observed
    assert all(builder.finish_calls == 1 and builder.root is not None for builder in observed)
    assert all(builder.max_buffered_rows == _PARAMS.row_batch_rows for builder in observed)
    assert all(
        builder.max_pending_refs <= _PARAMS.merge_fanout * builder.max_ref_levels
        for builder in observed
    )
    assert any(builder.root.depth > 1 for builder in observed if builder.root is not None)
    assert any(
        builder.counting_storage.stage_objects > builder.add_count for builder in observed
    )  # run compactions add content-addressed objects beyond leaf flushes
    assert all(builder.counting_storage.stage_bytes > 0 for builder in observed)
    assert all(
        builder.counting_storage.open_readers > 0
        and builder.counting_storage.closed_readers == builder.counting_storage.open_readers
        for builder in observed
    )


def test_late_unit_failure_leaves_proof_builder_unfinished_and_unobservable(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _multi_batch_chain(h)
    observed = _observe_proof_builders(monkeypatch)
    original = CanonicalNormalizer._check_committed_window
    checks = 0

    def fail_second_unit(self: CanonicalNormalizer, *args: Any, **kwargs: Any) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("late requested unit failure")
        original(self, *args, **kwargs)

    monkeypatch.setattr(CanonicalNormalizer, "_check_committed_window", fail_second_unit)
    selector = PitSelector(h.adapter, h.storage)
    with pytest.raises(RuntimeError, match="late requested unit failure"):
        with selector.iter_bounded(
            _spec(h, cutoff=K_E), "agg_trades", SYMBOL, START, END, params=_PARAMS
        ) as records:
            pytest.fail(f"proof prefix escaped before full validation: {next(records, None)!r}")

    assert checks == 2
    assert observed
    failed = observed[-1]
    assert failed.add_count > 0
    assert failed.finish_calls == 0
    assert failed._closed
    assert not failed._rows
    assert all(not level for level in failed._refs._levels)
    assert failed.counting_storage.stage_objects > 0  # orphan growth is counted, never deleted
    assert failed.counting_storage.open_readers == failed.counting_storage.closed_readers
