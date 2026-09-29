"""Bounded, content-addressed JSONL streams for ADR-0093 quality report evidence.

Records are canonical JSON objects, one per LF-terminated UTF-8 line.  A stream is an ordered
sequence; this layer enforces the byte/record/tree bounds and authenticates storage, while the
report rule owns record schemas and ordering.  Every resource bound is supplied explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from core.contracts.storage import StageRequest, StorageAdapter, StorageError
from core.domain.base import canonical_json

__all__ = [
    "QUALITY_REPORT_STREAM_FORMAT",
    "QualityReportStreamError",
    "QualityReportStreamIntegrityError",
    "QualityReportStreamLimits",
    "QualityReportStreamRef",
    "QualityReportStreamTooLarge",
    "QualityReportStreamWriter",
    "iter_quality_report_stream",
]

QUALITY_REPORT_STREAM_FORMAT: Final = "hlens.quality.report-jsonl@1.0.0"
_KEY_PREFIX: Final = "quality/report-evidence/v1/"
_KEY_RE: Final = re.compile(r"^quality/report-evidence/v1/([0-9a-f]{64})\.jsonl$")
_HEADER_MAX_BYTES: Final = 512
_REF_MAX_BYTES: Final = 512
_LEAF: Final = "leaf"
_INDEX: Final = "index"
_STREAMS: Final = frozenset({"events", "event_revisions", "evidence_gaps"})


class QualityReportStreamError(Exception):
    """Base error for a quality report evidence stream."""


class QualityReportStreamIntegrityError(QualityReportStreamError):
    """Stored bytes or tree structure differ from the committed stream reference."""


class QualityReportStreamTooLarge(QualityReportStreamError, ValueError):
    """One record cannot fit in the explicitly configured maximum leaf size."""


def _positive(name: str, value: object, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QualityReportStreamError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class QualityReportStreamLimits:
    leaf_max_records: int
    leaf_max_bytes: int
    fanout: int

    def __post_init__(self) -> None:
        _positive("leaf_max_records", self.leaf_max_records)
        _positive("leaf_max_bytes", self.leaf_max_bytes)
        _positive("fanout", self.fanout, 2)


@dataclass(frozen=True, slots=True)
class QualityReportStreamRef:
    stream: str
    format: str
    record_count: int
    leaf_count: int
    depth: int
    root_key: str
    root_sha256: str
    root_size: int


@dataclass(frozen=True, slots=True)
class _Child:
    key: str
    sha256: str
    size: int
    first: int
    count: int


def _key(sha256: str) -> str:
    return f"{_KEY_PREFIX}{sha256}.jsonl"


def _publish(storage: StorageAdapter, body: bytes) -> _Child:
    digest = hashlib.sha256(body).hexdigest()
    key = _key(digest)
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=digest, expected_size=len(body)), [body]
    )
    result = storage.publish(staged)
    return _Child(result.ref.key, result.ref.sha256, result.ref.size, 0, 0)


def _line(document: Mapping[str, Any]) -> bytes:
    try:
        return canonical_json(dict(document)).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise QualityReportStreamError(f"record is not canonical-JSON serializable: {exc}") from exc


class QualityReportStreamWriter:
    """Append fixed-size mapping records and publish an ordered bounded tree on ``finish``."""

    def __init__(
        self,
        storage: StorageAdapter,
        stream: str,
        *,
        limits: QualityReportStreamLimits,
    ) -> None:
        if not isinstance(stream, str) or stream not in _STREAMS:
            raise QualityReportStreamError(f"stream must be one of {sorted(_STREAMS)}")
        if not isinstance(limits, QualityReportStreamLimits):
            raise QualityReportStreamError("limits must be QualityReportStreamLimits")
        self._storage = storage
        self._stream = stream
        self._limits = limits
        self._leaf: list[bytes] = []
        self._leaf_bytes = 0
        self._leaf_first = 0
        self._count = 0
        self._leaf_count = 0
        self._levels: list[list[_Child]] = []
        self._finished = False

    def append(self, record: Mapping[str, Any]) -> None:
        if self._finished:
            raise QualityReportStreamError("writer is already finished")
        if not isinstance(record, Mapping) or any(not isinstance(key, str) for key in record):
            raise QualityReportStreamError("a stream record must be a string-keyed JSON object")
        line = _line(record)
        if len(line) > self._limits.leaf_max_bytes:
            raise QualityReportStreamTooLarge(
                f"record line of {len(line)} bytes exceeds leaf_max_bytes="
                f"{self._limits.leaf_max_bytes}"
            )
        if self._leaf and (
            len(self._leaf) >= self._limits.leaf_max_records
            or self._leaf_bytes + len(line) > self._limits.leaf_max_bytes
        ):
            self._flush_leaf()
        if not self._leaf:
            self._leaf_first = self._count
        self._leaf.append(line)
        self._leaf_bytes += len(line)
        self._count += 1

    def finish(self) -> QualityReportStreamRef:
        if self._finished:
            raise QualityReportStreamError("writer is already finished")
        if self._leaf:
            self._flush_leaf()
        root = self._finish_root()
        self._finished = True
        depth = 1
        capacity = self._limits.fanout
        while capacity < self._leaf_count:
            capacity *= self._limits.fanout
            depth += 1
        return QualityReportStreamRef(
            self._stream,
            QUALITY_REPORT_STREAM_FORMAT,
            self._count,
            self._leaf_count,
            depth,
            root.key,
            root.sha256,
            root.size,
        )

    def _flush_leaf(self) -> None:
        header = {
            "first_ordinal": self._leaf_first,
            "format": QUALITY_REPORT_STREAM_FORMAT,
            "node": _LEAF,
            "record_count": len(self._leaf),
            "stream": self._stream,
        }
        stored = _publish(self._storage, _line(header) + b"".join(self._leaf))
        child = _Child(stored.key, stored.sha256, stored.size, self._leaf_first, len(self._leaf))
        self._leaf = []
        self._leaf_bytes = 0
        self._leaf_count += 1
        self._push(0, child)

    def _push(self, index: int, child: _Child) -> None:
        while len(self._levels) <= index:
            self._levels.append([])
        bucket = self._levels[index]
        if len(bucket) == self._limits.fanout:
            node = self._emit(index + 1, bucket)
            self._levels[index] = [child]
            self._push(index + 1, node)
        else:
            bucket.append(child)

    def _emit(self, level: int, children: list[_Child]) -> _Child:
        first = children[0].first if children else 0
        count = sum(child.count for child in children)
        header = {
            "first_ordinal": first,
            "format": QUALITY_REPORT_STREAM_FORMAT,
            "level": level,
            "node": _INDEX,
            "record_count": count,
            "stream": self._stream,
        }
        lines = [_line(header)]
        for child in children:
            lines.append(
                _line(
                    {
                        "first_ordinal": child.first,
                        "key": child.key,
                        "record_count": child.count,
                        "sha256": child.sha256,
                        "size": child.size,
                    }
                )
            )
        stored = _publish(self._storage, b"".join(lines))
        return _Child(stored.key, stored.sha256, stored.size, first, count)

    def _finish_root(self) -> _Child:
        if self._count == 0:
            return self._emit(1, [])
        level = 0
        while True:
            above = any(self._levels[index] for index in range(level + 1, len(self._levels)))
            if not above:
                bucket = self._levels[level] if level < len(self._levels) else []
                return self._emit(level + 1, bucket)
            bucket = self._levels[level]
            self._levels[level] = []
            if bucket:
                self._push(level + 1, self._emit(level + 1, bucket))
            level += 1


def _read_object(storage: StorageAdapter, ref: _Child, maximum: int) -> list[bytes]:
    if ref.size > maximum:
        raise QualityReportStreamIntegrityError(f"object {ref.key} exceeds configured object bound")
    try:
        found = storage.lookup(ref.key)
        if found is None or (found.key, found.sha256, found.size) != (
            ref.key,
            ref.sha256,
            ref.size,
        ):
            raise QualityReportStreamIntegrityError(f"object {ref.key} is missing or misidentified")
        with storage.open_read(found) as handle:
            body = handle.read(ref.size + 1)
    except StorageError as exc:
        raise QualityReportStreamIntegrityError(f"object {ref.key} cannot be read: {exc}") from exc
    if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.sha256:
        raise QualityReportStreamIntegrityError(f"object {ref.key} bytes do not match its digest")
    if not body.endswith(b"\n"):
        raise QualityReportStreamIntegrityError(f"object {ref.key} does not end in LF")
    return body[:-1].split(b"\n")


def _canonical_object(
    line: bytes, maximum: int, expected: frozenset[str], what: str
) -> dict[str, Any]:
    if len(line) + 1 > maximum:
        raise QualityReportStreamIntegrityError(f"{what} exceeds its format bound")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise QualityReportStreamIntegrityError(f"{what} is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != expected:
        raise QualityReportStreamIntegrityError(f"{what} has an invalid field set")
    if canonical_json(value).encode("utf-8") != line:
        raise QualityReportStreamIntegrityError(f"{what} is not canonical JSON")
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QualityReportStreamIntegrityError(f"{name} must be an integer >= {minimum}")
    return value


@contextmanager
def iter_quality_report_stream(
    storage: StorageAdapter,
    ref: QualityReportStreamRef,
    *,
    limits: QualityReportStreamLimits,
) -> Iterator[Iterator[dict[str, Any]]]:
    """Yield the authenticated records in order; closing the context closes traversal promptly."""
    if ref.format != QUALITY_REPORT_STREAM_FORMAT:
        raise QualityReportStreamIntegrityError("stream uses an unsupported format")
    if ref.stream not in _STREAMS:
        raise QualityReportStreamIntegrityError("stream reference names an unknown stream")
    if not isinstance(limits, QualityReportStreamLimits):
        raise QualityReportStreamIntegrityError("limits must be QualityReportStreamLimits")
    match = _KEY_RE.fullmatch(ref.root_key)
    if match is None or match.group(1) != ref.root_sha256:
        raise QualityReportStreamIntegrityError("root key is not derived from its SHA-256")
    for name, value, minimum in (
        ("record_count", ref.record_count, 0),
        ("leaf_count", ref.leaf_count, 0),
        ("depth", ref.depth, 1),
        ("root_size", ref.root_size, 1),
    ):
        try:
            _positive(name, value, minimum)
        except QualityReportStreamError as exc:
            raise QualityReportStreamIntegrityError(str(exc)) from exc
    depth = 1
    capacity = limits.fanout
    while capacity < ref.leaf_count:
        capacity *= limits.fanout
        depth += 1
    if ref.depth != depth:
        raise QualityReportStreamIntegrityError("stream depth is not canonical for leaf count")

    def walk() -> Generator[dict[str, Any]]:
        next_ordinal = 0
        leaves = 0
        previous_leaf: tuple[int, int] | None = None
        root = _Child(ref.root_key, ref.root_sha256, ref.root_size, 0, ref.record_count)

        def index(
            node: _Child, level: int, rightmost: bool, is_root: bool
        ) -> Iterator[dict[str, Any]]:
            maximum = _HEADER_MAX_BYTES + limits.fanout * _REF_MAX_BYTES
            lines = _read_object(storage, node, maximum)
            header = _canonical_object(
                lines[0],
                _HEADER_MAX_BYTES,
                frozenset({"first_ordinal", "format", "level", "node", "record_count", "stream"}),
                "index header",
            )
            if (header["format"], header["stream"], header["node"]) != (
                QUALITY_REPORT_STREAM_FORMAT,
                ref.stream,
                _INDEX,
            ) or _integer(header["level"], "level", 1) != level:
                raise QualityReportStreamIntegrityError("index header identity or level mismatch")
            if (
                _integer(header["first_ordinal"], "first_ordinal"),
                _integer(header["record_count"], "record_count"),
            ) != (node.first, node.count):
                raise QualityReportStreamIntegrityError(
                    "index header differs from parent reference"
                )
            children: list[_Child] = []
            for raw in lines[1:]:
                item = _canonical_object(
                    raw,
                    _REF_MAX_BYTES,
                    frozenset({"first_ordinal", "key", "record_count", "sha256", "size"}),
                    "child ref",
                )
                key, digest = item["key"], item["sha256"]
                if (
                    not isinstance(key, str)
                    or not isinstance(digest, str)
                    or _KEY_RE.fullmatch(key) is None
                    or _key(digest) != key
                ):
                    raise QualityReportStreamIntegrityError("child key is not content addressed")
                children.append(
                    _Child(
                        key,
                        digest,
                        _integer(item["size"], "size", 1),
                        _integer(item["first_ordinal"], "first_ordinal"),
                        _integer(item["record_count"], "record_count", 1),
                    )
                )
            if len(children) > limits.fanout or (
                not children and not (is_root and level == 1 and node.count == 0)
            ):
                raise QualityReportStreamIntegrityError(
                    "index child count violates fanout/empty-root rule"
                )
            if not rightmost and len(children) != limits.fanout:
                raise QualityReportStreamIntegrityError("non-rightmost index node is not full")
            cursor = node.first
            for child in children:
                if child.first != cursor:
                    raise QualityReportStreamIntegrityError("child ranges are not contiguous")
                cursor += child.count
            if cursor != node.first + node.count:
                raise QualityReportStreamIntegrityError("child counts do not cover their parent")
            for position, child in enumerate(children):
                if level == 1:
                    yield from leaf(child)
                else:
                    yield from index(
                        child, level - 1, rightmost and position == len(children) - 1, False
                    )

        def leaf(node: _Child) -> Iterator[dict[str, Any]]:
            nonlocal next_ordinal, leaves, previous_leaf
            maximum = _HEADER_MAX_BYTES + limits.leaf_max_bytes
            lines = _read_object(storage, node, maximum)
            header = _canonical_object(
                lines[0],
                _HEADER_MAX_BYTES,
                frozenset({"first_ordinal", "format", "node", "record_count", "stream"}),
                "leaf header",
            )
            if (header["format"], header["stream"], header["node"]) != (
                QUALITY_REPORT_STREAM_FORMAT,
                ref.stream,
                _LEAF,
            ):
                raise QualityReportStreamIntegrityError("leaf header identity mismatch")
            if (
                _integer(header["first_ordinal"], "first_ordinal"),
                _integer(header["record_count"], "record_count"),
            ) != (node.first, node.count) or node.first != next_ordinal:
                raise QualityReportStreamIntegrityError(
                    "leaf range differs from its parent or order"
                )
            records = lines[1:]
            if (
                len(records) != node.count
                or not 1 <= len(records) <= limits.leaf_max_records
                or sum(len(item) + 1 for item in records) > limits.leaf_max_bytes
            ):
                raise QualityReportStreamIntegrityError(
                    "leaf record count/bytes exceed configured limits"
                )
            parsed: list[dict[str, Any]] = []
            for raw in records:
                if len(raw) + 1 > limits.leaf_max_bytes:
                    raise QualityReportStreamIntegrityError("record exceeds configured maximum")
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise QualityReportStreamIntegrityError("record is not UTF-8 JSON") from exc
                if not isinstance(record, dict) or canonical_json(record).encode("utf-8") != raw:
                    raise QualityReportStreamIntegrityError("record is not a canonical JSON object")
                parsed.append(record)
            used = sum(len(record) + 1 for record in records)
            if previous_leaf is not None:
                previous_count, previous_bytes = previous_leaf
                if (
                    previous_count < limits.leaf_max_records
                    and previous_bytes + len(records[0]) + 1 <= limits.leaf_max_bytes
                ):
                    raise QualityReportStreamIntegrityError("the previous leaf was closed early")
            previous_leaf = (len(records), used)
            next_ordinal += len(records)
            leaves += 1
            yield from parsed

        yield from index(root, ref.depth, True, True)
        if next_ordinal != ref.record_count or leaves != ref.leaf_count:
            raise QualityReportStreamIntegrityError("root counts disagree with traversed records")

    iterator = walk()
    try:
        yield iterator
    finally:
        iterator.close()
