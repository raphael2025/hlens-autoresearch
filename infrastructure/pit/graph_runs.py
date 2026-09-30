"""Research prototype: disk-backed validation of one PIT cutoff graph.

The validator is not dispatched by ``iter_bounded``. Record and evidence invariants use sorted
runs; cycle validation bulk-builds a key-addressable adjacency tree and uses external COW state
plus content-addressed linked DFS frames. This avoids depth-wide edge rescans, but tree page I/O,
COW orphan growth, adapter byte limits, and complete-process RSS still require measurement.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.contracts.revision import PrecedenceEvidence, RevisionRecord
from core.contracts.storage import ObjectRef, StageRequest, StorageAdapter
from core.domain.base import canonical_json
from infrastructure.pit.runs import RunLimits, RunRef, RunSetBuilder, iter_run
from infrastructure.streaming.content_key_tree import (
    ContentKeyTree,
    ContentKeyTreeError,
    KeyTreeParams,
    _fits_json_bytes,
    _reject_duplicate_json_keys,
)

_DFS_FRAME_FORMAT = "hlens.pit.graph-dfs-frame@1.0.0"
_DFS_FRAME_PREFIX = "research/pit-graph-dfs/v1/"


class PITGraphInvariantError(ValueError):
    """A bounded PIT graph violated an invariant enforced by ``RevisionGraph``."""


@dataclass(frozen=True, slots=True)
class _DFSFrame:
    node: str
    after: str | None
    parent: ObjectRef | None


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
    """Check the ``RevisionGraph`` invariants for rows known at ``cutoff`` using disk indexes.

    The input iterables are streamed into bounded sorted runs. Run roots are folded online, so
    this validator does not allocate per-graph ID, payload, claim, edge, or degree maps. Cycle
    checking uses key-range seeks and retains only the current DFS frame in Python memory; the
    existing complete conflict-head output is outside this validator.
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
    if root is None:
        return
    key_params = KeyTreeParams(
        page_max_bytes=limits.leaf_max_bytes,
        leaf_max_records=min(capacity, limits.leaf_max_records),
        fanout=limits.fanout,
    )

    def unique_adjacency_rows() -> Iterable[tuple[tuple[str, str], str]]:
        with iter_run(storage, root) as edges:
            for (newer, older), _duplicates in itertools.groupby(
                edges, key=lambda row: (row["newer"], row["older"])
            ):
                yield (newer, older), "1"

    adjacency = ContentKeyTree.build(storage, unique_adjacency_rows(), params=key_params)
    if adjacency.root_ref is None:
        return
    states = ContentKeyTree(storage, key_params)
    stack_top: ObjectRef | None = None

    # One adjacency scan seeds each source vertex. Vertices reachable only as older endpoints
    # are still visited by DFS, including dangling revisions that have no record of their own.
    edge_rows = adjacency.iter_from()
    try:
        for newer, _source_edges in itertools.groupby(edge_rows, key=lambda item: item[0][0]):
            if states.get((newer,)) is not None:
                continue
            states = states.put((newer,), "g")
            stack_top = _write_dfs_frame(
                storage,
                _DFSFrame(node=newer, after=None, parent=None),
                page_max_bytes=key_params.page_max_bytes,
            )

            while stack_top is not None:
                frame = _read_dfs_frame(
                    storage, stack_top, page_max_bytes=key_params.page_max_bytes
                )
                seek = (frame.node,) if frame.after is None else (frame.node, frame.after)
                following = adjacency.successor(seek, inclusive=frame.after is None)
                if following is None or following[0][0] != frame.node:
                    states = states.put((frame.node,), "b")
                    stack_top = frame.parent
                    continue

                older = following[0][1]
                # Checkpoint the parent cursor in one immutable stack frame before descent. The
                # former top is now an orphan; the new frame links to the same parent frame.
                advanced = _write_dfs_frame(
                    storage,
                    _DFSFrame(node=frame.node, after=older, parent=frame.parent),
                    page_max_bytes=key_params.page_max_bytes,
                )
                color = states.get((older,))
                if color == "g":
                    raise PITGraphInvariantError(
                        f"supersedes 图不得成环：回边 {frame.node!r} → {older!r}"
                    )
                if color == "b":
                    stack_top = advanced
                    continue
                if color is not None:
                    raise PITGraphInvariantError("图校验状态页包含未知节点颜色")
                states = states.put((older,), "g")
                stack_top = _write_dfs_frame(
                    storage,
                    _DFSFrame(node=older, after=None, parent=advanced),
                    page_max_bytes=key_params.page_max_bytes,
                )
    finally:
        edge_rows.close()


def _write_dfs_frame(
    storage: StorageAdapter, frame: _DFSFrame, *, page_max_bytes: int
) -> ObjectRef:
    parent = (
        None
        if frame.parent is None
        else {
            "key": frame.parent.key,
            "sha256": frame.parent.sha256,
            "size": frame.parent.size,
        }
    )
    page = {
        "format": _DFS_FRAME_FORMAT,
        "node": frame.node,
        "after": frame.after,
        "parent": parent,
    }
    if not _fits_json_bytes(page, page_max_bytes):
        raise ContentKeyTreeError("DFS frame exceeds page_max_bytes")
    body = canonical_json(page).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    key = f"{_DFS_FRAME_PREFIX}{digest}.json"
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=digest, expected_size=len(body)), [body]
    )
    return storage.publish(staged).ref


def _read_dfs_frame(storage: StorageAdapter, ref: ObjectRef, *, page_max_bytes: int) -> _DFSFrame:
    looked_up = storage.lookup(ref.key)
    if looked_up != ref:
        raise PITGraphInvariantError("DFS frame is missing or differs from its reference")
    with storage.open_read(looked_up) as handle:
        body = handle.read(page_max_bytes + 1)
    if (
        len(body) > page_max_bytes
        or len(body) != ref.size
        or hashlib.sha256(body).hexdigest() != ref.sha256
    ):
        raise PITGraphInvariantError("DFS frame fails its byte, hash, or size check")
    try:
        page = json.loads(body, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ContentKeyTreeError) as exc:
        raise PITGraphInvariantError("DFS frame is not valid JSON") from exc
    if not isinstance(page, dict) or set(page) != {"format", "node", "after", "parent"}:
        raise PITGraphInvariantError("DFS frame fields are not exact")
    node = page["node"]
    after = page["after"]
    parent_data = page["parent"]
    if (
        page["format"] != _DFS_FRAME_FORMAT
        or not isinstance(node, str)
        or not node
        or (after is not None and (not isinstance(after, str) or not after))
    ):
        raise PITGraphInvariantError("DFS frame field values are invalid")
    parent: ObjectRef | None
    if parent_data is None:
        parent = None
    elif isinstance(parent_data, dict) and set(parent_data) == {"key", "sha256", "size"}:
        key, sha256, size = parent_data["key"], parent_data["sha256"], parent_data["size"]
        if (
            not isinstance(key, str)
            or not isinstance(sha256, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise PITGraphInvariantError("DFS frame parent reference is invalid")
        parent_ref = storage.lookup(key)
        if parent_ref is None or (parent_ref.sha256, parent_ref.size) != (sha256, size):
            raise PITGraphInvariantError(
                "DFS frame parent object is missing or differs from its ref"
            )
        parent = parent_ref
    else:
        raise PITGraphInvariantError("DFS frame parent fields are invalid")
    return _DFSFrame(node=node, after=after, parent=parent)
