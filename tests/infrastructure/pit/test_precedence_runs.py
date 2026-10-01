"""Disk-backed PIT maximal-head closure tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infrastructure.pit import precedence_runs
from infrastructure.pit import selector as selector_module
from infrastructure.pit.precedence_runs import maximal_heads_from_runs
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PitRunParams, PitSelector
from infrastructure.revision.precedence import maximal_heads
from tests.infrastructure.pit.test_selector import END, K_E, START, _chain, _spec
from tests.infrastructure.revision import rest_store_support as revision_support
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness


def test_maximal_heads_spills_and_walks_dangling_multi_hop_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with revision_support.sqlite_harness(tmp_path) as harness:
        capacity = 2
        limits = RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2)
        edges = [("new-head", "missing-00")]
        edges.extend((f"missing-{index:02d}", f"missing-{index + 1:02d}") for index in range(32))
        edges.extend(
            [
                ("missing-32", "old-head"),
                ("branch-head", "unavailable-record"),
                ("new-head", "missing-00"),  # duplicate edges have set semantics
            ]
        )
        maximum_pending = 0
        base_builder = precedence_runs.RunSetBuilder  # type: ignore[attr-defined]

        class ObservedRunSetBuilder(base_builder):  # type: ignore[misc, valid-type]
            def add(self, row: Any) -> None:
                nonlocal maximum_pending
                super().add(row)
                maximum_pending = max(maximum_pending, len(self._rows))

        monkeypatch.setattr(precedence_runs, "RunSetBuilder", ObservedRunSetBuilder)
        actual = maximal_heads_from_runs(
            harness.storage,
            ["old-head", "branch-head", "new-head", "branch-head"],
            edges,
            capacity=capacity,
            merge_fanout=2,
            limits=limits,
        )
        legacy = maximal_heads(
            [
                SimpleNamespace(  # type: ignore[misc]
                    revision_id=revision_id,
                    observation_key="key",
                    supersedes=(),
                )
                for revision_id in ("old-head", "branch-head", "new-head", "branch-head")
            ],
            [
                SimpleNamespace(  # type: ignore[misc]
                    observation_key="key",
                    revision_id=newer,
                    superseded_revision_id=older,
                )
                for newer, older in edges
            ],
        )

        assert actual == ("branch-head", "new-head")
        assert actual == legacy
        assert maximum_pending <= capacity


def test_iter_bounded_keeps_parity_without_unmeasured_external_traversal(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chain(h)
    spec = _spec(h, cutoff=K_E)
    selector = PitSelector(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory
    )
    expected = selector.select(spec, "agg_trades", SYMBOL, START, END)
    calls = 0
    original = selector_module.maximal_heads_from_runs  # type: ignore[attr-defined]

    def observed(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(selector_module, "maximal_heads_from_runs", observed)
    params = PitRunParams(
        row_batch_rows=1,
        edge_batch_rows=1,
        merge_fanout=2,
        key_history_buffer=1,
        limits=RunLimits(leaf_max_records=1, leaf_max_bytes=1 << 16, fanout=2),
    )
    with selector.iter_bounded(
        spec, "agg_trades", SYMBOL, START, END, params=params
    ) as actual_records:
        actual = [item.selection for item in actual_records]

    assert calls == 0
    assert [
        (
            item.observation_key,
            item.simulation_time,
            item.knowledge_cutoff,
            item.status,
            item.selected_revision_id,
            item.head_count,
        )
        for item in actual
    ] == [
        (
            item.observation_key,
            item.simulation_time,
            item.knowledge_cutoff,
            item.status,
            item.selected_revision_id,
            len(item.maximal_heads),
        )
        for item in expected.selections
    ]
