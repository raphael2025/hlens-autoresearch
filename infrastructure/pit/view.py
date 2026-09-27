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

from collections.abc import Callable, Mapping, Sequence

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import AlwaysFalse, AlwaysTrue, BooleanExpression

from core.contracts.catalog import (
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    TableInfo,
)
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
