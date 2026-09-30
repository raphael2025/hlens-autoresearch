"""Production bounded-selector reuse and SQLite scratch lifecycle checks."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PitRunParams, PitSelector
from infrastructure.pit.sqlite_graph import SQLitePitGraph
from tests.infrastructure.pit.test_selector import END, START, _chain, _spec
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, utc

_PARAMS = PitRunParams(
    row_batch_rows=2,
    edge_batch_rows=2,
    merge_fanout=2,
    key_history_buffer=2,
    limits=RunLimits(leaf_max_records=2, leaf_max_bytes=1 << 16, fanout=2),
)
_INTERVAL = (utc(2023, 11, 15), utc(2023, 12, 31))


class _CountedAvailability(Mapping[str, datetime]):
    def __init__(self, inner: Mapping[str, datetime]) -> None:
        self._inner = inner
        self.reads: dict[str, int] = {}

    def __getitem__(self, revision_id: str) -> datetime:
        self.reads[revision_id] = self.reads.get(revision_id, 0) + 1
        return self._inner[revision_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._inner)

    def __len__(self) -> int:
        return len(self._inner)


def _selector(h: RestHarness) -> PitSelector:
    return PitSelector(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
    )


def _spec_for(h: RestHarness) -> PointInTimeSpec:
    return _spec(h, cutoff=utc(2030, 1, 1), interval=_INTERVAL)


def _active_graph_dirs(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.glob("pit-graph-*")))


def test_bounded_selector_builds_one_graph_and_matches_select_interval(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chain(h, reconcile=False)
    selector = _selector(h)
    spec = _spec_for(h)
    expected = selector.select(spec, "agg_trades", SYMBOL, START, END)

    calls = 0
    lookup_counts: list[dict[str, int]] = []
    original = SQLitePitGraph.build

    def observed_build(
        graph: SQLitePitGraph,
        records: Iterable[Any],
        edges: Iterable[Any],
        available: Mapping[str, datetime],
    ) -> None:
        nonlocal calls
        calls += 1
        counted = _CountedAvailability(available)
        original(graph, records, edges, counted)
        lookup_counts.append(counted.reads)

    monkeypatch.setattr(SQLitePitGraph, "build", observed_build)
    with selector.iter_bounded(spec, "agg_trades", SYMBOL, START, END, params=_PARAMS) as results:
        actual = [row.selection for row in results]

    assert calls == 1  # one Canonical observation key, built once across all instants
    assert len(lookup_counts) == 1
    assert lookup_counts[0] and set(lookup_counts[0].values()) == {1}
    assert [
        (
            row.observation_key,
            row.simulation_time,
            row.knowledge_cutoff,
            row.status,
            row.selected_revision_id,
            row.head_count,
        )
        for row in actual
    ] == [
        (
            row.observation_key,
            row.simulation_time,
            row.knowledge_cutoff,
            row.status,
            row.selected_revision_id,
            len(row.maximal_heads),
        )
        for row in expected.selections
    ]
    assert _active_graph_dirs(h.canonical_scratch_directory) == ()


def test_bounded_selector_early_close_removes_invocation_graph(h: RestHarness) -> None:
    _chain(h)
    selector = _selector(h)
    with selector.iter_bounded(
        _spec_for(h), "agg_trades", SYMBOL, START, END, params=_PARAMS
    ) as results:
        next(results)
        owned = _active_graph_dirs(h.canonical_scratch_directory)
        assert len(owned) == 1
        assert (owned[0] / ".hlens-pit-graph-owner").is_file()
    assert _active_graph_dirs(h.canonical_scratch_directory) == ()


def test_bounded_selector_conflict_sink_error_removes_invocation_graph(h: RestHarness) -> None:
    _chain(h, reconcile=False)
    selector = _selector(h)

    def fail_sink(_record: Any) -> None:
        raise RuntimeError("conflict-sink-failure")

    with pytest.raises(RuntimeError, match="conflict-sink-failure"):
        with selector.iter_bounded(
            _spec_for(h),
            "agg_trades",
            SYMBOL,
            START,
            END,
            params=_PARAMS,
            conflict_sink=fail_sink,
        ) as results:
            list(results)
    assert _active_graph_dirs(h.canonical_scratch_directory) == ()
