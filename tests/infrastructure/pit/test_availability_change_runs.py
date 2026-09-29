"""External PIT interval timelines preserve ordering while spilling bounded batches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus, RevisionRecord
from core.contracts.storage import StorageAdapter
from infrastructure.pit import selector as selector_module
from infrastructure.pit.runs import RunLimits, RunSetBuilder
from infrastructure.pit.selector import PitRunParams, PitSelector, _evaluate
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import END, FAR, START, _chain, _spec
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness


def _storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def test_external_timeline_spills_groups_equal_times_and_matches_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage(tmp_path)
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=30)
    revision_count = 257
    # Many revisions share availability times; input revision order intentionally differs from
    # availability order so the external run has to sort and coalesce the evaluation timeline.
    offsets = [((index * 13) % 23) + 1 for index in range(revision_count)]
    offsets[0] = 0  # exact start is excluded from changes (the start instant is evaluated once)
    offsets[1] = 30  # exact end is excluded by the half-open simulation interval
    records = cast(
        tuple[RevisionRecord, ...],
        tuple(
            SimpleNamespace(
                revision_id=f"r{index:04d}",
                availability=SimpleNamespace(times=SimpleNamespace(knowledge_time=cutoff)),
            )
            for index in range(revision_count)
        ),
    )
    available = {
        record.revision_id: start + timedelta(seconds=offsets[index])
        for index, record in enumerate(records)
    }
    spec = cast(
        PointInTimeSpec,
        SimpleNamespace(
            knowledge_cutoff=cutoff,
            simulation_time=None,
            simulation_start=start,
            simulation_end=end,
        ),
    )
    params = PitRunParams(
        row_batch_rows=3,
        edge_batch_rows=3,
        merge_fanout=2,
        key_history_buffer=1,
        limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
    )
    max_buffered_rows = 0
    builders: list[RunSetBuilder] = []
    original_builder = RunSetBuilder

    class ObservedRunSetBuilder(original_builder):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            builders.append(self)

        def add(self, row: Mapping[str, Any]) -> None:
            nonlocal max_buffered_rows
            super().add(row)
            max_buffered_rows = max(max_buffered_rows, len(self._rows))

    monkeypatch.setattr(selector_module, "RunSetBuilder", ObservedRunSetBuilder)

    external_instants: list[datetime] = []
    legacy_instants: list[datetime] = []

    def observed_head_fn(instants: list[datetime]) -> Any:
        def head_fn(
            _records: Sequence[Any],
            _edges: Sequence[Any],
            at: datetime,
            _cutoff: datetime,
            _available: Mapping[str, datetime],
        ) -> tuple[str, ...]:
            instants.append(at)
            if at == start:
                return ()
            if at < start + timedelta(seconds=8):
                return ("selected",)
            return ("left", "right")

        return head_fn

    external = list(
        _evaluate(
            "key",
            records,
            (),
            spec,
            available,
            run_storage=cast(StorageAdapter, storage),
            run_params=params,
            head_fn=observed_head_fn(external_instants),
        )
    )
    legacy = list(
        _evaluate("key", records, (), spec, available, head_fn=observed_head_fn(legacy_instants))
    )

    assert external == legacy
    assert [item.status for item in external] == [
        PointInTimeStatus.ABSENT,
        PointInTimeStatus.SELECTED,
        PointInTimeStatus.CONFLICT,
    ]
    expected_instants = [
        start,
        *sorted({start + timedelta(seconds=offset) for offset in offsets if 0 < offset < 30}),
    ]
    assert external_instants == legacy_instants == expected_instants
    assert len(builders) == 1
    assert max_buffered_rows <= params.row_batch_rows
    assert all(builder._finished and not builder._rows for builder in builders)

    empty_start = start + timedelta(seconds=40)
    empty_spec = cast(
        PointInTimeSpec,
        SimpleNamespace(
            knowledge_cutoff=cutoff,
            simulation_time=None,
            simulation_start=empty_start,
            simulation_end=empty_start + timedelta(seconds=10),
        ),
    )
    empty_external = list(
        _evaluate(
            "key",
            records,
            (),
            empty_spec,
            available,
            run_storage=cast(StorageAdapter, storage),
            run_params=params,
            head_fn=observed_head_fn([]),
        )
    )
    empty_legacy = list(
        _evaluate("key", records, (), empty_spec, available, head_fn=observed_head_fn([]))
    )
    assert empty_external == empty_legacy
    assert len(empty_external) == 1 and empty_external[0].simulation_time == empty_start
    assert len(builders) == 2 and builders[1]._finished and not builders[1]._rows


def test_bounded_interval_selected_conflict_matches_legacy(h: RestHarness) -> None:
    # With no reconciliation snapshot, the archive and REST images are independent heads once
    # both are available. The interval crosses both availability changes.
    _chain(h, reconcile=False)
    available_times = sorted({row["available_time"] for row in h.rows(c.TRADES)})
    assert len(available_times) == 2
    start = available_times[0] - timedelta(microseconds=1)
    end = available_times[1] + timedelta(microseconds=1)
    spec = _spec(h, cutoff=FAR, interval=(start, end))
    legacy = PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, START, END)
    with PitSelector(h.adapter, h.storage).iter_bounded(
        spec,
        "agg_trades",
        SYMBOL,
        START,
        END,
        params=PitRunParams(
            row_batch_rows=1,
            edge_batch_rows=1,
            merge_fanout=2,
            key_history_buffer=1,
            limits=RunLimits(leaf_max_records=1, leaf_max_bytes=1 << 16, fanout=2),
        ),
    ) as records:
        bounded = list(records)

    assert [item.selection for item in bounded] == list(legacy.selections)
    assert [item.selection.status for item in bounded] == [
        PointInTimeStatus.ABSENT,
        PointInTimeStatus.SELECTED,
        PointInTimeStatus.CONFLICT,
    ]
