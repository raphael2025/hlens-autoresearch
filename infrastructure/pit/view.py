"""A read-only catalog view pinned to a manifest's snapshot bindings (Phase 1 F1).

Every read of a bound table sees exactly its bound snapshot: ``load_table`` reports it as the
current snapshot (history walks start there), ``scan_columns`` time-travels to it. The shared
verifiers (``PersistedRowVerifier``, the reconciler's edge re-derivation, the normalizer's unit
verification) therefore run unchanged on the view and prove what those snapshots held; the heads
never move, so their "pinned read" converges on the first attempt.

A table the bindings do not name reads as **empty** (no snapshot, no rows). That can only remove
information: a missing Canonical revision, edge or Raw row makes a key absent, a conflict, or a
verification failure — never a different selection — so an unbound table cannot make a build
choose wrongly. The selector additionally requires the Canonical table itself to be bound.
Writes are refused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import cast

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import AlwaysFalse, AlwaysTrue, BooleanExpression

from core.contracts.catalog import (
    BatchRejected,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    TableInfo,
    TableNotFound,
)
from infrastructure.revision.row_integrity import history_from
from infrastructure.revision.store import RevisionCatalog

__all__ = ["PinnedCatalogView", "PinnedViewError"]


class PinnedViewError(Exception):
    """The view was asked to do something a pinned, read-only view cannot do."""


class PinnedCatalogView:
    """``RevisionCatalog`` reads at fixed snapshots; no commits, no allocation."""

    def __init__(self, adapter: RevisionCatalog, bindings: Mapping[str, str]) -> None:
        self._adapter = adapter
        self._bindings = dict(bindings)

    @property
    def bindings(self) -> Mapping[str, str]:
        return dict(self._bindings)

    def load_table(self, table: str) -> TableInfo | None:
        info = self._adapter.load_table(table)
        if info is None:
            return None
        bound = self._bindings.get(table)
        snapshot = None if bound is None else self._adapter.get_snapshot(table, bound)
        return info.model_copy(update={"current_snapshot": snapshot})

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        return self._adapter.get_snapshot(table, snapshot_id)

    def history(self, table: str, snapshot_id: str) -> Iterator[SnapshotInfo]:
        """An explicit snapshot's ancestry, whatever the bindings say (like ``get_snapshot``).

        Delegates to the underlying catalog's walk, so a view over ``PyIcebergCatalogAdapter``
        keeps its one-metadata-load history instead of one ``get_snapshot`` load per step.
        """
        yield from history_from(self._adapter, table, snapshot_id)

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> pa.Table:
        if snapshot_id is not None:
            # An explicit historical snapshot (e.g. an edge batch's own commit): immutable, so
            # the read is reproducible whatever the bindings say.
            return self._adapter.scan_columns(
                table, columns=columns, row_filter=row_filter, limit=limit, snapshot_id=snapshot_id
            )
        bound = self._bindings.get(table)
        if bound is None:
            # Unbound = empty, with the table's real column types.
            return self._adapter.scan_columns(table, columns=columns, row_filter=AlwaysFalse())
        return self._adapter.scan_columns(
            table, columns=columns, row_filter=row_filter, limit=limit, snapshot_id=bound
        )

    def scan_column_batches(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        snapshot_id: str | None = None,
    ) -> Iterator[pa.RecordBatch]:
        """Stream selected columns at an explicit or bound snapshot.

        This optional capability is delegated only when the underlying adapter implements it.
        The reader/iterator is returned unchanged so its ownership, close behavior, and lifetime
        remain governed by the adapter. An unbound table uses the same always-false projection as
        :meth:`scan_columns`; no full-table read is used as a fallback.
        """
        scan_batches = getattr(self._adapter, "scan_column_batches", None)
        if not callable(scan_batches):
            raise PinnedViewError("the underlying catalog does not support streaming column scans")
        if isinstance(columns, str) or not columns:
            raise BatchRejected("scan_column_batches needs at least one column")

        if snapshot_id is not None:
            # Explicit historical snapshots take precedence over the view's binding.
            return cast(
                Iterator[pa.RecordBatch],
                scan_batches(
                    table, columns=columns, row_filter=row_filter, snapshot_id=snapshot_id
                ),
            )

        bound = self._bindings.get(table)
        if bound is None:
            # Unbound means the pinned view has no rows for this table. Loading metadata only
            # preserves the adapter's unknown-table failure without preflighting every manifest
            # on a moving, unbound current head.
            if self._adapter.load_table(table) is None:
                raise TableNotFound(f"table {table} does not exist")
            return iter(())
        return cast(
            Iterator[pa.RecordBatch],
            scan_batches(table, columns=columns, row_filter=row_filter, snapshot_id=bound),
        )

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult:
        raise PinnedViewError("a pinned catalog view is read-only")

    def max_int64(
        self,
        table: str,
        column: str,
        *,
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        check: Callable[[int], None] | None = None,
    ) -> int | None:
        raise PinnedViewError("a pinned catalog view never allocates")
