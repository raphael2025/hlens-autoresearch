"""External-run maximal-head traversal for PIT's fixed-working-set path."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable

from core.contracts.storage import StorageAdapter
from infrastructure.pit.runs import RunLimits, RunRef, RunSetBuilder, iter_run


def maximal_heads_from_runs(
    storage: StorageAdapter,
    candidates: Iterable[str],
    edges: Iterable[tuple[str, str]],
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> tuple[str, ...]:
    """Return candidate heads with disk-backed multi-source reachability.

    ``edges`` are directed ``newer -> older`` and may traverse dangling or unavailable IDs. The
    frontier and expanded sets are separately external-sorted: reached nodes are retained even
    when they were already expanded, because every candidate in the reached set is superseded.
    Working memory is limited to the caller's run buffers and merge fanout. The returned tuple is
    the existing complete heads output contract and can itself grow with the number of heads.
    """
    root = _maximal_heads_root(
        storage, candidates, edges, capacity=capacity, merge_fanout=merge_fanout, limits=limits
    )
    return () if root is None else _read_ids(storage, root)


def maximal_head_summary_from_runs(
    storage: StorageAdapter,
    candidates: Iterable[str],
    edges: Iterable[tuple[str, str]],
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    emit: Callable[[int, int, str], None],
) -> tuple[int, str | None]:
    """Emit every ordered head and retain only its count and sole ID, if there is one.

    The content-addressed result run is traversed twice: once to count, then to emit records
    that bind that count. No head IDs are accumulated in Python memory.
    """
    root = _maximal_heads_root(
        storage, candidates, edges, capacity=capacity, merge_fanout=merge_fanout, limits=limits
    )
    if root is None:
        return 0, None
    count = 0
    sole: str | None = None
    with iter_run(storage, root) as rows:
        for row in rows:
            if count == 0:
                sole = row["id"]
            count += 1
    if count < 2:
        return count, sole
    first: str | None = None
    ordinal = 0
    with iter_run(storage, root) as rows:
        for row in rows:
            revision_id = row["id"]
            if ordinal == 0:
                first = revision_id
            emit(count, ordinal, revision_id)
            ordinal += 1
    if ordinal != count:
        raise RuntimeError("maximal-head run changed between count and emission passes")
    return count, first if count == 1 else None


def _maximal_heads_root(
    storage: StorageAdapter,
    candidates: Iterable[str],
    edges: Iterable[tuple[str, str]],
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    candidates_root = _build_ids(
        storage, candidates, capacity=capacity, merge_fanout=merge_fanout, limits=limits
    )
    if candidates_root is None:
        return None
    edges_root = _build_edges(
        storage, edges, capacity=capacity, merge_fanout=merge_fanout, limits=limits
    )
    if edges_root is None:
        return candidates_root

    expanded_root = candidates_root
    frontier_root = candidates_root
    reached_root: RunRef | None = None
    while frontier_root is not None:
        descendants_root = _expand_frontier(
            storage, frontier_root, edges_root, capacity=capacity,
            merge_fanout=merge_fanout, limits=limits,
        )
        if descendants_root is None:
            break
        reached_root = _merge_id_sets(
            storage, reached_root, descendants_root, capacity=capacity,
            merge_fanout=merge_fanout, limits=limits,
        )
        next_frontier = _subtract_id_sets(
            storage, descendants_root, expanded_root, capacity=capacity,
            merge_fanout=merge_fanout, limits=limits,
        )
        if next_frontier is None:
            break
        next_expanded_root = _merge_id_sets(
            storage, expanded_root, next_frontier, capacity=capacity,
            merge_fanout=merge_fanout, limits=limits,
        )
        if next_expanded_root is None:  # pragma: no cover - both inputs are nonempty roots
            raise RuntimeError("cannot merge non-empty expanded and frontier runs")
        expanded_root = next_expanded_root
        frontier_root = next_frontier
    return _subtract_id_sets(
        storage, candidates_root, reached_root, capacity=capacity,
        merge_fanout=merge_fanout, limits=limits,
    )


def _build_ids(
    storage: StorageAdapter,
    values: Iterable[str],
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    with RunSetBuilder(
        storage,
        key=lambda row: row["id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    ) as builder:
        for value in values:
            builder.add({"id": value})
        root = builder.finish()
    return _deduplicate_ids(
        storage, root, capacity=capacity, merge_fanout=merge_fanout, limits=limits
    )


def _build_edges(
    storage: StorageAdapter,
    values: Iterable[tuple[str, str]],
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    with RunSetBuilder(
        storage,
        key=lambda row: (row["newer"], row["older"]),
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    ) as builder:
        for newer, older in values:
            builder.add({"newer": newer, "older": older})
        return builder.finish()


def _deduplicate_ids(
    storage: StorageAdapter,
    root: RunRef | None,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    if root is None:
        return None
    with (
        iter_run(storage, root) as rows,
        RunSetBuilder(
            storage,
            key=lambda row: row["id"],
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as builder,
    ):
        previous: str | None = None
        for row in rows:
            value = row["id"]
            if value != previous:
                builder.add({"id": value})
                previous = value
        return builder.finish()


def _merge_id_sets(
    storage: StorageAdapter,
    left: RunRef | None,
    right: RunRef | None,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    if left is None:
        return right
    if right is None:
        return left
    with (
        iter_run(storage, left) as left_rows,
        iter_run(storage, right) as right_rows,
        RunSetBuilder(
            storage,
            key=lambda row: row["id"],
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as builder,
    ):
        previous: str | None = None
        for row in heapq.merge(left_rows, right_rows, key=lambda item: item["id"]):
            value = row["id"]
            if value != previous:
                builder.add({"id": value})
                previous = value
        return builder.finish()


def _subtract_id_sets(
    storage: StorageAdapter,
    left: RunRef | None,
    right: RunRef | None,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    if left is None:
        return None
    if right is None:
        return left
    with (
        iter_run(storage, left) as left_rows,
        iter_run(storage, right) as right_rows,
        RunSetBuilder(
            storage,
            key=lambda row: row["id"],
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as builder,
    ):
        next_right = next(right_rows, None)
        for row in left_rows:
            value = row["id"]
            while next_right is not None and next_right["id"] < value:
                next_right = next(right_rows, None)
            if next_right is None or next_right["id"] != value:
                builder.add({"id": value})
        return builder.finish()


def _expand_frontier(
    storage: StorageAdapter,
    frontier: RunRef,
    edges: RunRef,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> RunRef | None:
    """Streaming sort-merge join of frontier node IDs against edge source IDs."""
    with (
        iter_run(storage, frontier) as frontier_rows,
        iter_run(storage, edges) as edge_rows,
        RunSetBuilder(
            storage,
            key=lambda row: row["id"],
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as descendants,
    ):
        next_edge = next(edge_rows, None)
        for frontier_row in frontier_rows:
            newer = frontier_row["id"]
            while next_edge is not None and next_edge["newer"] < newer:
                next_edge = next(edge_rows, None)
            while next_edge is not None and next_edge["newer"] == newer:
                descendants.add({"id": next_edge["older"]})
                next_edge = next(edge_rows, None)
        root = descendants.finish()
    return _deduplicate_ids(
        storage, root, capacity=capacity, merge_fanout=merge_fanout, limits=limits
    )


def _read_ids(storage: StorageAdapter, root: RunRef) -> tuple[str, ...]:
    with iter_run(storage, root) as rows:
        return tuple(row["id"] for row in rows)
