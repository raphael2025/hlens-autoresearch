"""Research prototype: disk-backed validation of one PIT cutoff graph.

The validator is not dispatched by ``iter_bounded``. Its graph invariants pass focused checks, but
the current external Kahn pass rescans edge runs by depth; do not enable it on history-scale input
until the key-addressable adjacency/state index is implemented and measured.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from core.contracts.revision import PrecedenceEvidence, RevisionRecord
from core.contracts.storage import StorageAdapter
from infrastructure.pit.runs import RunLimits, RunRef, RunSetBuilder, iter_run


class PITGraphInvariantError(ValueError):
    """A bounded PIT graph violated an invariant enforced by ``RevisionGraph``."""


def validate_pit_graph_runs(
    storage: StorageAdapter,
    records: Iterable[RevisionRecord],
    evidence: Iterable[PrecedenceEvidence],
    *,
    cutoff: datetime,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> None:
    """Check the ``RevisionGraph`` invariants for rows known at ``cutoff`` using sorted runs.

    The input iterables are streamed once into bounded indexes. Run roots are folded online, so
    this validator does not allocate per-graph ID, payload, claim, edge, or degree maps. A cycle
    diagnostic retains only a fixed sample and count; the graph rejection remains fail closed.
    """
    with (
        RunSetBuilder(
            storage,
            key=lambda row: row["id"],
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as revision_ids,
        RunSetBuilder(
            storage,
            key=lambda row: row["arrival_seq"],
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as arrivals,
        RunSetBuilder(
            storage,
            key=lambda row: (row["observation_key"], row["source_id"], row["payload_hash"]),
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as payloads,
        RunSetBuilder(
            storage,
            key=lambda row: (row["revision_id"], row["observation_key"]),
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as claims,
        RunSetBuilder(
            storage,
            key=lambda row: (row["newer"], row["older"], row["knowledge_time"]),
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as required_evidence,
        RunSetBuilder(
            storage,
            key=lambda row: (row["newer"], row["older"], row["knowledge_time"]),
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as evidence_rows,
        RunSetBuilder(
            storage,
            key=lambda row: (row["newer"], row["older"]),
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
        ) as graph_edges,
    ):
        for record in records:
            if record.availability.times.knowledge_time > cutoff:
                continue
            revision_ids.add({"id": record.revision_id})
            arrivals.add({"arrival_seq": record.arrival_seq, "revision_id": record.revision_id})
            payloads.add(
                {
                    "observation_key": record.observation_key,
                    "source_id": record.source_id,
                    "payload_hash": record.payload_hash,
                    "revision_id": record.revision_id,
                }
            )
            claims.add(
                {"revision_id": record.revision_id, "observation_key": record.observation_key}
            )
            for older in record.supersedes:
                claims.add({"revision_id": older, "observation_key": record.observation_key})
                required_evidence.add(
                    {
                        "newer": record.revision_id,
                        "older": older,
                        "knowledge_time": record.availability.times.knowledge_time,
                    }
                )
                graph_edges.add({"newer": record.revision_id, "older": older})

        for item in evidence:
            if item.knowledge_time > cutoff:
                continue
            claims.add({"revision_id": item.revision_id, "observation_key": item.observation_key})
            claims.add(
                {
                    "revision_id": item.superseded_revision_id,
                    "observation_key": item.observation_key,
                }
            )
            evidence_rows.add(
                {
                    "newer": item.revision_id,
                    "older": item.superseded_revision_id,
                    "knowledge_time": item.knowledge_time,
                }
            )
            graph_edges.add({"newer": item.revision_id, "older": item.superseded_revision_id})

        revision_ids_root = revision_ids.finish()
        arrivals_root = arrivals.finish()
        payloads_root = payloads.finish()
        claims_root = claims.finish()
        required_root = required_evidence.finish()
        evidence_root = evidence_rows.finish()
        graph_edges_root = graph_edges.finish()

    _reject_adjacent_duplicates(
        storage,
        revision_ids_root,
        key=lambda row: row["id"],
        error=lambda row: PITGraphInvariantError(f"revision_id 重复：{row['id']!r}"),
    )
    _reject_adjacent_duplicates(
        storage,
        arrivals_root,
        key=lambda row: row["arrival_seq"],
        error=lambda row: PITGraphInvariantError(f"arrival_seq 重复：{row['arrival_seq']}"),
    )
    _reject_adjacent_duplicates(
        storage,
        payloads_root,
        key=lambda row: (row["observation_key"], row["source_id"], row["payload_hash"]),
        error=lambda row: PITGraphInvariantError(
            "重复 payload（同一 observation_key 下 source_id + payload_hash 相同）："
            f"{row['revision_id']!r} 不得成为新 revision"
        ),
    )
    _assert_single_key_claims(storage, claims_root)
    _assert_required_evidence(storage, required_root, evidence_root)
    _assert_acyclic(storage, graph_edges_root, capacity, merge_fanout, limits)


def _reject_adjacent_duplicates(
    storage: StorageAdapter,
    root: RunRef | None,
    *,
    key: Callable[[Mapping[str, Any]], Any],
    error: Callable[[Mapping[str, Any]], Exception],
) -> None:
    if root is None:
        return
    with iter_run(storage, root) as rows:
        previous: Mapping[str, Any] | None = None
        for row in rows:
            if previous is not None and key(previous) == key(row):
                raise error(row)
            previous = row


def _assert_single_key_claims(storage: StorageAdapter, root: RunRef | None) -> None:
    if root is None:
        return
    with iter_run(storage, root) as rows:
        for revision_id, group in itertools.groupby(rows, key=lambda row: row["revision_id"]):
            first_key: str | None = None
            for claim in group:
                observation_key = claim["observation_key"]
                if first_key is None:
                    first_key = observation_key
                elif observation_key != first_key:
                    left, right = sorted((first_key, observation_key))
                    raise PITGraphInvariantError(
                        f"revision_id {revision_id!r} 跨 observation_key 归属冲突：同时被 "
                        f"{[left, right]} 认领"
                    )


def _assert_required_evidence(
    storage: StorageAdapter, required_root: RunRef | None, evidence_root: RunRef | None
) -> None:
    if required_root is None:
        return
    with iter_run(storage, required_root) as required:
        if evidence_root is None:
            first = next(required, None)
            if first is not None:
                raise _missing_evidence(first)
            return
        with iter_run(storage, evidence_root) as evidence_rows:
            evidence_groups = iter(
                itertools.groupby(evidence_rows, key=lambda row: (row["newer"], row["older"]))
            )
            current = next(evidence_groups, None)
            for pair, requirements in itertools.groupby(
                required, key=lambda row: (row["newer"], row["older"])
            ):
                while current is not None and current[0] < pair:
                    current = next(evidence_groups, None)
                if current is None or current[0] != pair:
                    raise _missing_evidence({"newer": pair[0], "older": pair[1]})
                earliest_evidence = next(current[1], None)
                if earliest_evidence is None:
                    raise _missing_evidence({"newer": pair[0], "older": pair[1]})
                for requirement in requirements:
                    if earliest_evidence["knowledge_time"] > requirement["knowledge_time"]:
                        raise _missing_evidence(requirement)
                current = next(evidence_groups, None)


def _missing_evidence(requirement: Mapping[str, Any]) -> PITGraphInvariantError:
    return PITGraphInvariantError(
        f"supersedes 边 {requirement['newer']!r} → {requirement['older']!r} 缺少不晚于该 revision "
        "knowledge_time 的 precedence 证据"
    )


def _assert_acyclic(
    storage: StorageAdapter,
    root: RunRef | None,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> None:
    active = root
    while active is not None:
        with (
            RunSetBuilder(
                storage,
                key=lambda row: row["node"],
                capacity=capacity,
                merge_fanout=merge_fanout,
                limits=limits,
            ) as degree_rows,
            RunSetBuilder(
                storage,
                key=lambda row: row["node"],
                capacity=capacity,
                merge_fanout=merge_fanout,
                limits=limits,
            ) as ready_rows,
        ):
            with iter_run(storage, active) as edges:
                for edge in edges:
                    degree_rows.add({"node": edge["newer"], "increment": 0})
                    degree_rows.add({"node": edge["older"], "increment": 1})
            degree_root = degree_rows.finish()
            ready_count = 0
            if degree_root is not None:
                with iter_run(storage, degree_root) as nodes:
                    for node, group in itertools.groupby(nodes, key=lambda row: row["node"]):
                        if sum(item["increment"] for item in group) == 0:
                            ready_rows.add({"node": node})
                            ready_count += 1
            ready_root = ready_rows.finish()

        if ready_count == 0:
            node_count, sample = _cycle_nodes(storage, active, capacity, merge_fanout, limits)
            raise PITGraphInvariantError(
                f"supersedes 图不得成环：涉及 {sample}（共 {node_count} 个剩余节点）"
            )
        if ready_root is None:  # pragma: no cover - ready_count and root are coupled
            return
        with (
            iter_run(storage, active) as edges,
            iter_run(storage, ready_root) as ready,
            RunSetBuilder(
                storage,
                key=lambda row: (row["newer"], row["older"]),
                capacity=capacity,
                merge_fanout=merge_fanout,
                limits=limits,
            ) as remaining,
        ):
            next_ready = next(ready, None)
            previous_pair: tuple[str, str] | None = None
            for edge in edges:
                pair = (edge["newer"], edge["older"])
                if pair == previous_pair:
                    continue
                previous_pair = pair
                while next_ready is not None and next_ready["node"] < pair[0]:
                    next_ready = next(ready, None)
                if next_ready is None or next_ready["node"] != pair[0]:
                    remaining.add(edge)
            active = remaining.finish()


def _cycle_nodes(
    storage: StorageAdapter,
    root: RunRef,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
) -> tuple[int, tuple[str, ...]]:
    with RunSetBuilder(
        storage,
        key=lambda row: row["node"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    ) as nodes:
        with iter_run(storage, root) as edges:
            for edge in edges:
                nodes.add({"node": edge["newer"]})
                nodes.add({"node": edge["older"]})
        node_root = nodes.finish()
    sample: list[str] = []
    count = 0
    if node_root is not None:
        with iter_run(storage, node_root) as rows:
            previous: str | None = None
            for row in rows:
                node = row["node"]
                if node != previous:
                    count += 1
                    if len(sample) < 8:
                        sample.append(repr(node))
                    previous = node
    return count, tuple(sample)
