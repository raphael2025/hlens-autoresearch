"""Append-only persistence of event runs in ``event.events`` (Phase 3; ADR-0056).

``EventTable(adapter)`` is the one writer / reader of the physical Event table:

- ``write(result)``: one run = one batch = one Iceberg snapshot, ``batch_id = event.{result_hash}``.
  The existing rows of ``result_hash`` are read at the current head first: equal rows are a
  replay (nothing committed), other rows are ``EventTableConflict`` (never appended, never
  overwritten). Otherwise the batch is committed against that head; a lost optimistic race
  (``CommitConflict``) re-reads and retries, the same batch id with other content
  (``BatchConflict``) is ``EventTableConflict``. The committed rows are read back at the new
  snapshot and must equal what was written. An empty run (no events) writes nothing: a commit
  needs at least one row, so the whole run belongs in ``EventResultStore``.
- ``read(result_hash, snapshot_id=None)``: pinned to one snapshot (the head, resolved once, when
  none is given; a snapshot the table does not have is ``SnapshotNotFound``, never the head).
  The rows are verified before anything is returned — one run block, ``event_index`` exactly
  ``0..n-1`` with ``n == event_count``, the ``EventResult`` rebuilt through its contract (which
  re-checks every ``event_id`` and the ``result_hash``) and ``event_table(rebuilt)`` equal to the
  stored logical columns; any disagreement is ``EventTableCorrupted`` (never repaired). A run the
  snapshot does not hold is ``None``.

There is no delete / overwrite / update path. The adapter must be a catalog adapter whose
registry contains ``PHASE3_TABLES`` (see ``table_definition``); this module never creates tables.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import pyarrow as pa  # type: ignore[import-untyped]
from pydantic import ValidationError
from pyiceberg.expressions import BooleanExpression, EqualTo

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    TableInfo,
    TableNotFound,
)
from core.contracts.event import EventResult
from core.domain.base import Ref
from infrastructure.event.table import EventTableRow, event_table
from infrastructure.event.table_definition import EVENT_EVENTS

__all__ = [
    "EventCatalog",
    "EventRunWrite",
    "EventTable",
    "EventTableConflict",
    "EventTableCorrupted",
    "EventTableError",
    "StoredEventRun",
    "event_batch_id",
    "event_rows",
    "event_rows_batch",
]

_TABLE: Final = EVENT_EVENTS.table
_COLUMNS: Final = tuple(field.name for field in EVENT_EVENTS.arrow_schema)
_RUN_KEYS: Final = (
    "provider",
    "result_hash",
    "event_count",
    "request_hash",
    "provider_hash",
    "as_of",
    "subject",  # one request per subject (ADR-0057): every row of a run has the run's subject
)
_ATTEMPTS: Final = 8


class EventTableError(RuntimeError):
    """Base class of ``event.events`` persistence errors."""


class EventTableConflict(EventTableError):
    """The table already holds other content under this run; nothing was written."""


class EventTableCorrupted(EventTableError):
    """Stored rows do not verify as the run they claim to be (never repaired)."""


class EventCatalog(Protocol):
    """The catalog capability this module needs; ``PyIcebergCatalogAdapter`` satisfies it."""

    def load_table(self, table: str) -> TableInfo | None: ...

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult: ...

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = ...,
        limit: int | None = ...,
        snapshot_id: str | None = ...,
    ) -> pa.Table: ...


def event_batch_id(result_hash: str) -> str:
    return f"event.{result_hash}"


def event_rows(result: EventResult) -> list[dict[str, Any]]:
    """The rows of ``result`` as ``event.events`` holds them (normalised through its schema)."""
    return list(event_rows_batch(result).to_pylist())


def event_rows_batch(result: EventResult) -> pa.Table:
    """The ``pyarrow.Table`` batch of ``result`` (logical Event table + run block)."""
    if not isinstance(result, EventResult):
        raise TypeError("event rows need an EventResult")
    rows = event_table(result)
    records = [
        {
            "event_id": row.event_id,
            "event": row.event,
            "spec_hash": row.spec_hash,
            "event_time": row.event_time,
            "attributes_json": row.attributes_json,
            "input_ids": list(row.input_ids),
            "upstream_event_ids": list(row.upstream_event_ids),
            "provider": row.provider,
            "result_hash": row.result_hash,
            "subject": row.subject,
            "event_index": index,
            "event_count": len(rows),
            "request_hash": result.request_hash,
            "provider_hash": result.provider_hash,
            "as_of": result.as_of,
        }
        for index, row in enumerate(rows)
    ]
    return pa.Table.from_pylist(records, schema=EVENT_EVENTS.arrow_schema)


def _logical_row(row: dict[str, Any]) -> EventTableRow:
    return EventTableRow(
        event_id=row["event_id"],
        event=row["event"],
        spec_hash=row["spec_hash"],
        event_time=row["event_time"],
        attributes_json=row["attributes_json"],
        input_ids=tuple(row["input_ids"]),
        upstream_event_ids=tuple(row["upstream_event_ids"]),
        provider=row["provider"],
        result_hash=row["result_hash"],
        subject=row["subject"],
    )


def _rebuild(result_hash: str, rows: list[dict[str, Any]]) -> EventResult:
    """Verify ``rows`` (sorted by ``event_index``) as the complete run ``result_hash``."""
    head = rows[0]
    if any(row[key] != head[key] for row in rows for key in _RUN_KEYS):
        raise EventTableCorrupted(f"event run {result_hash}: rows disagree on the run block")
    if head["result_hash"] != result_hash:
        raise EventTableCorrupted(f"event run {result_hash}: rows claim another run")
    if [row["event_index"] for row in rows] != list(range(len(rows))):
        raise EventTableCorrupted(f"event run {result_hash}: event_index is not 0..n-1")
    if head["event_count"] != len(rows):
        raise EventTableCorrupted(
            f"event run {result_hash}: {len(rows)} rows but event_count {head['event_count']}"
        )
    try:
        document = {
            "request_hash": head["request_hash"],
            "provider": head["provider"],
            "provider_hash": head["provider_hash"],
            "as_of": head["as_of"].isoformat(),
            "events": [
                {
                    "event": Ref.parse(row["event"]).model_dump(mode="json"),
                    "spec_hash": row["spec_hash"],
                    "event_time": row["event_time"].isoformat(),
                    "attributes": json.loads(row["attributes_json"]),
                    "input_ids": list(row["input_ids"]),
                    "upstream_event_ids": list(row["upstream_event_ids"]),
                    "event_id": row["event_id"],
                    **({} if row["subject"] is None else {"subject": row["subject"]}),
                }
                for row in rows
            ],
            "result_hash": head["result_hash"],
            **({} if head["subject"] is None else {"subject": head["subject"]}),
        }
        result = EventResult.model_validate_json(json.dumps(document))
    except (ValidationError, ValueError, TypeError) as exc:
        raise EventTableCorrupted(f"event run {result_hash} does not rebuild") from exc
    if event_table(result) != tuple(_logical_row(row) for row in rows):
        raise EventTableCorrupted(f"event run {result_hash}: columns disagree with the rebuild")
    return result


@dataclass(frozen=True, slots=True)
class EventRunWrite:
    """What ``EventTable.write`` did for one run."""

    result_hash: str
    row_count: int
    #: The snapshot holding the run; ``None`` for an empty run (nothing written).
    snapshot_id: str | None
    #: ``None`` when nothing was committed now (a replay, or an empty run).
    outcome: CommitOutcome | None

    @property
    def empty(self) -> bool:
        return self.row_count == 0

    @property
    def replayed(self) -> bool:
        return not self.empty and self.outcome is not CommitOutcome.COMMITTED


@dataclass(frozen=True, slots=True)
class StoredEventRun:
    """A verified run read from one snapshot of ``event.events``."""

    snapshot_id: str
    result: EventResult
    rows: tuple[EventTableRow, ...]


class EventTable:
    """The one writer / reader of ``event.events``; ``result_hash`` idempotent, append-only."""

    def __init__(self, adapter: EventCatalog) -> None:
        self._adapter = adapter

    def write(self, result: EventResult) -> EventRunWrite:
        batch = event_rows_batch(result)
        expected: list[dict[str, Any]] = batch.to_pylist()
        result_hash = result.result_hash
        if not expected:
            return EventRunWrite(result_hash, 0, None, None)
        fingerprint = EVENT_EVENTS.fingerprint_rule.fingerprint(batch)
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            head = self._head()
            existing = self._rows(result_hash, head)
            if existing:
                if existing != expected:
                    raise EventTableConflict(
                        f"event run {result_hash} is stored with other content; never overwritten"
                    )
                assert head is not None  # rows were read from it
                return EventRunWrite(result_hash, len(expected), head, None)
            request = CommitRequest(
                table=_TABLE,
                batch_id=event_batch_id(result_hash),
                batch_fingerprint=fingerprint,
                row_count=len(expected),
                expected_parent_snapshot_id=head,
            )
            try:
                committed = self._adapter.commit_batch(request, batch)
            except CommitConflict as exc:
                last = exc  # another batch landed first: re-read, then retry
                continue
            except BatchConflict as exc:
                raise EventTableConflict(
                    f"event batch {request.batch_id} is committed with other content"
                ) from exc
            snapshot_id = committed.snapshot.snapshot_id
            if self._rows(result_hash, snapshot_id) != expected:
                raise EventTableCorrupted(f"event run {result_hash} reads back differently")
            return EventRunWrite(result_hash, len(expected), snapshot_id, committed.outcome)
        raise EventTableConflict(f"event run {result_hash} lost {_ATTEMPTS} races") from last

    def read(self, result_hash: str, *, snapshot_id: str | None = None) -> StoredEventRun | None:
        """The verified run at ``snapshot_id`` (default: the head, resolved once) or ``None``."""
        pinned = self._head() if snapshot_id is None else snapshot_id
        rows = self._rows(result_hash, pinned)
        if not rows:
            return None
        assert pinned is not None  # rows were read from it
        result = _rebuild(result_hash, rows)
        return StoredEventRun(pinned, result, tuple(_logical_row(row) for row in rows))

    def load(self, result_hash: str, *, snapshot_id: str | None = None) -> EventResult | None:
        stored = self.read(result_hash, snapshot_id=snapshot_id)
        return None if stored is None else stored.result

    def _rows(self, result_hash: str, snapshot_id: str | None) -> list[dict[str, Any]]:
        if snapshot_id is None:
            return []  # the table has no snapshot yet
        rows: list[dict[str, Any]] = self._adapter.scan_columns(
            _TABLE,
            columns=_COLUMNS,
            row_filter=EqualTo("result_hash", result_hash),  # type: ignore[call-arg, arg-type]
            snapshot_id=snapshot_id,
        ).to_pylist()
        return sorted(rows, key=lambda row: (row["event_index"], row["event_id"]))

    def _head(self) -> str | None:
        info = self._adapter.load_table(_TABLE)
        if info is None:
            raise TableNotFound(f"table {_TABLE} does not exist; call ensure_event_tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id
