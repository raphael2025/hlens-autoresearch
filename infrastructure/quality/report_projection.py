"""Versioned event and event-revision projection for Quality canonical-partition v3.

This module defines only the canonical-partition v3 event/revision projection. Legacy report
versions keep their inline event shape and historical report/event hashes. Revision IDs are
externally sorted and deduplicated through bounded content-addressed RunSets; no collection grows
with the number of IDs.

The event ID is SHA-256 over the domain tag
``hlens.quality.canonical-partition@3.0.0/event-id/v1\\0``, followed by an unsigned 8-byte
big-endian length and canonical UTF-8 JSON of the v3 rule identity plus every fixed event field,
then one unsigned 8-byte length and UTF-8 byte sequence for each sorted unique revision ID. The
fixed JSON includes ``revision_count``; IDs therefore have unambiguous boundaries and the digest
commits to the complete ordered sequence.

Use :class:`CanonicalPartitionV3Projector` as a context manager per event. The caller writes the
fixed event mapping and consumes the ordered revision mappings into the ADR-0093 stream writers
inside that context. Leaving early closes the RunSet reader and makes the projector fail closed;
the next event cannot conceal an incomplete revision stream.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = [
    "CANONICAL_PARTITION_V3_RULE_HASH",
    "CANONICAL_PARTITION_V3_RULE_ID",
    "CANONICAL_PARTITION_V3_RULE_SPEC",
    "CANONICAL_PARTITION_V3_RULE_VERSION",
    "CanonicalPartitionProjectionError",
    "CanonicalPartitionV3Projector",
    "ProjectedCanonicalEvent",
]

CANONICAL_PARTITION_V3_RULE_ID: Final = "hlens.quality.canonical-partition"
CANONICAL_PARTITION_V3_RULE_VERSION: Final = "3.0.0"
CANONICAL_PARTITION_V3_RULE_SPEC: Final[dict[str, Any]] = {
    "rule": CANONICAL_PARTITION_V3_RULE_ID,
    "version": CANONICAL_PARTITION_V3_RULE_VERSION,
    "subject": "one Canonical partition: table x venue symbol x UTC day",
    "inputs": "current heads of the Canonical table, both Raw element tables, their source tables "
    "and the Raw evidence table, pinned; the report is computed on exactly those snapshots",
    "proof": "hlens.pit.maximal-head@1.0.0 selector proofs (Canonical re-normalized, edges "
    "re-derived); an unprovable partition gets no report",
    "events": {
        "report_inputs": "the bound snapshots (canonical JSON)",
        "competing_heads": "key with >1 maximal head, knowledge_cutoff and simulation at the end "
        "of time; all unique heads are identified by ordered event_revisions records",
        "bar_1m_gap": "maximal run of minutes of the day without any Canonical bar revision",
        "agg_trade_id_discontinuity": "jump between consecutive aggregate trade ids of the day",
        "bar_1m_invariant_violation": "low/high do not bound open/close, low > high, or a taker "
        "volume exceeds its total",
        "evidence_gaps": "number of evidence-gap records in the report's evidence_gaps stream "
        "(ADR-0031)",
    },
    "thresholds": "none (outliers need calibrated thresholds: a later rule version)",
    "evidence_gaps": "every Canonical revision of the partition with an evidence gap is written "
    "as a fixed record in the ordered evidence_gaps stream before the report manifest is "
    "committed; "
    "legacy v1/v2 report rows and hashes remain unchanged (ADR-0031 / ADR-0093)",
    "report_id": "<rule>@<version>.<table>.<venue symbol>.<day>.<sha256 of the rule hashes used "
    "(this set, the PIT rule, its required policies) and the bound snapshots>",
    "knowledge_time": "first commit's clock reading, reused by every replay",
    "legacy_compatibility": "v1/v2 inline reports and their report/event hashes remain unchanged",
    "event_projection": {
        "format": "hlens.quality.report-jsonl@1.0.0",
        "event_fields": {
            "event_id": "qevt3- + lowercase SHA-256 hex",
            "event_type": "non-empty string",
            "table": "non-empty string or null",
            "observation_key": "non-empty string or null",
            "revision_first_ordinal": "non-negative integer; global zero-based stream ordinal",
            "revision_count": "non-negative integer; number of unique revision IDs",
            "event_start": "UTC datetime ISO-8601 with +00:00, or null",
            "event_end": "UTC datetime ISO-8601 with +00:00, or null",
            "detail": "non-empty string",
        },
        "record_projection": (
            "canonical_json(record mapping) encoded as UTF-8 followed by one LF; no BOM"
        ),
        "revision_records": {
            "fields": ["event_ordinal", "revision_id"],
            "order": "event_ordinal ascending, then revision_id ascending by Unicode code point",
            "revision_id_text": "exact UTF-8 scalar sequence; no Unicode normalization",
            "duplicates": "one record per unique revision_id within an event",
        },
        "event_id": {
            "algorithm": "SHA-256",
            "domain_separator": {
                "ascii_prefix": "hlens.quality.canonical-partition@3.0.0/event-id/v1",
                "terminal_byte_hex": "00",
            },
            "fixed_fields_encoding": (
                "8-byte unsigned big-endian byte length + canonical_json UTF-8"
            ),
            "revision_id_encoding": (
                "per sorted unique ID: 8-byte unsigned big-endian UTF-8 length, then bytes"
            ),
            "identity_fields": [
                "rule_id",
                "rule_version",
                "rule_hash",
                "event_type",
                "table",
                "observation_key",
                "revision_first_ordinal",
                "revision_count",
                "event_start",
                "event_end",
                "detail",
                "every sorted unique revision_id",
            ],
        },
        "resource_parameters": (
            "Run capacity, merge fanout, and RunLimits are explicit caller inputs; no defaults"
        ),
    },
}
CANONICAL_PARTITION_V3_RULE_HASH: Final = hashlib.sha256(
    canonical_json(CANONICAL_PARTITION_V3_RULE_SPEC).encode("utf-8")
).hexdigest()

_EVENT_ID_DOMAIN: Final = b"hlens.quality.canonical-partition@3.0.0/event-id/v1\x00"


class CanonicalPartitionProjectionError(ValueError):
    """Event inputs or RunSet traversal do not satisfy the v3 projection rule."""


@dataclass(frozen=True, slots=True)
class ProjectedCanonicalEvent:
    """One fixed event record and its ordered fixed-size revision records."""

    event_record: Mapping[str, Any]
    revision_records: Iterator[Mapping[str, Any]]


def _positive_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CanonicalPartitionProjectionError(f"{name} must be an integer >= {minimum}")
    return value


def _text(name: str, value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        qualifier = " or null" if optional else ""
        raise CanonicalPartitionProjectionError(f"{name} must be a non-empty string{qualifier}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalPartitionProjectionError(f"{name} must be valid UTF-8 text") from exc
    return value


def _time(name: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise CanonicalPartitionProjectionError(f"{name} must be a datetime or null")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CanonicalPartitionProjectionError(f"{name} has an invalid UTC offset") from exc
    if value.tzinfo is None or offset is None:
        raise CanonicalPartitionProjectionError(f"{name} must be timezone-aware")
    if offset != timedelta(0):
        raise CanonicalPartitionProjectionError(f"{name} must have UTC offset +00:00")
    return value.astimezone(UTC).isoformat()


def _validate_revision_id(value: object) -> str:
    revision_id = _text("revision_id", value)
    assert revision_id is not None
    return revision_id


def _run_row(revision_id: str) -> Mapping[str, Any]:
    return {"revision_id": revision_id}


def _unique_revision_ids(rows: Iterable[Mapping[str, Any]]) -> Iterator[str]:
    previous: str | None = None
    for row in rows:
        revision_id = row.get("revision_id")
        if not isinstance(revision_id, str):
            raise CanonicalPartitionProjectionError("RunSet contains an invalid revision_id")
        if previous is None or revision_id != previous:
            yield revision_id
        previous = revision_id


def _unique_count(storage: StorageAdapter, run: RunRef | None) -> int:
    if run is None:
        return 0
    count = 0
    with iter_run(storage, run) as rows:
        for _ in _unique_revision_ids(rows):
            count += 1
    return count


def _length_prefixed(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big", signed=False))
    hasher.update(value)


def _event_id(
    storage: StorageAdapter,
    run: RunRef | None,
    *,
    event_fields: Mapping[str, Any],
    revision_count: int,
) -> str:
    identity = {
        "rule_id": CANONICAL_PARTITION_V3_RULE_ID,
        "rule_version": CANONICAL_PARTITION_V3_RULE_VERSION,
        "rule_hash": CANONICAL_PARTITION_V3_RULE_HASH,
        "event_fields": dict(event_fields),
    }
    encoded_fields = canonical_json(identity).encode("utf-8")
    hasher = hashlib.sha256()
    hasher.update(_EVENT_ID_DOMAIN)
    _length_prefixed(hasher, encoded_fields)
    seen = 0
    if run is not None:
        with iter_run(storage, run) as rows:
            for revision_id in _unique_revision_ids(rows):
                _length_prefixed(hasher, revision_id.encode("utf-8"))
                seen += 1
    if seen != revision_count:
        raise CanonicalPartitionProjectionError(
            f"RunSet unique revision count changed from {revision_count} to {seen}"
        )
    return f"qevt3-{hasher.hexdigest()}"


def _revision_records(
    rows: Iterator[Mapping[str, Any]], *, event_ordinal: int
) -> Iterator[Mapping[str, Any]]:
    for revision_id in _unique_revision_ids(rows):
        yield {"event_ordinal": event_ordinal, "revision_id": revision_id}


class CanonicalPartitionV3Projector:
    """Assign report-global ordinals and project events with bounded revision-ID sorting.

    ``capacity``, ``merge_fanout``, and ``limits`` are required. Each event's revision IDs are
    staged as one-field RunSet records, then read in sorted order for unique counting, ID hashing,
    and output. The projector advances the next global revision ordinal by exactly the unique
    revision count. If a caller exits an event context before consuming all revision records, the
    projector becomes unusable so no later event can hide the incomplete ordinal range.
    """

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        capacity: int,
        merge_fanout: int,
        limits: RunLimits,
    ) -> None:
        _positive_int("capacity", capacity, minimum=1)
        _positive_int("merge_fanout", merge_fanout, minimum=2)
        if not isinstance(limits, RunLimits):
            raise CanonicalPartitionProjectionError("limits must be a RunLimits instance")
        self._storage = storage
        self._capacity = capacity
        self._merge_fanout = merge_fanout
        self._limits = limits
        self._event_ordinal = 0
        self._revision_ordinal = 0
        self._failed = False

    @property
    def next_event_ordinal(self) -> int:
        return self._event_ordinal

    @property
    def next_revision_ordinal(self) -> int:
        return self._revision_ordinal

    @contextmanager
    def project_event(
        self,
        *,
        event_type: str,
        table: str | None,
        observation_key: str | None,
        revision_ids: Iterable[str],
        event_start: datetime | None,
        event_end: datetime | None,
        detail: str,
    ) -> Iterator[ProjectedCanonicalEvent]:
        if self._failed:
            raise CanonicalPartitionProjectionError("projector is failed after an incomplete event")
        event_type_text = _text("event_type", event_type)
        detail_text = _text("detail", detail)
        assert event_type_text is not None and detail_text is not None
        event_type = event_type_text
        table = _text("table", table, optional=True)
        observation_key = _text("observation_key", observation_key, optional=True)
        detail = detail_text
        event_start_text = _time("event_start", event_start)
        event_end_text = _time("event_end", event_end)
        if isinstance(revision_ids, str | bytes) or not isinstance(revision_ids, Iterable):
            raise CanonicalPartitionProjectionError("revision_ids must be iterable")

        event_ordinal = _positive_int("event_ordinal", self._event_ordinal)
        revision_first_ordinal = _positive_int("revision_first_ordinal", self._revision_ordinal)
        builder = RunSetBuilder(
            self._storage,
            key=lambda row: row["revision_id"],
            capacity=self._capacity,
            merge_fanout=self._merge_fanout,
            limits=self._limits,
        )
        try:
            with builder:
                for raw_id in revision_ids:
                    builder.add(_run_row(_validate_revision_id(raw_id)))
                run = builder.finish()
                revision_count = _positive_int("revision_count", _unique_count(self._storage, run))
                event_fields = {
                    "event_type": event_type,
                    "table": table,
                    "observation_key": observation_key,
                    "revision_first_ordinal": revision_first_ordinal,
                    "revision_count": revision_count,
                    "event_start": event_start_text,
                    "event_end": event_end_text,
                    "detail": detail,
                }
                event_record = {
                    "event_id": _event_id(
                        self._storage,
                        run,
                        event_fields=event_fields,
                        revision_count=revision_count,
                    ),
                    **event_fields,
                }
                emitted = 0
                exhausted = revision_count == 0
                rows_context = iter_run(self._storage, run) if run is not None else None
                if rows_context is None:
                    revision_rows: Iterator[Mapping[str, Any]] = iter(())
                    close_rows = None
                else:
                    rows = rows_context.__enter__()
                    revision_rows = _revision_records(rows, event_ordinal=event_ordinal)
                    close_rows = rows
                body_raised = False
                try:

                    class _CountedRevisionIterator(Iterator[Mapping[str, Any]]):
                        def __iter__(self) -> _CountedRevisionIterator:
                            return self

                        def __next__(self) -> Mapping[str, Any]:
                            nonlocal emitted, exhausted
                            try:
                                record: Mapping[str, Any] = next(revision_rows)
                            except StopIteration:
                                exhausted = True
                                raise
                            emitted += 1
                            return record

                    try:
                        yield ProjectedCanonicalEvent(
                            event_record=event_record,
                            revision_records=_CountedRevisionIterator(),
                        )
                    except BaseException:
                        body_raised = True
                        self._failed = True
                        raise
                finally:
                    close = getattr(revision_rows, "close", None)
                    if callable(close):
                        close()
                    if close_rows is not None:
                        close = getattr(close_rows, "close", None)
                        if callable(close):
                            close()
                        assert rows_context is not None
                        rows_context.__exit__(None, None, None)
                    if not exhausted or emitted != revision_count or self._failed:
                        self._failed = True
                        if not body_raised:
                            raise CanonicalPartitionProjectionError(
                                "revision records were not fully consumed"
                            )
                    else:
                        self._event_ordinal = event_ordinal + 1
                        self._revision_ordinal = revision_first_ordinal + revision_count
        except BaseException:
            self._failed = True
            raise
        finally:
            builder.close()
