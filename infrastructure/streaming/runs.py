"""Content-addressed sorted runs shared by bounded infrastructure streams.

Rows from arbitrary source order are spilled in caller-sized batches, sorted, and merged with
bounded fanout. Capacity and run-shape limits are always supplied by the caller (ADR-0077 DQ-9).
The run format is stable and remains compatible with persisted PIT runs.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from core.contracts.storage import StageRequest, StorageAdapter
from core.domain.base import canonical_json

__all__ = [
    "RUN_OBJECT_FORMAT",
    "RUN_OBJECT_KEY_PATTERN",
    "KeyHistoryBuffer",
    "RunIntegrityError",
    "RunLimits",
    "RunObjectRef",
    "RunRef",
    "RunSetBuilder",
    "RunWriteError",
    "iter_run",
    "merge_sorted_runs",
    "run_object_key",
    "spill_sorted_runs",
    "write_sorted_run",
]

#: Domain-separated format tag for every object this module writes (header line's ``"format"``).
RUN_OBJECT_FORMAT: Final = "hlens.pit.sorted-run@1.0.0"
_RUN_KEY_PREFIX: Final = "research/pit-sorted-run/v1/"
RUN_OBJECT_KEY_PATTERN: Final = r"^research/pit-sorted-run/v1/[0-9a-f]{64}\.jsonl$"
_SHA256_HEX_RE: Final = re.compile(r"^[0-9a-f]{64}$")
#: Conservative upper bound on one leaf/index header line's encoded size; ``leaf_max_bytes`` must
#: leave this much headroom for record/child-ref bytes so a leaf object never exceeds it.
_HEADER_RESERVE_BYTES: Final = 512
#: Sanity cap on the number of levels ``write_sorted_run`` will cascade-flush while finalizing a
#: run's index tree; a real run never approaches this (depth grows as log(N) of leaf_count).
_MAX_FINALIZE_STEPS: Final = 10_000


class RunWriteError(ValueError):
    """A sorted-run write request cannot be honored (bad parameters, an oversized record)."""


class RunIntegrityError(Exception):
    """A run object, once read back, does not reproduce what its reference commits to."""


@dataclass(frozen=True, slots=True)
class RunObjectRef:
    """The content identity of one leaf or index object: ``key`` is derived from ``sha256``."""

    key: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class RunRef:
    """The root reference to one sorted run: its object tree, row count and shape."""

    root: RunObjectRef
    record_count: int
    leaf_count: int
    depth: int


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Explicit, caller-chosen bounds for one run's object tree (ADR-0077 DQ-9: no defaults)."""

    leaf_max_records: int
    leaf_max_bytes: int
    fanout: int

    def __post_init__(self) -> None:
        if self.leaf_max_records <= 0:
            raise RunWriteError("leaf_max_records must be positive")
        if self.leaf_max_bytes <= _HEADER_RESERVE_BYTES:
            raise RunWriteError(f"leaf_max_bytes must be greater than {_HEADER_RESERVE_BYTES}")
        if self.fanout < 2:
            raise RunWriteError("fanout must be at least 2")


class _RunRefAccumulator:
    """Online fanout-bounded compaction of sorted run refs into one sorted root."""

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        key: Callable[[Mapping[str, Any]], Any],
        merge_fanout: int,
        limits: RunLimits,
    ) -> None:
        if isinstance(merge_fanout, bool) or not isinstance(merge_fanout, int) or merge_fanout < 2:
            raise RunWriteError("merge_fanout must be at least 2")
        self._storage = storage
        self._key = key
        self._merge_fanout = merge_fanout
        self._limits = limits
        self._levels: list[list[RunRef]] = []

    def _merge(self, group: Sequence[RunRef]) -> RunRef:
        if not 2 <= len(group) <= self._merge_fanout:
            raise RunWriteError("a run merge group must be within the configured fanout")
        with ExitStack() as stack:
            iterators = [stack.enter_context(iter_run(self._storage, ref)) for ref in group]
            return write_sorted_run(
                self._storage, heapq.merge(*iterators, key=self._key), self._limits
            )

    def add(self, ref: RunRef) -> None:
        self._add(ref, 0)

    def _add(self, ref: RunRef, level: int) -> None:
        while len(self._levels) <= level:
            self._levels.append([])
        bucket = self._levels[level]
        bucket.append(ref)
        if len(bucket) == self._merge_fanout:
            group = list(bucket)
            bucket.clear()
            self._add(self._merge(group), level + 1)

    def finish(self) -> RunRef | None:
        while True:
            nonempty = [i for i, bucket in enumerate(self._levels) if bucket]
            count = sum(len(self._levels[i]) for i in nonempty)
            if count == 0:
                return None
            if count == 1:
                root = self._levels[nonempty[0]][0]
                self._levels.clear()
                return root
            level = nonempty[0]
            group = list(self._levels[level])
            self._levels[level].clear()
            carried = group[0] if len(group) == 1 else self._merge(group)
            self._add(carried, level + 1)


class RunSetBuilder:
    """Build one globally sorted run using a fixed row buffer and hierarchical run refs.

    Completed capacity-sized batches are sorted and immediately folded into a fanout-bounded
    hierarchy. Intermediate objects are content-addressed and intentionally remain orphaned if a
    later operation fails; this helper never deletes published data (ADR-0077 §9).
    """

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        key: Callable[[Mapping[str, Any]], Any],
        capacity: int,
        merge_fanout: int,
        limits: RunLimits,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise RunWriteError("capacity must be a positive integer")
        self._storage = storage
        self._key = key
        self._capacity = capacity
        self._limits = limits
        self._refs = _RunRefAccumulator(storage, key=key, merge_fanout=merge_fanout, limits=limits)
        self._rows: list[Mapping[str, Any]] = []
        self._finished = False
        self._closed = False

    def __enter__(self) -> RunSetBuilder:
        if self._closed or self._finished:
            raise RunWriteError("a finished or closed run set cannot be reopened")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if exc_info[0] is not None or not self._finished:
            self.close()

    def close(self) -> None:
        """Release in-memory buffers; published run objects remain content-addressed orphans."""
        self._rows.clear()
        self._refs._levels.clear()
        self._closed = True

    def add(self, row: Mapping[str, Any]) -> None:
        if self._finished or self._closed:
            raise RunWriteError("cannot add to a finished or closed run set")
        self._rows.append(row)
        if len(self._rows) >= self._capacity:
            self._flush()

    def extend(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            self.add(row)

    def _flush(self) -> None:
        if not self._rows:
            return
        self._rows.sort(key=self._key)
        self._refs.add(write_sorted_run(self._storage, self._rows, self._limits))
        self._rows.clear()

    def finish(self) -> RunRef | None:
        if self._finished or self._closed:
            raise RunWriteError("run set finish may only be called once before close")
        self._flush()
        self._finished = True
        return self._refs.finish()


def run_object_key(sha256: str) -> str:
    """The canonical key of a run object: ``research/sorted-run/v1/<sha256>.jsonl``."""
    if not isinstance(sha256, str) or _SHA256_HEX_RE.fullmatch(sha256) is None:
        raise RunWriteError(
            f"a run object's sha256 must be 64 lowercase hex characters: {sha256!r}"
        )
    return f"{_RUN_KEY_PREFIX}{sha256}.jsonl"


# ==========================================================================================
# row codec: canonical row dicts (datetime / date / Decimal / str / int / float / bool / None,
# nested lists and mappings of the same) <-> JSON-safe, unambiguously round-trippable payloads.
# ==========================================================================================


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, datetime):
        return {"$dt": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, Mapping):
        return {"$obj": {str(k): _encode_value(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return [_encode_value(v) for v in value]
    raise RunWriteError(f"unsupported row value type for a sorted run: {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        keys = set(value)
        if keys == {"$dt"} and isinstance(value["$dt"], str):
            return datetime.fromisoformat(value["$dt"])
        if keys == {"$date"} and isinstance(value["$date"], str):
            return date.fromisoformat(value["$date"])
        if keys == {"$dec"} and isinstance(value["$dec"], str):
            return Decimal(value["$dec"])
        if keys == {"$obj"} and isinstance(value["$obj"], dict):
            return {k: _decode_value(v) for k, v in value["$obj"].items()}
        raise RunIntegrityError(f"unrecognized encoded row value shape: {sorted(value)!r}")
    if isinstance(value, list):
        return [_decode_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RunIntegrityError(f"unexpected decoded JSON value type: {type(value).__name__}")


def _encode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): _encode_value(v) for k, v in row.items()}


def _decode_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _decode_value(v) for k, v in payload.items()}


# ==========================================================================================
# write
# ==========================================================================================


def _stage_publish(storage: StorageAdapter, body: bytes) -> RunObjectRef:
    """Hash ``body`` first, then stage and publish it (DQ-11 = a: no default tempfile spool)."""
    sha256 = hashlib.sha256(body).hexdigest()
    key = run_object_key(sha256)
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=sha256, expected_size=len(body)), [body]
    )
    result = storage.publish(staged)
    return RunObjectRef(key=result.ref.key, sha256=result.ref.sha256, size=result.ref.size)


def _flush_index(
    storage: StorageAdapter, level: int, buf: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    first_ordinal = buf[0]["first_ordinal"]
    record_count = sum(item["record_count"] for item in buf)
    header = {
        "format": RUN_OBJECT_FORMAT,
        "node": "index",
        "level": level,
        "first_ordinal": first_ordinal,
        "record_count": record_count,
    }
    lines = [canonical_json(header).encode("utf-8") + b"\n"]
    for item in buf:
        ref: RunObjectRef = item["ref"]
        child = {
            "key": ref.key,
            "sha256": ref.sha256,
            "size": ref.size,
            "first_ordinal": item["first_ordinal"],
            "record_count": item["record_count"],
        }
        lines.append(canonical_json(child).encode("utf-8") + b"\n")
    ref_out = _stage_publish(storage, b"".join(lines))
    return {"ref": ref_out, "first_ordinal": first_ordinal, "record_count": record_count}


def write_sorted_run(
    storage: StorageAdapter, rows: Iterable[Mapping[str, Any]], limits: RunLimits
) -> RunRef:
    """Write ``rows`` (already in the run's intended order) as one sorted-run object tree.

    Does not sort: producing a sorted batch (or an already-sorted merge stream) is the caller's
    job — see :func:`spill_sorted_runs` and :func:`merge_sorted_runs`. A record whose encoded
    line alone would not fit in one leaf object fails closed (``RunWriteError``); it is never
    truncated or split across leaves.
    """
    max_body_bytes = limits.leaf_max_bytes - _HEADER_RESERVE_BYTES
    pending: dict[int, list[dict[str, Any]]] = {}

    def push(level: int, info: dict[str, Any]) -> None:
        buf = pending.setdefault(level, [])
        buf.append(info)
        if len(buf) == limits.fanout:
            flushed = _flush_index(storage, level, buf)
            pending[level] = []
            push(level + 1, flushed)

    leaf_lines: list[bytes] = []
    leaf_bytes = 0
    leaf_first_ordinal = 0
    ordinal = 0
    leaf_count = 0
    total_records = 0

    def flush_leaf() -> None:
        nonlocal leaf_lines, leaf_bytes, leaf_count
        if not leaf_lines:
            return
        record_count = len(leaf_lines)
        header = {
            "format": RUN_OBJECT_FORMAT,
            "node": "leaf",
            "first_ordinal": leaf_first_ordinal,
            "record_count": record_count,
        }
        body = canonical_json(header).encode("utf-8") + b"\n" + b"".join(leaf_lines)
        ref = _stage_publish(storage, body)
        push(0, {"ref": ref, "first_ordinal": leaf_first_ordinal, "record_count": record_count})
        leaf_count += 1
        leaf_lines = []
        leaf_bytes = 0

    for row in rows:
        line = canonical_json(_encode_row(row)).encode("utf-8") + b"\n"
        if len(line) > max_body_bytes:
            raise RunWriteError(
                f"one record's encoded line ({len(line)} bytes) exceeds what leaf_max_bytes="
                f"{limits.leaf_max_bytes} allows (header reserve {_HEADER_RESERVE_BYTES}); "
                "refusing to truncate or split a record"
            )
        if leaf_lines and (
            len(leaf_lines) >= limits.leaf_max_records or leaf_bytes + len(line) > max_body_bytes
        ):
            flush_leaf()
        if not leaf_lines:
            leaf_first_ordinal = ordinal
        leaf_lines.append(line)
        leaf_bytes += len(line)
        ordinal += 1
        total_records += 1
    flush_leaf()

    if total_records == 0:
        header = {
            "format": RUN_OBJECT_FORMAT,
            "node": "index",
            "level": 0,
            "first_ordinal": 0,
            "record_count": 0,
        }
        ref = _stage_publish(storage, canonical_json(header).encode("utf-8") + b"\n")
        return RunRef(root=ref, record_count=0, leaf_count=0, depth=1)

    # Cascade every remaining level bottom-up. A trailing partial buffer at one level and an
    # already fully-cascaded object several levels higher (from an earlier in-stream fanout
    # flush) are two *disconnected* subtrees at this point; checking only ``level + 1`` for
    # "anything above" would let one of them become the returned root while silently dropping
    # the other. Instead, after every flush, look at *all* levels ``pending`` still holds
    # something at: only when exactly one level has exactly one pending item left is there a
    # single connected tree, and that item is the root (its own header ``level`` is
    # ``only_level - 1``, i.e. ``depth == only_level``).
    level = 0
    for _ in range(_MAX_FINALIZE_STEPS):
        buf = pending.get(level, [])
        if buf:
            flushed = _flush_index(storage, level, buf)
            pending[level] = []
            pending.setdefault(level + 1, []).append(flushed)
        nonempty_levels = [candidate for candidate, items in pending.items() if items]
        if len(nonempty_levels) == 1:
            (only_level,) = nonempty_levels
            only_buf = pending[only_level]
            if len(only_buf) == 1:
                return RunRef(
                    root=only_buf[0]["ref"],
                    record_count=total_records,
                    leaf_count=leaf_count,
                    depth=only_level,
                )
        level += 1
    raise RunIntegrityError(  # pragma: no cover - defensive; depth grows as log(N)
        "run index construction did not converge within a bounded number of steps"
    )


def spill_sorted_runs(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: Callable[[Mapping[str, Any]], Any],
    capacity: int,
    storage: StorageAdapter,
    limits: RunLimits,
) -> Iterator[RunRef]:
    """Turn an arbitrary-order row stream into sorted runs of at most ``capacity`` rows each.

    This is the "canonical scan's arbitrary file order first goes through a fixed-capacity sorted
    run" step of ADR-0077 §6.1.2: at most ``capacity`` rows are ever held in memory at once.
    """
    if capacity <= 0:
        raise RunWriteError("capacity must be positive")
    batch: list[Mapping[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= capacity:
            batch.sort(key=key)
            yield write_sorted_run(storage, batch, limits)
            batch = []
    if batch:
        batch.sort(key=key)
        yield write_sorted_run(storage, batch, limits)


# ==========================================================================================
# read
# ==========================================================================================


def _read_object(storage: StorageAdapter, ref: RunObjectRef) -> tuple[dict[str, Any], list[str]]:
    looked_up = storage.lookup(ref.key)
    if looked_up is None:
        raise RunIntegrityError(f"run object {ref.key} is not published")
    if looked_up.sha256 != ref.sha256 or looked_up.size != ref.size:
        raise RunIntegrityError(f"run object {ref.key} does not match its reference")
    with storage.open_read(looked_up) as handle:
        body = handle.read()
    digest = hashlib.sha256(body).hexdigest()
    if digest != ref.sha256 or len(body) != ref.size:
        raise RunIntegrityError(f"run object {ref.key} content does not match its hash")
    text = body.decode("utf-8")
    if not text.endswith("\n"):
        raise RunIntegrityError(f"run object {ref.key} is not newline-terminated")
    lines = text.split("\n")[:-1]
    if not lines:
        raise RunIntegrityError(f"run object {ref.key} has no header line")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RunIntegrityError(f"run object {ref.key} has an unparsable header") from exc
    if not isinstance(header, dict) or header.get("format") != RUN_OBJECT_FORMAT:
        raise RunIntegrityError(f"run object {ref.key} has an unrecognized header")
    return header, lines[1:]


def _rows_of(
    storage: StorageAdapter,
    objref: RunObjectRef,
    *,
    expected_level: int | None,
    next_ordinal: list[int],
) -> Iterator[Mapping[str, Any]]:
    header, lines = _read_object(storage, objref)
    if header.get("first_ordinal") != next_ordinal[0]:
        raise RunIntegrityError(f"run object {objref.key} does not continue the stream in order")
    node = header.get("node")
    if node == "leaf":
        if expected_level is not None:
            raise RunIntegrityError(
                f"run object {objref.key} is a leaf where an index was expected"
            )
        record_count = header.get("record_count")
        if not isinstance(record_count, int) or record_count != len(lines):
            raise RunIntegrityError(f"run object {objref.key} record_count does not match its body")
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunIntegrityError(
                    f"run object {objref.key} has an unparsable record"
                ) from exc
            if not isinstance(payload, dict):
                raise RunIntegrityError(f"run object {objref.key} has a non-object record")
            yield _decode_row(payload)
            next_ordinal[0] += 1
    elif node == "index":
        level = header.get("level")
        if not isinstance(level, int) or level < 0:
            raise RunIntegrityError(f"run object {objref.key} has an invalid index level")
        if expected_level is not None and level != expected_level:
            raise RunIntegrityError(
                f"run object {objref.key} index level does not match its parent"
            )
        produced = 0
        for line in lines:
            try:
                child = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunIntegrityError(
                    f"run object {objref.key} has an unparsable child reference"
                ) from exc
            if not isinstance(child, dict):
                raise RunIntegrityError(f"run object {objref.key} has a non-object child reference")
            try:
                child_ref = RunObjectRef(
                    key=child["key"], sha256=child["sha256"], size=child["size"]
                )
                child_first = child["first_ordinal"]
                child_count = child["record_count"]
            except KeyError as exc:
                raise RunIntegrityError(
                    f"run object {objref.key} has a malformed child reference"
                ) from exc
            if child_first != next_ordinal[0]:
                raise RunIntegrityError(
                    f"run object {objref.key} child references are not contiguous"
                )
            before = next_ordinal[0]
            yield from _rows_of(
                storage,
                child_ref,
                expected_level=(level - 1) if level > 0 else None,
                next_ordinal=next_ordinal,
            )
            if next_ordinal[0] - before != child_count:
                raise RunIntegrityError(
                    f"run object {objref.key} child record_count does not match its body"
                )
            produced += child_count
        record_count = header.get("record_count")
        if not isinstance(record_count, int) or produced != record_count:
            raise RunIntegrityError(
                f"run object {objref.key} record_count does not match its children"
            )
    else:
        raise RunIntegrityError(f"run object {objref.key} has an unrecognized node type {node!r}")


@contextmanager
def iter_run(storage: StorageAdapter, ref: RunRef) -> Iterator[Iterator[Mapping[str, Any]]]:
    """Read a run back in order, verifying it against ``ref`` from the root down.

    Every leaf and index object is fetched, hash-checked and header-checked; ordinal continuity
    is enforced at every level, so truncation, reordering or an inserted/removed object is caught
    before (or, for a tail truncation, immediately after) it would otherwise go unnoticed.
    """

    def _generate() -> Iterator[Mapping[str, Any]]:
        next_ordinal = [0]
        expected_root_level = ref.depth - 1
        count = 0
        for row in _rows_of(
            storage, ref.root, expected_level=expected_root_level, next_ordinal=next_ordinal
        ):
            count += 1
            yield row
        if count != ref.record_count or next_ordinal[0] != ref.record_count:
            raise RunIntegrityError(
                f"run {ref.root.key} produced {count} record(s), expected {ref.record_count}"
            )

    yield _generate()


def merge_sorted_runs(
    storage: StorageAdapter,
    refs: Sequence[RunRef],
    *,
    key: Callable[[Mapping[str, Any]], Any],
    merge_fanout: int,
    limits: RunLimits,
) -> _MergeContext:
    """A bounded k-way merge of ``refs`` into one ordered stream (ADR-0077 §6.1.2 / §6.1.3).

    At most ``merge_fanout`` run readers are ever open at once. When ``len(refs) >
    merge_fanout``, groups of ``merge_fanout`` runs are first reduced, pass by pass, into
    intermediate persisted runs (the reduction's own leaf/index shape is governed by ``limits``)
    until at most ``merge_fanout`` remain; only then is the final streaming merge produced. The
    process never holds every input ``RunRef`` open at the same time, and the number of refs
    resident between passes is bounded by ``ceil(len(refs) / merge_fanout)`` at each level, not by
    the total row count.
    """
    if merge_fanout < 2:
        raise RunWriteError("merge_fanout must be at least 2")
    return _MergeContext(storage, list(refs), key=key, merge_fanout=merge_fanout, limits=limits)


class _MergeContext:
    """``ContextManager[Iterator[row]]`` returned by :func:`merge_sorted_runs`."""

    def __init__(
        self,
        storage: StorageAdapter,
        refs: list[RunRef],
        *,
        key: Callable[[Mapping[str, Any]], Any],
        merge_fanout: int,
        limits: RunLimits,
    ) -> None:
        self._storage = storage
        self._refs = refs
        self._key = key
        self._merge_fanout = merge_fanout
        self._limits = limits
        self._stack: ExitStack | None = None

    def __enter__(self) -> Iterator[Mapping[str, Any]]:
        storage, key, merge_fanout, limits = (
            self._storage,
            self._key,
            self._merge_fanout,
            self._limits,
        )
        current = self._refs
        while len(current) > merge_fanout:
            next_level: list[RunRef] = []
            for start in range(0, len(current), merge_fanout):
                group = current[start : start + merge_fanout]
                if len(group) == 1:
                    next_level.append(group[0])
                    continue
                with ExitStack() as pass_stack:
                    iterators = [pass_stack.enter_context(iter_run(storage, ref)) for ref in group]
                    merged = heapq.merge(*iterators, key=key)
                    next_level.append(write_sorted_run(storage, merged, limits))
            current = next_level
        stack = ExitStack()
        self._stack = stack
        iterators = [stack.enter_context(iter_run(storage, ref)) for ref in current]
        return heapq.merge(*iterators, key=key)

    def __exit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None


# ==========================================================================================
# one key's possibly-oversized history (ADR-0077 §6.1.2 item 2)
# ==========================================================================================


class KeyHistoryBuffer:
    """Bounded accumulator for one observation key's rows during the v3 evaluate loop.

    Rows must be added in the run's sort order (as they arrive from :func:`merge_sorted_runs`
    grouped by key); up to ``buffer_limit`` are held in memory, and any further rows are spilled
    into a sorted run of the same content-addressed format used everywhere else in this module,
    rather than growing an unbounded Python list for a pathologically deep chain. :meth:`rows`
    reconstructs the full ordered sequence, reading any spilled runs back through
    :func:`iter_run`.
    """

    def __init__(
        self,
        *,
        storage: StorageAdapter,
        buffer_limit: int,
        merge_fanout: int,
        key: Callable[[Mapping[str, Any]], Any],
        limits: RunLimits,
    ) -> None:
        if isinstance(buffer_limit, bool) or not isinstance(buffer_limit, int) or buffer_limit <= 0:
            raise RunWriteError("buffer_limit must be a positive integer")
        self._storage = storage
        self._buffer_limit = buffer_limit
        self._limits = limits
        self._refs = _RunRefAccumulator(storage, key=key, merge_fanout=merge_fanout, limits=limits)
        self._tail: list[Mapping[str, Any]] = []
        self._has_spilled = False
        self._finished = False
        self._root: RunRef | None = None

    def add(self, row: Mapping[str, Any]) -> None:
        if self._finished:
            raise RunWriteError("cannot add to a finalized key history")
        self._tail.append(row)
        if len(self._tail) >= self._buffer_limit:
            self._refs.add(write_sorted_run(self._storage, self._tail, self._limits))
            self._tail = []
            self._has_spilled = True

    @property
    def spilled(self) -> bool:
        """Whether any rows were spilled to a run (the key's history exceeded ``buffer_limit``)."""
        return self._has_spilled

    def _finish(self) -> RunRef | None:
        if self._finished:
            return self._root
        if self._has_spilled and self._tail:
            self._refs.add(write_sorted_run(self._storage, self._tail, self._limits))
            self._tail = []
        self._root = self._refs.finish()
        self._finished = True
        return self._root

    @contextmanager
    def rows(self) -> Iterator[Iterator[Mapping[str, Any]]]:
        root = self._finish()
        if root is None:
            yield iter(self._tail)
            return
        with iter_run(self._storage, root) as rows:
            yield rows
