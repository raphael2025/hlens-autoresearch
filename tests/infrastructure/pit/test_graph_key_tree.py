"""Key-addressed single-pass graph cycle validation prototype tests."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import RevisionGraph
from core.contracts.storage import StageRequest, StorageAdapter
from infrastructure.pit.graph_runs import PITGraphInvariantError, validate_pit_graph_runs
from infrastructure.pit.runs import RunLimits
from tests.infrastructure.revision import rest_store_support as storage_support
from tests.infrastructure.revision.test_precedence import KEY, T0, edge, record

_LIMITS = RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2)
_ADJACENCY_PREFIX = "research/content-key-tree/v1/"
_FRAME_PREFIX = "research/pit-graph-dfs/v1/"


class _CountingStorage:
    def __init__(self, storage: StorageAdapter) -> None:
        self._storage = storage
        self.reads: Counter[str] = Counter()
        self.stages: Counter[str] = Counter()
        self.opened = 0
        self.closed = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def stage(self, request: StageRequest, content: Any) -> Any:
        self.stages[_category(request.key)] += 1
        return self._storage.stage(request, content)

    def open_read(self, ref: Any) -> Any:
        category = _category(ref.key)
        self.reads[category] += 1
        self.opened += 1
        return _CountingReader(self._storage.open_read(ref), self)


class _CountingReader:
    def __init__(self, reader: Any, storage: _CountingStorage) -> None:
        self._reader = reader
        self._storage = storage
        self._closed = False

    def __enter__(self) -> _CountingReader:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        return self._reader.read(size)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._storage.closed += 1
            self._reader.close()


def _category(key: str) -> str:
    if key.startswith(_ADJACENCY_PREFIX):
        return "tree"
    if key.startswith(_FRAME_PREFIX):
        return "frame"
    return "other"


def _chain(size: int) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    revisions = tuple(
        record(
            f"tree-node-{index:04d}",
            supersedes=(f"tree-node-{index - 1:04d}",) if index else (),
            seq=index,
        )
        for index in range(size)
    )
    evidence = tuple(
        edge(item.revision_id, item.supersedes[0]) for item in revisions if item.supersedes
    )
    return revisions, evidence


def _validate(storage: StorageAdapter, revisions: Any, evidence: Any) -> None:
    validate_pit_graph_runs(
        storage,
        revisions,
        evidence,
        cutoff=T0,
        capacity=2,
        merge_fanout=2,
        limits=_LIMITS,
    )


def test_tree_cycle_validator_matches_revision_graph_on_dangling_branch_and_duplicate_edges(
    tmp_path: Path,
) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        revisions = (
            record("branch-a", supersedes=("branch-b", "branch-c"), seq=1),
            record("branch-b", supersedes=("branch-d",), seq=2),
            record("branch-c", supersedes=("branch-d",), seq=3),
        )
        evidence = tuple(
            edge(revision.revision_id, older)
            for revision in revisions
            for older in revision.supersedes
        )
        # The graph run sees each precedence edge twice: once from `supersedes` and once from
        # verified evidence. The adjacency builder must collapse identical directed edges.
        RevisionGraph(revisions=revisions, precedence_evidence=evidence)
        _validate(harness.storage, revisions, evidence)


def _count_chain_io(tmp_path: Path, size: int) -> tuple[Counter[str], Counter[str], int, int]:
    with storage_support.sqlite_harness(tmp_path) as harness:
        storage = _CountingStorage(harness.storage)
        revisions, evidence = _chain(size)
        _validate(storage, revisions, evidence)
        assert storage.opened == storage.closed
        return storage.reads, storage.stages, storage.opened, storage.closed


def test_tree_cycle_validator_walks_long_chain_with_linear_frame_io_and_subquadratic_tree_io(
    tmp_path: Path,
) -> None:
    small_reads, small_stages, _, _ = _count_chain_io(tmp_path / "small", 48)
    large_reads, large_stages, opened, closed = _count_chain_io(tmp_path / "large", 96)

    # A chain with N vertices and N-1 edges writes one linked frame per discovered vertex plus
    # one parent-cursor frame per unique edge; every written frame is read once before pop/advance.
    assert small_stages["frame"] == 2 * 48 - 1
    assert large_stages["frame"] == 2 * 96 - 1
    assert large_reads["frame"] == large_stages["frame"]
    assert small_reads["frame"] == small_stages["frame"]
    assert large_reads["tree"] > 0
    assert large_stages["tree"] > 0
    # Doubling V/E may add one B-tree level, but must not trigger a full-edge scan per depth.
    assert large_reads["tree"] <= small_reads["tree"] * 3
    assert large_stages["tree"] <= small_stages["tree"] * 3
    assert opened == closed


def test_tree_cycle_validator_rejects_cycle_after_spill(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        left = record("loop-left", supersedes=("loop-right",), seq=1)
        right = record("loop-right", supersedes=("loop-left",), seq=2)
        with pytest.raises(PITGraphInvariantError, match="supersedes 图不得成环"):
            _validate(
                harness.storage,
                (left, right),
                (
                    edge(left.revision_id, right.revision_id),
                    edge(right.revision_id, left.revision_id),
                ),
            )


def test_tree_cycle_validator_keeps_duplicate_and_evidence_rejections(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        first = record("duplicate-id", seq=1)
        second = record("duplicate-id", seq=2)
        with pytest.raises(PITGraphInvariantError, match="revision_id 重复"):
            _validate(harness.storage, (first, second), ())

        newer = record("without-edge-proof", supersedes=("unrecorded",), seq=3)
        with pytest.raises(PITGraphInvariantError, match="缺少不晚于该 revision"):
            _validate(harness.storage, (newer,), ())

        late = edge(newer.revision_id, "unrecorded").model_copy(
            update={"knowledge_time": T0 + timedelta(seconds=1)}
        )
        with pytest.raises(PITGraphInvariantError, match="缺少不晚于该 revision"):
            validate_pit_graph_runs(
                harness.storage,
                (newer,),
                (late,),
                cutoff=T0 + timedelta(seconds=1),
                capacity=1,
                merge_fanout=2,
                limits=_LIMITS,
            )


def test_tree_cycle_validator_keeps_cross_key_claim_rejection(tmp_path: Path) -> None:
    with storage_support.sqlite_harness(tmp_path) as harness:
        first = record("claimed-once", seq=1)
        other_key = record("other-key-claim", supersedes=(first.revision_id,), seq=2).model_copy(
            update={"observation_key": f"{KEY}:other"}
        )
        proof = edge(other_key.revision_id, first.revision_id).model_copy(
            update={"observation_key": f"{KEY}:other"}
        )
        with pytest.raises(PITGraphInvariantError, match="跨 observation_key"):
            _validate(harness.storage, (first, other_key), (proof,))
