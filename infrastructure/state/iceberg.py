"""Append-only Iceberg persistence for complete StateResult runs (ADR-0089)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, cast

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
from core.contracts.state import (
    StateProviderDescriptor,
    StateRequest,
    StateResult,
)
from core.domain.base import (
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    Kind,
    Ref,
    contract_schema_version_scope,
)
from core.domain.specs import StateSpec
from infrastructure.state.table import state_table
from infrastructure.state.table_definition import STATE_STATES

__all__ = [
    "PHASE2_COLUMNS",
    "StateCatalog",
    "StateRunWrite",
    "StateTable",
    "StateTableConflict",
    "StateTableCorrupted",
    "StateTableError",
    "StateTableRow",
    "StoredStateRun",
    "state_batch_id",
    "state_rows",
    "state_rows_batch",
]

_TABLE: Final = STATE_STATES.table
PHASE2_COLUMNS: Final = tuple(field.name for field in STATE_STATES.arrow_schema)
_RUN_KEYS: Final = (
    "state_ref",
    "spec_hash",
    "provider",
    "request_hash",
    "result_hash",
    "provider_hash",
    "evaluation_count",
    "run_schema_version",
)
_ATTEMPTS: Final = 8


class StateTableError(RuntimeError):
    """Base class for ``state.states`` persistence errors."""


class StateTableConflict(StateTableError):
    """The table already holds different rows under this StateResult identity."""


class StateTableCorrupted(StateTableError):
    """Stored rows fail their run-envelope or StateResult integrity checks."""


class StateCatalog(Protocol):
    """The catalog capability required by this module; the production adapter satisfies it."""

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


def state_batch_id(result_hash: str) -> str:
    return f"state.{result_hash}"


def state_rows(
    result: StateResult,
    spec: StateSpec,
    request: StateRequest,
    descriptor: StateProviderDescriptor,
) -> list[dict[str, Any]]:
    """The physical rows of one checked StateResult (logical projection + run envelope)."""
    return cast(
        list[dict[str, Any]], state_rows_batch(result, spec, request, descriptor).to_pylist()
    )


def state_rows_batch(
    result: StateResult,
    spec: StateSpec,
    request: StateRequest,
    descriptor: StateProviderDescriptor,
) -> pa.Table:
    """A typed Arrow batch for one complete and checked StateResult."""
    if not isinstance(result, StateResult):
        raise TypeError("state rows need a StateResult")
    if not isinstance(spec, StateSpec) or not isinstance(request, StateRequest):
        raise TypeError("state rows need a StateSpec and StateRequest")
    if not isinstance(descriptor, StateProviderDescriptor):
        raise TypeError("state rows need a StateProviderDescriptor")
    try:
        result.check_answers(request, descriptor, spec)
    except ValueError as exc:
        raise StateTableError(f"StateResult does not answer its request: {exc}") from exc
    version = _run_version(result)
    logical = state_table(spec, request, result, descriptor)
    records = logical.to_pylist()
    count = len(records)
    if count == 0:
        raise StateTableError("an empty StateResult cannot be stored (the DTO requires values)")
    for index, row in enumerate(records):
        row.update(
            provider_hash=result.provider_hash,
            evaluation_index=index,
            evaluation_count=count,
            run_schema_version=version,
        )
    return pa.Table.from_pylist(records, schema=STATE_STATES.arrow_schema)


def _run_version(result: StateResult) -> str:
    versions = {result.schema_version, *(item.schema_version for item in result.values)}
    if len(versions) != 1:
        raise StateTableError(
            f"StateResult {result.result_hash} mixes schema versions {sorted(versions)}"
        )
    (version,) = versions
    if version not in PUBLISHED_CONTRACT_SCHEMA_VERSIONS:
        raise StateTableError(
            f"StateResult {result.result_hash} has unpublished schema version {version}"
        )
    return version


@dataclass(frozen=True, slots=True)
class StateTableRow:
    state_ref: str
    spec_hash: str
    provider: str
    request_hash: str
    result_hash: str
    evaluation_time: datetime
    state: str | None
    inputs_used: int
    latest_input_time: datetime | None


def _logical_row(row: dict[str, Any]) -> StateTableRow:
    return StateTableRow(
        state_ref=row["state_ref"],
        spec_hash=row["spec_hash"],
        provider=row["provider"],
        request_hash=row["request_hash"],
        result_hash=row["result_hash"],
        evaluation_time=row["evaluation_time"],
        state=row["state"],
        inputs_used=row["inputs_used"],
        latest_input_time=row["latest_input_time"],
    )


def _rebuild(result_hash: str, rows: list[dict[str, Any]]) -> StateResult:
    if not rows:
        raise StateTableCorrupted(f"state run {result_hash} has no rows")
    head = rows[0]
    if any(row[key] != head[key] for row in rows for key in _RUN_KEYS):
        raise StateTableCorrupted(f"state run {result_hash}: rows disagree on the run envelope")
    if head["result_hash"] != result_hash:
        raise StateTableCorrupted(f"state run {result_hash}: rows claim another run")
    if [row["evaluation_index"] for row in rows] != list(range(len(rows))):
        raise StateTableCorrupted(f"state run {result_hash}: evaluation_index is not 0..n-1")
    if head["evaluation_count"] != len(rows):
        raise StateTableCorrupted(
            f"state run {result_hash}: {len(rows)} rows but evaluation_count "
            f"{head['evaluation_count']}"
        )
    try:
        state_ref = Ref.parse(head["state_ref"])
        if state_ref.kind is not Kind.STATE:
            raise ValueError("state_ref is not a state reference")
        version = head["run_schema_version"]
        value_documents = [
            {
                "schema_version": version,
                "evaluation_time": row["evaluation_time"].isoformat(),
                "state": row["state"],
                "inputs_used": row["inputs_used"],
                **(
                    {}
                    if row["latest_input_time"] is None
                    else {"latest_input_time": row["latest_input_time"].isoformat()}
                ),
            }
            for row in rows
        ]
        document = {
            "schema_version": version,
            "request_hash": head["request_hash"],
            "provider": head["provider"],
            "provider_hash": head["provider_hash"],
            "values": value_documents,
            "result_hash": head["result_hash"],
        }
        with contract_schema_version_scope(version):
            result = StateResult.model_validate_json(json.dumps(document))
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateTableCorrupted(f"state run {result_hash} does not rebuild") from exc
    if _run_version(result) != version:  # pragma: no cover - explicit recorded version above
        raise StateTableCorrupted(f"state run {result_hash}: rebuilt at another version")
    if result.result_hash != result_hash:
        raise StateTableCorrupted(f"state run {result_hash}: StateResult hash mismatch")
    if any(
        row["provider"] != result.provider or row["request_hash"] != result.request_hash
        for row in rows
    ):
        raise StateTableCorrupted(f"state run {result_hash}: logical identity differs on rebuild")
    return result


@dataclass(frozen=True, slots=True)
class StateRunWrite:
    result_hash: str
    row_count: int
    snapshot_id: str
    outcome: CommitOutcome | None

    @property
    def replayed(self) -> bool:
        return self.outcome is None


@dataclass(frozen=True, slots=True)
class StoredStateRun:
    snapshot_id: str
    result: StateResult
    rows: tuple[StateTableRow, ...]


class StateTable:
    """The append-only writer and pinned-snapshot reader of ``state.states``."""

    def __init__(self, adapter: StateCatalog) -> None:
        self._adapter = adapter

    def write(
        self,
        spec: StateSpec,
        request: StateRequest,
        result: StateResult,
        descriptor: StateProviderDescriptor,
    ) -> StateRunWrite:
        batch = state_rows_batch(result, spec, request, descriptor)
        expected = batch.to_pylist()
        result_hash = result.result_hash
        fingerprint = STATE_STATES.fingerprint_rule.fingerprint(batch)
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            head = self._head()
            existing = self._rows(result_hash, head)
            if existing:
                if existing != expected:
                    raise StateTableConflict(
                        f"state run {result_hash} is stored with other content; never overwritten"
                    )
                assert head is not None
                return StateRunWrite(result_hash, len(expected), head, None)
            commit = CommitRequest(
                table=_TABLE,
                batch_id=state_batch_id(result_hash),
                batch_fingerprint=fingerprint,
                row_count=len(expected),
                expected_parent_snapshot_id=head,
            )
            try:
                committed = self._adapter.commit_batch(commit, batch)
            except CommitConflict as exc:
                last = exc
                continue
            except BatchConflict as exc:
                raise StateTableConflict(
                    f"state batch {commit.batch_id} is committed with other content"
                ) from exc
            snapshot = committed.snapshot.snapshot_id
            if self._rows(result_hash, snapshot) != expected:
                raise StateTableCorrupted(f"state run {result_hash} reads back differently")
            return StateRunWrite(result_hash, len(expected), snapshot, committed.outcome)
        raise StateTableConflict(f"state run {result_hash} lost {_ATTEMPTS} races") from last

    def read(
        self,
        result_hash: str,
        *,
        expected_spec: StateSpec | None = None,
        snapshot_id: str | None = None,
    ) -> StoredStateRun | None:
        """Read a verified run, optionally binding its stored State identity to ``expected_spec``.

        ``state_ref`` and ``spec_hash`` are not included in ``StateResult.result_hash``. Callers
        that need those stored columns authenticated must supply the expected spec; omitting it
        preserves the original read API and verifies the StateResult payload only.
        """
        if expected_spec is not None and not isinstance(expected_spec, StateSpec):
            raise TypeError("expected_spec must be a StateSpec")
        pinned = self._head() if snapshot_id is None else snapshot_id
        rows = self._rows(result_hash, pinned)
        if not rows:
            return None
        assert pinned is not None
        if expected_spec is not None:
            expected_ref = str(expected_spec.ref)
            expected_hash = expected_spec.content_hash()
            if any(
                row["state_ref"] != expected_ref or row["spec_hash"] != expected_hash
                for row in rows
            ):
                raise StateTableCorrupted(
                    f"state run {result_hash}: stored identity differs from expected StateSpec"
                )
        result = _rebuild(result_hash, rows)
        return StoredStateRun(pinned, result, tuple(_logical_row(row) for row in rows))

    def load(
        self,
        result_hash: str,
        *,
        expected_spec: StateSpec | None = None,
        snapshot_id: str | None = None,
    ) -> StateResult | None:
        """Load the StateResult, optionally checking its row identity against ``expected_spec``."""
        stored = self.read(result_hash, expected_spec=expected_spec, snapshot_id=snapshot_id)
        return None if stored is None else stored.result

    def _rows(self, result_hash: str, snapshot_id: str | None) -> list[dict[str, Any]]:
        if snapshot_id is None:
            return []
        rows = self._adapter.scan_columns(
            _TABLE,
            columns=PHASE2_COLUMNS,
            row_filter=EqualTo("result_hash", result_hash),  # type: ignore[call-arg, arg-type]
            snapshot_id=snapshot_id,
        ).to_pylist()
        return sorted(rows, key=lambda row: row["evaluation_index"])

    def _head(self) -> str | None:
        info = self._adapter.load_table(_TABLE)
        if info is None:
            raise TableNotFound(f"table {_TABLE} does not exist; call ensure_state_tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id
