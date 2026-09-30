"""Bounded normalizer proof staging used only by PIT v3."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pytest

from core.contracts.storage import StageRequest
from infrastructure.canonical import normalizer as normalizer_module
from infrastructure.canonical.normalizer import CanonicalNormalizer
from infrastructure.pit import selector as selector_module
from infrastructure.pit.runs import RunLimits, RunSetBuilder
from infrastructure.pit.selector import PitRunParams, PitSelector, _root_rows
from infrastructure.streaming import runs as streaming_runs
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


def _multi_batch_chain(h: RestHarness, *, count: int = 5) -> tuple[str, str]:
    items = ss.agg_items(count)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_A)
    with c.normalizer(h, clock=StepClock(start=K_A), microbatch_rows=2) as normalizer:
        normalizer.normalize_unit(c.ARCHIVE_AGGS.table, archive)
        normalizer.normalize_unit(c.REST_AGGS.table, response)
    h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, ss.DAY)
    return archive, response


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


def test_bounded_proofs_validate_batches_while_reverse_snapshot_stream_is_open(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _ = _multi_batch_chain(h, count=9)
    normalizer = c.normalizer(h, clock=StepClock(start=K_E), microbatch_rows=2)
    verified = normalizer.verify_unit(c.ARCHIVE_AGGS.table, archive)
    staged: list[tuple[int, int]] = []
    original_snapshots = CanonicalNormalizer._plan_snapshots_for_run
    original_check = CanonicalNormalizer._check_committed_window
    requested_streams: list[list[int]] = []
    active: list[bool] = []

    def observed_snapshots(
        self: CanonicalNormalizer, *args: Any, **kwargs: Any
    ) -> Iterator[tuple[int, Any]]:
        indices: list[int] = []
        requested_streams.append(indices)
        active.append(True)
        try:
            for index, snapshot in original_snapshots(self, *args, **kwargs):
                indices.append(index)
                yield index, snapshot
        finally:
            active[-1] = False

    def check_before_stream_is_exhausted(
        self: CanonicalNormalizer, *args: Any, **kwargs: Any
    ) -> None:
        assert any(active), "batch validation must consume snapshots incrementally"
        original_check(self, *args, **kwargs)

    monkeypatch.setattr(CanonicalNormalizer, "_plan_snapshots_for_run", observed_snapshots)
    monkeypatch.setattr(
        CanonicalNormalizer, "_check_committed_window", check_before_stream_is_exhausted
    )

    normalizer._stage_verified_unit(
        c.ARCHIVE_AGGS.table,
        archive,
        arrival_seqs=iter(sorted({row["arrival_seq"] for row in verified})),
        sink=lambda _row, batch_index, row_ordinal: staged.append((batch_index, row_ordinal)),
        request_capacity=_PARAMS.row_batch_rows,
        merge_fanout=_PARAMS.merge_fanout,
        run_limits=_PARAMS.limits,
    )

    assert requested_streams
    bounded_streams = [indices for indices in requested_streams if len(indices) > 1]
    assert bounded_streams
    assert all(indices == sorted(indices, reverse=True) for indices in bounded_streams)
    assert [batch_index for batch_index, _ in staged] == sorted(
        (batch_index for batch_index, _ in staged), reverse=True
    )
    assert not any(active)


def test_bounded_batch_request_run_write_failure_clears_writer_state(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _ = _multi_batch_chain(h, count=5)
    normalizer = c.normalizer(h, clock=StepClock(start=K_E), microbatch_rows=2)
    verified = normalizer.verify_unit(c.ARCHIVE_AGGS.table, archive)
    original_builder = normalizer_module.RunSetBuilder
    created: list[Any] = []

    class CapturingBuilder(original_builder):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    def fail_write(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError("simulated wanted-batch run failure")

    monkeypatch.setattr(normalizer_module, "RunSetBuilder", CapturingBuilder)
    monkeypatch.setattr(streaming_runs, "write_sorted_run", fail_write)
    with pytest.raises(OSError, match="simulated wanted-batch run failure"):
        normalizer._stage_verified_unit(
            c.ARCHIVE_AGGS.table,
            archive,
            arrival_seqs=iter((min(row["arrival_seq"] for row in verified),)),
            sink=lambda *_args: None,
            request_capacity=1,
            merge_fanout=_PARAMS.merge_fanout,
            run_limits=_PARAMS.limits,
        )

    [builder] = created
    assert builder._closed
    assert builder._rows == []
    assert builder._refs._levels == []


def test_duplicate_revision_ids_keep_legacy_ascending_batch_last_write_wins(
    h: RestHarness,
) -> None:
    # Bounded verification executes batches newest-first; the sort key must restore the legacy
    # unit / ascending-batch / row order before the selector applies dict last-write-wins.
    emitted = (
        (0, 2, 0, "newest batch"),
        (0, 1, 0, "middle batch"),
        (0, 0, 0, "oldest batch"),
        (1, 0, 0, "later unit"),
    )
    with RunSetBuilder(
        h.storage,
        key=selector_module._proof_row_sort_key,
        capacity=1,
        merge_fanout=_PARAMS.merge_fanout,
        limits=_PARAMS.limits,
    ) as builder:
        for unit_order, batch_index, row_ordinal, marker in emitted:
            builder.add(
                {
                    "revision_id": "duplicate-revision",
                    "unit_order": unit_order,
                    "batch_index": batch_index,
                    "planned_row_ordinal": row_ordinal,
                    "proof_row": {"marker": marker},
                }
            )
        root = builder.finish()

    with _root_rows(h.storage, root) as ordered:
        proof_rows = list(ordered)
    proofs: dict[str, Mapping[str, Any]] = {}
    for proof in proof_rows:
        proofs[proof["revision_id"]] = proof["proof_row"]

    assert proofs["duplicate-revision"]["marker"] == "later unit"


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
