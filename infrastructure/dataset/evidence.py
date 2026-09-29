"""Content-addressed ordered evidence streams of a v3 Research Dataset (ADR-0077 §2 / §3 / §5).

A ``ResearchDatasetEvidenceManifest`` does not inline its members, exclusions, lineage, evidence
gaps, quality reports or chunk proofs: each of the six streams is an ordered sequence of records
committed by the root of a Merkle-style tree of immutable objects, and the root's
``EvidenceObjectRef`` enters the manifest's content hash. This module is the one writer and the
one reader of that format (``hlens.dataset.evidence-jsonl@1.0.0``).

**Record projection (§6.2.3, DQ-7 = a; ``EVIDENCE_PROJECTION``).** One record is one line::

    canonical_json(model_dump(mode="json") without any "schema_version" key equal to the
    record's own top-level version, at any depth; a nested "schema_version" pinning a
    different published contract version is kept) + "\\n"

UTF-8, no BOM, LF only (``canonical_json`` escapes control characters, so a record never contains
a raw LF). The field set is the record model's full field set (``None`` fields included), keys
sorted by ``canonical_json``; datetimes, dates, enums and decimals are rendered by pydantic's JSON
mode exactly as the model's content hash sees them. The envelope is **not** in the bytes: a reader
rebuilds every record inside ``contract_schema_version_scope(<the manifest's recorded version>)``
(ADR-0052 V1 / V7: one write group, one version), so evidence bytes are identical across contract
minors and a partial replay (§6.2.2) re-derives the same objects. The writer proves each
projection lossless (parse back in scope, same Python dump) and the reader proves each line is the
unique projection of the record it parses to (re-project, byte-equal), so the root hash is a
function of the record sequence alone.

**Nested envelopes of another version (ADR-0088 PM decision, ADR-0051 second phase; projection
text corrected 2026-09-28, F-C).** A nested contract that keeps **another** published envelope (a
registered identity pinned at its publication version, e.g. the 2.0.0 ``PolicyBinding`` of
``UniverseMember.assumption``) keeps its ``schema_version`` key in the bytes; the top-level
envelope and every nested one equal to it are still removed. So the line of such a record is still
the unique projection of one record (a nested key equal to the record's own version, an
unpublished version, or a top-level key all fail the reader's re-projection check), and it
rebuilds bit-identically at the manifest's recorded version. Before the W-DL2 fix (phase 2,
``bbcfd3a``) such a record was refused by the writer's lossless proof, so no previously stored line
changed. ``EVIDENCE_PROJECTION`` itself still read "every schema_version removed, at any depth"
after that fix — inexact for the records it newly admits; this correction fixes the text and bumps
``DATASET_EVIDENCE_RULE_VERSION`` with it (no real v3 manifest was ever persisted under the old
text, so the change has no replay cost).

**Objects (§3).** A *leaf* holds a contiguous run of records under a header line
``{"first_ordinal", "format", "node": "leaf", "record_count", "stream"}``; an *index* holds at most
``fanout`` child references ``{"first_ordinal", "key", "record_count", "sha256", "size"}`` under
``{"first_ordinal", "format", "level", "node": "index", "record_count", "stream"}``. Level 1 index
nodes point at leaves; level ``n`` at level ``n - 1``. The root is always an index (an empty
stream is a level 1 root without children). The shape is canonical:

- a leaf is closed when it holds ``leaf_max_records`` records, or when the next record line would
  make the sum of its record line bytes exceed ``leaf_max_bytes`` (header excluded); a single
  record line longer than ``leaf_max_bytes`` fails closed (``EvidenceRecordTooLarge``);
- index nodes are filled left to right: every node off the rightmost path has exactly ``fanout``
  children, so ``depth = canonical_depth(leaf_count, fanout)``.

Object identity is the SHA-256 of its full bytes; its key is ``dataset_evidence_key(sha256)``
(DQ-6 = a). Each object is assembled in memory within those bounds, hashed, then staged with
``expected_sha256`` and published (DQ-11 = a): no stream is spooled, no temporary file is used,
``StorageAdapter`` is unchanged. Republishing identical content is ``already_present``.

**Reading (§5, §6.2.4).** ``iter_evidence`` walks the tree depth first. Every reference is resolved
by ``StorageAdapter.lookup(key)`` whose key / sha256 / size must equal the parent's, and only then
``open_read``; every header is checked against its parent reference, children's ordinals must be
contiguous, levels must descend by one, and the shape must be the canonical one. A whole object is
validated before any of its records is yielded. Working set: one object plus ``depth`` open child
lists of at most ``fanout`` references (writer: one leaf buffer plus the same index stack).

Limits are the four DQ-9 parameters minus ``chunk_rows``; they have **no defaults** here (DQ-9 is
OPEN) and come from the dataset rule spec. ``EVIDENCE_HEADER_MAX_BYTES`` and
``EVIDENCE_REF_LINE_MAX_BYTES`` are format constants of ``@1.0.0`` (a header / reference line is a
handful of integers and fixed-width hashes), not resource parameters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from pydantic import ValidationError

from core.contracts.storage import StageRequest, StorageAdapter, StorageError
from core.contracts.universe import (
    DATASET_EVIDENCE_FORMAT,
    AvailabilityEvidenceGap,
    DatasetChunkProof,
    DatasetQualityReportRef,
    EvidenceObjectRef,
    EvidenceStream,
    EvidenceStreamRef,
    ResearchDatasetEvidenceManifest,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
    dataset_evidence_key,
)
from core.domain.base import (
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    Contract,
    canonical_json,
    contract_schema_version_scope,
)
from infrastructure import contract_version
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError

__all__ = [
    "EVIDENCE_HEADER_MAX_BYTES",
    "EVIDENCE_PROJECTION",
    "EVIDENCE_RECORD_TYPES",
    "EVIDENCE_REF_LINE_MAX_BYTES",
    "EvidenceError",
    "EvidenceIntegrityError",
    "EvidenceLimitError",
    "EvidenceRecordTooLarge",
    "EvidenceTreeLimits",
    "EvidenceTreeWriter",
    "canonical_depth",
    "evidence_record_bytes",
    "evidence_record_from_bytes",
    "iter_evidence",
    "iter_evidence_stream",
    "publish_evidence_object",
]

#: The unique serialization projection of a record line (ADR-0077 §6.2.3); part of the rule spec.
#: Corrected (PM decision, 2026-09-28, F-C) to describe the nested-envelope extension precisely
#: (module docstring): a nested 'schema_version' pinning a *different* published contract version
#: is kept, not stripped. The text enters every v3 rule hash (``DATASET_EVIDENCE_RULE_VERSION``
#: bumped with it); no real v3 manifest was ever persisted under the old text, so no replay cost.
EVIDENCE_PROJECTION: Final = (
    "canonical_json(model_dump(mode='json') without any 'schema_version' key equal to the "
    "record's own top-level version, at any depth; a nested 'schema_version' pinning a different "
    "published contract version is kept) + LF; UTF-8, no BOM; full field set, None included; "
    "rebuilt inside contract_schema_version_scope(<manifest schema_version>)"
)
#: Format constants of ``hlens.dataset.evidence-jsonl@1.0.0`` (not DQ-9 resource parameters).
EVIDENCE_HEADER_MAX_BYTES: Final = 512
EVIDENCE_REF_LINE_MAX_BYTES: Final = 512

#: The record model of each stream (ADR-0077 §1.6).
EVIDENCE_RECORD_TYPES: Final[Mapping[EvidenceStream, type[Contract]]] = MappingProxyType(
    {
        EvidenceStream.MEMBERS: UniverseMember,
        EvidenceStream.EXCLUSIONS: UniverseExclusion,
        EvidenceStream.LINEAGE: SelectedRevisionLineage,
        EvidenceStream.EVIDENCE_GAPS: AvailabilityEvidenceGap,
        EvidenceStream.QUALITY_REPORTS: DatasetQualityReportRef,
        EvidenceStream.CHUNK_PROOFS: DatasetChunkProof,
    }
)

_LEAF: Final = "leaf"
_INDEX: Final = "index"
_LEAF_HEADER_KEYS: Final = frozenset({"first_ordinal", "format", "node", "record_count", "stream"})
_INDEX_HEADER_KEYS: Final = _LEAF_HEADER_KEYS | {"level"}
_CHILD_KEYS: Final = frozenset({"first_ordinal", "key", "record_count", "sha256", "size"})


class EvidenceError(Exception):
    """Base class of evidence stream failures (every one is fail closed)."""


class EvidenceLimitError(EvidenceError, ValueError):
    """The tree limits are not usable (non-integers, zero, ``fanout < 2``)."""


class EvidenceRecordTooLarge(EvidenceError):
    """One record line exceeds ``leaf_max_bytes``: never truncated, the build fails (§9)."""


class EvidenceIntegrityError(EvidenceError, CatalogIntegrityError):
    """Evidence bytes, structure or identities disagree with what commits them."""


# =========================================================================================
# limits
# =========================================================================================


def _positive(name: str, value: object, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceLimitError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceTreeLimits:
    """The tree parameters of a dataset rule (DQ-9: explicit, no defaults, values OPEN).

    - ``leaf_max_records``: records per leaf (>= 1);
    - ``leaf_max_bytes``: sum of a leaf's record line bytes, header excluded (>= 1);
    - ``fanout``: children per index node (>= 2).
    """

    leaf_max_records: int
    leaf_max_bytes: int
    fanout: int

    def __post_init__(self) -> None:
        _positive("leaf_max_records", self.leaf_max_records)
        _positive("leaf_max_bytes", self.leaf_max_bytes)
        _positive("fanout", self.fanout, minimum=2)

    @property
    def leaf_object_max_bytes(self) -> int:
        return EVIDENCE_HEADER_MAX_BYTES + self.leaf_max_bytes

    @property
    def index_object_max_bytes(self) -> int:
        return EVIDENCE_HEADER_MAX_BYTES + self.fanout * EVIDENCE_REF_LINE_MAX_BYTES


def canonical_depth(leaf_count: int, fanout: int) -> int:
    """The root level of the canonical tree over ``leaf_count`` leaves (>= 1)."""
    _positive("fanout", fanout, minimum=2)
    if isinstance(leaf_count, bool) or not isinstance(leaf_count, int) or leaf_count < 0:
        raise EvidenceLimitError(f"leaf_count must be an integer >= 0, got {leaf_count!r}")
    depth, width = 1, fanout
    while width < leaf_count:
        width *= fanout
        depth += 1
    return depth


# =========================================================================================
# record projection (§6.2.3)
# =========================================================================================


def _strip_envelope(value: Any, version: str) -> Any:
    """``value`` without every nested ``schema_version`` equal to ``version`` (the record's own).

    A nested envelope of another version is kept, and must be a published contract version.
    """
    if isinstance(value, dict):
        stripped: dict[str, Any] = {}
        for key, item in value.items():
            if key != "schema_version":
                stripped[key] = _strip_envelope(item, version)
            elif item != version:
                if not isinstance(item, str) or item not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
                    raise EvidenceIntegrityError(
                        f"a nested schema_version {item!r} is not a published contract version"
                    )
                stripped[key] = item
        return stripped
    if isinstance(value, list):
        return [_strip_envelope(item, version) for item in value]
    return value


def evidence_record_bytes(record: Contract) -> bytes:
    """The one line ``record`` is stored as (``EVIDENCE_PROJECTION``).

    The record's own envelope is never in the bytes, nor is any nested envelope equal to it; a
    nested envelope of another published version is kept (module docstring).
    """
    if not isinstance(record, Contract):
        raise EvidenceIntegrityError(f"an evidence record must be a contract, got {record!r}")
    payload = record.model_dump(mode="json")
    version = payload.pop("schema_version")
    return (canonical_json(_strip_envelope(payload, version)) + "\n").encode("utf-8")


def evidence_record_from_bytes(
    stream: EvidenceStream, line: bytes, *, schema_version: str
) -> Contract:
    """The record ``line`` (with its LF) projects from, rebuilt at ``schema_version``, or raise.

    The line must be the unique projection of the record it parses to: any other encoding of the
    same values (key order, whitespace, escapes, a stored top-level envelope, a nested envelope
    equal to ``schema_version``, duplicate keys) is rejected. A nested envelope of another
    published version, as the writer keeps it, is rebuilt as stored.
    """
    stream = EvidenceStream(stream)
    record_type = EVIDENCE_RECORD_TYPES[stream]
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"{stream.value} record is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceIntegrityError(f"{stream.value} record is not a JSON object")
    try:
        with contract_schema_version_scope(schema_version):
            record = record_type.model_validate(payload)
    except ValidationError as exc:
        raise EvidenceIntegrityError(
            f"{stream.value} record is not a valid {record_type.__name__}: {exc}"
        ) from exc
    if evidence_record_bytes(record) != line:
        raise EvidenceIntegrityError(
            f"{stream.value} record bytes are not the canonical projection of their record"
        )
    return record


# =========================================================================================
# objects
# =========================================================================================


@dataclass(frozen=True, slots=True)
class _Child:
    """A reference line of an index object (or the root reference of a stream)."""

    key: str
    sha256: str
    size: int
    first_ordinal: int
    record_count: int


def _line(document: Mapping[str, Any], limit: int, what: str) -> bytes:
    data = (canonical_json(dict(document)) + "\n").encode("utf-8")
    if len(data) > limit:  # pragma: no cover - fixed-width fields cannot reach it
        raise EvidenceIntegrityError(f"{what} line exceeds {limit} bytes")
    return data


def _leaf_header(stream: EvidenceStream, first: int, count: int) -> bytes:
    return _line(
        {
            "first_ordinal": first,
            "format": DATASET_EVIDENCE_FORMAT,
            "node": _LEAF,
            "record_count": count,
            "stream": stream.value,
        },
        EVIDENCE_HEADER_MAX_BYTES,
        "leaf header",
    )


def _index_header(stream: EvidenceStream, level: int, first: int, count: int) -> bytes:
    return _line(
        {
            "first_ordinal": first,
            "format": DATASET_EVIDENCE_FORMAT,
            "level": level,
            "node": _INDEX,
            "record_count": count,
            "stream": stream.value,
        },
        EVIDENCE_HEADER_MAX_BYTES,
        "index header",
    )


def _child_line(child: _Child) -> bytes:
    return _line(
        {
            "first_ordinal": child.first_ordinal,
            "key": child.key,
            "record_count": child.record_count,
            "sha256": child.sha256,
            "size": child.size,
        },
        EVIDENCE_REF_LINE_MAX_BYTES,
        "child reference",
    )


def publish_evidence_object(storage: StorageAdapter, data: bytes) -> EvidenceObjectRef:
    """Hash ``data``, stage it with that ``expected_sha256`` and publish it (DQ-11 = a).

    Idempotent: identical bytes are ``already_present`` under the same content key.
    """
    if not isinstance(data, bytes) or not data:
        raise EvidenceIntegrityError("an evidence object is a non-empty bytes value")
    sha256 = hashlib.sha256(data).hexdigest()
    key = dataset_evidence_key(sha256)
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=sha256, expected_size=len(data)), (data,)
    )
    published = storage.publish(staged).ref
    if (published.key, published.sha256, published.size) != (key, sha256, len(data)):
        raise EvidenceIntegrityError(f"evidence object {key} published as another identity")
    return EvidenceObjectRef(key=key, sha256=sha256, size=len(data))


def _published_child(storage: StorageAdapter, data: bytes, first: int, count: int) -> _Child:
    ref = publish_evidence_object(storage, data)
    return _Child(ref.key, ref.sha256, ref.size, first, count)


# =========================================================================================
# writer
# =========================================================================================


class EvidenceTreeWriter:
    """Writes one stream's records, in the order given, as a canonical evidence tree.

    Ordering and uniqueness are the caller's (the dataset generator checks its streams); this
    writer proves the projection, the write group's single contract version and the size bounds.
    ``finish`` publishes what is buffered and returns the stream's ``EvidenceStreamRef``; a writer
    abandoned before ``finish`` leaves only unreferenced, content-addressed orphans (§9).
    """

    def __init__(
        self,
        storage: StorageAdapter,
        stream: EvidenceStream,
        *,
        limits: EvidenceTreeLimits,
        schema_version: str,
    ) -> None:
        if not isinstance(limits, EvidenceTreeLimits):
            raise EvidenceLimitError("limits must be EvidenceTreeLimits")
        if schema_version not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
            raise EvidenceIntegrityError(f"{schema_version!r} is not a published contract version")
        self._storage = storage
        self._stream = EvidenceStream(stream)
        self._type = EVIDENCE_RECORD_TYPES[self._stream]
        self._limits = limits
        self._version = schema_version
        self._leaf: list[bytes] = []
        self._leaf_bytes = 0
        self._leaf_first = 0
        self._count = 0
        self._leaf_count = 0
        #: ``_levels[i]`` buffers the children of the open level ``i + 1`` node (<= fanout).
        self._levels: list[list[_Child]] = []
        self._finished = False

    @property
    def stream(self) -> EvidenceStream:
        return self._stream

    @property
    def record_count(self) -> int:
        return self._count

    def append(self, record: Contract) -> None:
        if self._finished:
            raise EvidenceIntegrityError(f"{self._stream.value} writer is already finished")
        if type(record) is not self._type:
            raise EvidenceIntegrityError(
                f"{self._stream.value} records are {self._type.__name__}, got "
                f"{type(record).__name__}"
            )
        if record.schema_version != self._version:
            raise EvidenceIntegrityError(
                f"{self._stream.value} record at contract {record.schema_version} in a write "
                f"group of {self._version}: a group has exactly one version"
            )
        line = evidence_record_bytes(record)
        if len(line) > self._limits.leaf_max_bytes:
            raise EvidenceRecordTooLarge(
                f"a {self._stream.value} record of {len(line)} bytes exceeds leaf_max_bytes "
                f"{self._limits.leaf_max_bytes}"
            )
        again = evidence_record_from_bytes(self._stream, line, schema_version=self._version)
        if again.model_dump(mode="python") != record.model_dump(mode="python"):
            raise EvidenceIntegrityError(
                f"the {self._stream.value} projection of this record is lossy"
            )
        if self._leaf and (
            len(self._leaf) == self._limits.leaf_max_records
            or self._leaf_bytes + len(line) > self._limits.leaf_max_bytes
        ):
            self._flush_leaf()
        self._leaf.append(line)
        self._leaf_bytes += len(line)
        self._count += 1

    def finish(self) -> EvidenceStreamRef:
        if self._finished:
            raise EvidenceIntegrityError(f"{self._stream.value} writer is already finished")
        if self._leaf:
            self._flush_leaf()
        root = self._finish_index()
        self._finished = True
        return EvidenceStreamRef(
            stream=self._stream,
            format=DATASET_EVIDENCE_FORMAT,
            record_count=self._count,
            leaf_count=self._leaf_count,
            depth=canonical_depth(self._leaf_count, self._limits.fanout),
            root=EvidenceObjectRef(key=root.key, sha256=root.sha256, size=root.size),
        )

    # ------------------------------------------------------------------ internals

    def _flush_leaf(self) -> None:
        count = len(self._leaf)
        data = _leaf_header(self._stream, self._leaf_first, count) + b"".join(self._leaf)
        child = _published_child(self._storage, data, self._leaf_first, count)
        self._leaf_first += count
        self._leaf = []
        self._leaf_bytes = 0
        self._leaf_count += 1
        self._push(0, child)

    def _push(self, index: int, child: _Child) -> None:
        """Add ``child`` to level ``index + 1``; a full level is emitted only when it overflows."""
        while len(self._levels) <= index:
            self._levels.append([])
        buffer = self._levels[index]
        if len(buffer) == self._limits.fanout:
            node = self._emit(index + 1, buffer)
            self._levels[index] = [child]
            self._push(index + 1, node)
        else:
            buffer.append(child)

    def _emit(self, level: int, children: list[_Child]) -> _Child:
        first = children[0].first_ordinal if children else 0
        count = sum(child.record_count for child in children)
        data = _index_header(self._stream, level, first, count) + b"".join(
            _child_line(child) for child in children
        )
        return _published_child(self._storage, data, first, count)

    def _finish_index(self) -> _Child:
        index = 0
        while True:
            above = any(self._levels[higher] for higher in range(index + 1, len(self._levels)))
            if not above:
                children = self._levels[index] if index < len(self._levels) else []
                return self._emit(index + 1, children)
            buffer = self._levels[index]
            self._levels[index] = []
            if buffer:
                self._push(index + 1, self._emit(index + 1, buffer))
            index += 1


# =========================================================================================
# reader
# =========================================================================================


def _int_field(document: Mapping[str, Any], name: str, what: str, *, minimum: int = 0) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceIntegrityError(f"{what}: {name} must be an integer >= {minimum}")
    return value


def _json_line(line: bytes, limit: int, keys: frozenset[str], what: str) -> dict[str, Any]:
    if len(line) + 1 > limit:
        raise EvidenceIntegrityError(f"{what} line exceeds {limit} bytes")
    try:
        document = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"{what} is not UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != keys:
        raise EvidenceIntegrityError(f"{what} does not have exactly the fields {sorted(keys)}")
    if (canonical_json(document)).encode("utf-8") != line:
        raise EvidenceIntegrityError(f"{what} is not canonical JSON")
    return document


def _parse_header(
    line: bytes, stream: EvidenceStream, node: str, parent: _Child, level: int | None
) -> None:
    keys = _INDEX_HEADER_KEYS if node == _INDEX else _LEAF_HEADER_KEYS
    what = f"{stream.value} {node} header of {parent.key}"
    header = _json_line(line, EVIDENCE_HEADER_MAX_BYTES, keys, what)
    if header["format"] != DATASET_EVIDENCE_FORMAT:
        raise EvidenceIntegrityError(f"{what}: format is not {DATASET_EVIDENCE_FORMAT}")
    if header["stream"] != stream.value or header["node"] != node:
        raise EvidenceIntegrityError(f"{what}: names another stream or node kind")
    first = _int_field(header, "first_ordinal", what)
    count = _int_field(header, "record_count", what)
    if (first, count) != (parent.first_ordinal, parent.record_count):
        raise EvidenceIntegrityError(
            f"{what}: covers [{first}, +{count}) but its parent says "
            f"[{parent.first_ordinal}, +{parent.record_count})"
        )
    if level is not None and _int_field(header, "level", what, minimum=1) != level:
        raise EvidenceIntegrityError(f"{what}: level is not {level}")


def _parse_child(line: bytes, what: str) -> _Child:
    document = _json_line(line, EVIDENCE_REF_LINE_MAX_BYTES, _CHILD_KEYS, what)
    sha256, key = document["sha256"], document["key"]
    try:
        expected_key = dataset_evidence_key(sha256)
    except ValueError as exc:
        raise EvidenceIntegrityError(f"{what}: sha256 is not a content hash") from exc
    if key != expected_key:
        raise EvidenceIntegrityError(f"{what}: key is not the content key of its sha256")
    return _Child(
        key=key,
        sha256=sha256,
        size=_int_field(document, "size", what, minimum=1),
        first_ordinal=_int_field(document, "first_ordinal", what),
        record_count=_int_field(document, "record_count", what, minimum=1),
    )


def _read_object(storage: StorageAdapter, child: _Child, limit: int) -> list[bytes]:
    """The lines (LF stripped) of the object ``child`` names, resolved by ``lookup`` first."""
    if child.size > limit:
        raise EvidenceIntegrityError(f"evidence object {child.key} exceeds {limit} bytes")
    try:
        found = storage.lookup(child.key)
        if found is None:
            raise EvidenceIntegrityError(f"evidence object {child.key} is missing")
        if (found.key, found.sha256, found.size) != (child.key, child.sha256, child.size):
            raise EvidenceIntegrityError(
                f"evidence object {child.key} is stored as another identity than referenced"
            )
        with storage.open_read(found) as handle:
            data: bytes = handle.read(child.size + 1)
    except StorageError as exc:
        raise EvidenceIntegrityError(f"evidence object {child.key} cannot be read: {exc}") from exc
    if len(data) != child.size or hashlib.sha256(data).hexdigest() != child.sha256:
        raise EvidenceIntegrityError(f"evidence object {child.key} bytes differ from its reference")
    if not data.endswith(b"\n"):
        raise EvidenceIntegrityError(f"evidence object {child.key} does not end with LF")
    return data[:-1].split(b"\n")


@dataclass(slots=True)
class _Walk:
    storage: StorageAdapter
    stream: EvidenceStream
    limits: EvidenceTreeLimits
    version: str
    next_ordinal: int = 0
    leaves: int = 0
    #: (record count, record bytes) of the previous leaf: its closing must be canonical.
    previous_leaf: tuple[int, int] | None = None

    def index(self, ref: _Child, level: int, *, rightmost: bool, root: bool) -> Iterator[Contract]:
        lines = _read_object(self.storage, ref, self.limits.index_object_max_bytes)
        _parse_header(lines[0], self.stream, _INDEX, ref, level)
        what = f"{self.stream.value} index {ref.key}"
        children = [_parse_child(line, what) for line in lines[1:]]
        if len(children) > self.limits.fanout:
            raise EvidenceIntegrityError(f"{what} has more than fanout children")
        if not children and not (root and level == 1 and ref.record_count == 0):
            raise EvidenceIntegrityError(f"{what} has no children")
        if not rightmost and len(children) != self.limits.fanout:
            raise EvidenceIntegrityError(f"{what} is not full off the rightmost path")
        running = ref.first_ordinal
        for child in children:
            if child.first_ordinal != running:
                raise EvidenceIntegrityError(f"{what} children are not contiguous")
            running += child.record_count
        if running != ref.first_ordinal + ref.record_count:
            raise EvidenceIntegrityError(f"{what} children do not sum to its record count")
        for position, child in enumerate(children):
            last = rightmost and position == len(children) - 1
            if level == 1:
                yield from self.leaf(child)
            else:
                yield from self.index(child, level - 1, rightmost=last, root=False)

    def leaf(self, ref: _Child) -> Iterator[Contract]:
        """A leaf's own closing is checked by the next leaf; the last one may be short."""
        lines = _read_object(self.storage, ref, self.limits.leaf_object_max_bytes)
        _parse_header(lines[0], self.stream, _LEAF, ref, None)
        what = f"{self.stream.value} leaf {ref.key}"
        if ref.first_ordinal != self.next_ordinal:
            raise EvidenceIntegrityError(f"{what} does not continue at ordinal {self.next_ordinal}")
        records = [line + b"\n" for line in lines[1:]]
        size = sum(len(line) for line in records)
        count = len(records)
        if count != ref.record_count or not 1 <= count <= self.limits.leaf_max_records:
            raise EvidenceIntegrityError(f"{what} record count disagrees with its header / limits")
        if size > self.limits.leaf_max_bytes:
            raise EvidenceIntegrityError(f"{what} exceeds leaf_max_bytes")
        if self.previous_leaf is not None:
            previous_count, used = self.previous_leaf
            if previous_count != self.limits.leaf_max_records and used + len(records[0]) <= (
                self.limits.leaf_max_bytes
            ):
                raise EvidenceIntegrityError(f"the leaf before {what} was closed early")
        parsed = [
            evidence_record_from_bytes(self.stream, line, schema_version=self.version)
            for line in records
        ]
        self.previous_leaf = (count, size)
        self.leaves += 1
        self.next_ordinal += count
        yield from parsed


def _walk(
    storage: StorageAdapter,
    ref: EvidenceStreamRef,
    limits: EvidenceTreeLimits,
    schema_version: str,
) -> Generator[Contract]:
    if ref.format != DATASET_EVIDENCE_FORMAT:
        raise EvidenceIntegrityError(f"{ref.stream.value} stream is not {DATASET_EVIDENCE_FORMAT}")
    if ref.depth != canonical_depth(ref.leaf_count, limits.fanout):
        raise EvidenceIntegrityError(
            f"{ref.stream.value} stream depth {ref.depth} is not the canonical depth of "
            f"{ref.leaf_count} leaves at fanout {limits.fanout}"
        )
    walk = _Walk(storage, ref.stream, limits, schema_version)
    root = _Child(ref.root.key, ref.root.sha256, ref.root.size, 0, ref.record_count)
    yield from walk.index(root, ref.depth, rightmost=True, root=True)
    if walk.next_ordinal != ref.record_count or walk.leaves != ref.leaf_count:
        raise EvidenceIntegrityError(  # pragma: no cover - the root header / sums prove it
            f"{ref.stream.value} stream has {walk.next_ordinal} records in {walk.leaves} leaves, "
            f"not {ref.record_count} in {ref.leaf_count}"
        )


@contextmanager
def iter_evidence_stream(
    storage: StorageAdapter,
    ref: EvidenceStreamRef,
    *,
    limits: EvidenceTreeLimits,
    schema_version: str,
) -> Iterator[Iterator[Contract]]:
    """The records ``ref`` commits, in ordinal order, rebuilt at ``schema_version``.

    Every yielded record is authenticated by the root hash; any disagreement raises
    ``EvidenceIntegrityError`` (before a bad object's records are yielded). The iterator is
    released on exhaustion, close, error or early exit of the ``with`` block; no object handle
    is held between records.
    """
    if not isinstance(ref, EvidenceStreamRef):
        raise EvidenceIntegrityError("ref must be an EvidenceStreamRef")
    if not isinstance(limits, EvidenceTreeLimits):
        raise EvidenceLimitError("limits must be EvidenceTreeLimits")
    if schema_version not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        raise EvidenceIntegrityError(f"{schema_version!r} is not a published contract version")
    records = _walk(storage, ref, limits, schema_version)
    try:
        yield records
    finally:
        records.close()


@contextmanager
def iter_evidence(
    storage: StorageAdapter,
    manifest: ResearchDatasetEvidenceManifest,
    stream: EvidenceStream,
    *,
    limits: EvidenceTreeLimits,
) -> Iterator[Iterator[Contract]]:
    """``iter_evidence_stream`` of one of ``manifest``'s streams at its recorded version (V7).

    Proves only "this is the stream the manifest commits", not that it is the right derivation
    (that is the streaming verifier, §6). ``limits`` must be those of ``manifest.rule``; checking
    that binding is the caller's (``DatasetEvidenceBuilder.iter_evidence`` does).
    """
    if not isinstance(manifest, ResearchDatasetEvidenceManifest):
        raise EvidenceIntegrityError("manifest must be a ResearchDatasetEvidenceManifest")
    version = contract_version.replay_version(
        manifest.schema_version, what=f"evidence manifest {manifest.content_hash()}"
    )
    ref = manifest.evidence_for(EvidenceStream(stream))
    with iter_evidence_stream(storage, ref, limits=limits, schema_version=version) as records:
        yield records
