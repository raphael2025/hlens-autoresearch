"""``DatasetChunkWriter`` over ``research.dataset_selection_chunks`` (B3, ADR-0077 §4 / §4.4).

Commits (or, on replay, proves identical) one fixed-size chunk of Research Dataset rows at a
time, through the frozen ``CatalogAdapter.commit_batch`` idempotency (``(table, batch_id)`` +
independently recomputed fingerprint, ADR-0021 D-01 / ``core/contracts/catalog.py``) plus a
readback comparison over the ADR-0075 bounded ``scan_column_batches`` stream — never by extending
the frozen core ``CatalogAdapter`` Protocol or by walking Iceberg snapshot history (ADR-0077
§6.2.6). The writer holds no state across calls: everything it needs after a restart is read back
from the table, matching ``infrastructure/dataset/builder.py``'s v2 ``_materialize`` pattern.

Working set per call: one chunk's rows (bounded by the caller's ``chunk_rows``), one commit
request/result and one bounded readback of that same chunk — never the selection's row count or
chunk count. Every read streams the fixed snapshot through ``scan_column_batches`` (ADR-0075:
no PyIceberg high-level planner, whose manifest / entry / task lists grow with the table's file
count, i.e. with the chunks of every build) and stops as soon as its verdict is decided.

Integrity, fail closed (``CatalogIntegrityError``):

- a chunk committed with other rows (same ``batch_id``, different fingerprint — ``BatchConflict``
  from the adapter) or whose readback differs from what was just committed / already committed
  ("篡改" — corruption or a byte-level rewrite after commit);
- a hole: the moment a chunk is committed for the first time (not a replay), any row of the same
  ``selection_id`` with a **later** ``chunk_index`` already present is proof that an earlier chunk
  is missing (ADR-0077 §4.4: "第一个缺失 chunk 之后不得存在已提交 chunk");
- ``seal``: any row of the ``selection_id`` at or beyond the declared ``chunk_count`` (an extra or
  duplicate chunk batch, per the ``DatasetChunkWriter.seal`` contract in ``builder.py``).

Concurrency follows ADR-0023 §7 (single writer per table, optimistic ``expected_parent_snapshot_id``
retried against the same ``batch_id``); other selections' chunk batches may interleave in the same
shared production table — every read here filters by ``selection_id``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import (
    And,
    BooleanExpression,
    EqualTo,
    GreaterThan,
    GreaterThanOrEqual,
)
from pyiceberg.schema import assign_fresh_schema_ids

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    TableNotFound,
)
from core.contracts.universe import DatasetChunkProof, dataset_chunk_batch_id
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_SELECTION_CHUNKS
from infrastructure.dataset.builder import ChunkCommitted, DatasetSpecError
from infrastructure.dataset.selection import SELECTION_NAMESPACE
from infrastructure.revision.store import RevisionCatalog

__all__ = ["IcebergChunkWriter"]

#: Optimistic-concurrency retries against a moving ``expected_parent_snapshot_id`` (as
#: ``DatasetBuilder._materialize``'s v2 ``_ATTEMPTS``): another writer of a *different* selection's
#: chunk may commit to the same shared table between our head read and our commit attempt.
_ATTEMPTS: Final = 8


def _check_chunk_table(definition: RegisteredTableDefinition) -> None:
    if not isinstance(definition, RegisteredTableDefinition):
        raise DatasetSpecError("chunk_table must be a RegisteredTableDefinition")
    if definition.table.split(".", 1)[0] != SELECTION_NAMESPACE:
        raise DatasetSpecError(f"the chunk table must be in the {SELECTION_NAMESPACE} namespace")
    fresh = assign_fresh_schema_ids(DATASET_SELECTION_CHUNKS.schema)
    if definition.schema.model_dump_json() != fresh.model_dump_json():
        raise DatasetSpecError(
            "the chunk table must have exactly the DATASET_SELECTION_CHUNKS schema"
        )
    if definition.fingerprint_rule.rule_id != PYARROW_BATCH_FINGERPRINT.rule_id:
        raise DatasetSpecError("the chunk table must use the Phase 1 batch fingerprint")


def _ordinal(row: Mapping[str, Any]) -> int:
    return int(row["row_ordinal"])


def _scan_at_most(
    adapter: RevisionCatalog,
    table: str,
    *,
    columns: tuple[str, ...],
    row_filter: BooleanExpression,
    snapshot_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """At most ``limit`` rows of one fixed snapshot, streamed; the reader is always closed.

    ADR-0075 bounded scan: one data file and one Arrow batch at a time, never the high-level
    planner. Callers pass a ``limit`` one past what a lawful table can hold, so stopping there
    cannot change their verdict.
    """
    rows: list[dict[str, Any]] = []
    reader = adapter.scan_column_batches(
        table, columns=columns, row_filter=row_filter, snapshot_id=snapshot_id
    )
    try:
        for record_batch in reader:
            remaining = limit - len(rows)
            if record_batch.num_rows > remaining:
                record_batch = record_batch.slice(0, remaining)
            rows.extend(record_batch.to_pylist())
            if len(rows) >= limit:
                break
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()
    return rows


class IcebergChunkWriter:
    """Commits Research Dataset v3 chunks to one Iceberg table (ADR-0077 §4; B3).

    ``chunk_table`` must be a ``research.*``-namespaced ``RegisteredTableDefinition`` of exactly
    the frozen ``DATASET_SELECTION_CHUNKS`` shape (production callers pass that constant itself;
    tests may bind an equivalently-shaped table under another name).
    """

    def __init__(self, adapter: RevisionCatalog, chunk_table: RegisteredTableDefinition) -> None:
        _check_chunk_table(chunk_table)
        self._adapter = adapter
        self._table = chunk_table

    @property
    def table(self) -> str:
        return self._table.table

    # ------------------------------------------------------------------ commit

    def commit_chunk(
        self, selection_id: str, chunk_index: int, rows: Sequence[Mapping[str, Any]]
    ) -> ChunkCommitted:
        """Commit (or prove identical to) the chunk ``dataset_chunk_batch_id(selection_id,
        chunk_index)``; fail closed on a hole, a tampered readback or a conflicting batch."""
        batch_id = dataset_chunk_batch_id(selection_id, chunk_index)
        if not rows:
            raise CatalogIntegrityError(f"chunk {chunk_index} of {selection_id} has no rows")
        table = self._table.table
        # The fingerprint and the committed batch are of the rows in row_ordinal order (ADR-0077
        # §4.2: "行按 row_ordinal 升序"), never whatever order the caller happened to pass.
        ordered = sorted((dict(row) for row in rows), key=_ordinal)
        batch = pa.Table.from_pylist(ordered, schema=self._table.arrow_schema)
        expected = batch.to_pylist()
        first = expected[0]["row_ordinal"]
        for offset, row in enumerate(expected):
            if row["row_ordinal"] != first + offset:
                raise CatalogIntegrityError(
                    f"chunk {chunk_index} of {selection_id} rows are not contiguous"
                )
            if row["chunk_index"] != chunk_index:
                raise CatalogIntegrityError(
                    f"chunk {chunk_index} of {selection_id} carries another chunk_index"
                )
            if row["selection_id"] != selection_id:
                raise CatalogIntegrityError(
                    f"chunk {chunk_index} of {selection_id} carries another selection_id"
                )
        fingerprint = self._table.fingerprint_rule.fingerprint(batch)

        result = None
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            request = CommitRequest(
                table=table,
                batch_id=batch_id,
                batch_fingerprint=fingerprint,
                row_count=len(expected),
                expected_parent_snapshot_id=self._head(table),
            )
            try:
                result = self._adapter.commit_batch(request, batch)
            except CommitConflict as exc:
                last = exc
                continue
            except BatchConflict as exc:
                raise CatalogIntegrityError(f"{batch_id} is committed with other rows") from exc
            break
        if result is None:
            raise CatalogIntegrityError(
                f"chunk {chunk_index} of {selection_id} lost {_ATTEMPTS} races"
            ) from last

        snapshot = result.snapshot.snapshot_id
        # One row past the chunk is enough to prove an extra row; no more is ever held.
        found = _scan_at_most(
            self._adapter,
            table,
            columns=tuple(field.name for field in self._table.arrow_schema),
            row_filter=And(
                EqualTo("selection_id", selection_id),  # type: ignore[call-arg, arg-type]
                EqualTo("chunk_index", chunk_index),  # type: ignore[call-arg, arg-type]
            ),
            snapshot_id=snapshot,
            limit=len(expected) + 1,
        )
        if sorted(found, key=_ordinal) != expected:
            raise CatalogIntegrityError(
                f"chunk {chunk_index} of {selection_id} reads back differently at {snapshot}"
            )

        replayed = result.outcome is CommitOutcome.ALREADY_COMMITTED
        if not replayed:
            # A hole: some later chunk of this selection is already committed even though this
            # one had never been (ADR-0077 §4.4) — proof of tampering or a bypassed writer.
            ahead = _scan_at_most(
                self._adapter,
                table,
                columns=("chunk_index",),
                row_filter=And(
                    EqualTo("selection_id", selection_id),  # type: ignore[call-arg, arg-type]
                    GreaterThan("chunk_index", chunk_index),  # type: ignore[call-arg, arg-type]
                ),
                snapshot_id=snapshot,
                limit=1,
            )
            if ahead:
                raise CatalogIntegrityError(
                    f"{selection_id} has a chunk beyond {chunk_index} already committed: a hole"
                )

        proof = DatasetChunkProof(
            chunk_index=chunk_index,
            batch_id=batch_id,
            snapshot_id=snapshot,
            first_row_ordinal=first,
            row_count=len(expected),
            batch_fingerprint=fingerprint,
        )
        return ChunkCommitted(proof=proof, replayed=replayed)

    # ------------------------------------------------------------------ seal

    def seal(self, selection_id: str, chunk_count: int) -> None:
        """Prove no chunk ``>= chunk_count`` of ``selection_id`` exists (``builder.py``'s
        ``DatasetChunkWriter.seal`` contract)."""
        if isinstance(chunk_count, bool) or not isinstance(chunk_count, int) or chunk_count < 1:
            raise CatalogIntegrityError(f"chunk_count must be a positive int, got {chunk_count!r}")
        table = self._table.table
        snapshot = self._head(table)
        if snapshot is None:
            raise CatalogIntegrityError(f"{selection_id} has no committed chunk to seal")
        extra = _scan_at_most(
            self._adapter,
            table,
            columns=("chunk_index",),
            row_filter=And(
                EqualTo("selection_id", selection_id),  # type: ignore[call-arg, arg-type]
                GreaterThanOrEqual("chunk_index", chunk_count),  # type: ignore[call-arg, arg-type]
            ),
            snapshot_id=snapshot,
            limit=1,
        )
        if extra:
            raise CatalogIntegrityError(
                f"{selection_id} has a chunk at or beyond chunk_count {chunk_count}"
            )

    # ------------------------------------------------------------------ helpers

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id
