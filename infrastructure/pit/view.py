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
from typing import Any, cast

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

    def __init__(
        self,
        adapter: RevisionCatalog,
        bindings: Mapping[str, str | None],
        *,
        bounded_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._adapter = adapter
        self._bindings = dict(bindings)
        self._bounded_metadata = dict(bounded_metadata or {})

    @property
    def bindings(self) -> Mapping[str, str | None]:
        return dict(self._bindings)

    def load_table(self, table: str) -> TableInfo | None:
        bounded = self._bounded_metadata.get(table)
        if bounded is not None:
            info = getattr(self._adapter, "table_info_from_bounded", None)
            if not callable(info):
                raise PinnedViewError("the underlying catalog cannot describe bounded metadata")
            return cast(TableInfo, info(bounded))
        info = self._adapter.load_table(table)
        if info is None:
            return None
        bound = self._bindings.get(table)
        snapshot = None if bound is None else self._adapter.get_snapshot(table, bound)
        return info.model_copy(update={"current_snapshot": snapshot})

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        bounded = self._bounded_metadata.get(table)
        if bounded is not None:
            snapshot = bounded.require_snapshot(snapshot_id)
            return self._snapshot_info(table, snapshot)
        return self._adapter.get_snapshot(table, snapshot_id)

    def history(self, table: str, snapshot_id: str) -> Iterator[SnapshotInfo]:
        """An explicit snapshot's ancestry, whatever the bindings say (like ``get_snapshot``).

        Delegates to the underlying catalog's walk, so a view over ``PyIcebergCatalogAdapter``
        keeps its one-metadata-load history instead of one ``get_snapshot`` load per step.
        """
        bounded = self._bounded_metadata.get(table)
        if bounded is None:
            yield from history_from(self._adapter, table, snapshot_id)
            return
        for snapshot in bounded.iter_history(snapshot_id):
            yield self._snapshot_info(table, snapshot)

    def pin_bounded_metadata(self, table: str, *, storage: Any, limits: Any) -> Any:
        """Pin the current immutable metadata pointer, selecting this view's exact binding.

        The selected ID is resolved by the bounded adapter against that pointer's external
        snapshot index.  An unbound table deliberately selects ``None`` (empty); this bridge
        never substitutes the metadata file's current head.
        """
        pin_at = getattr(self._adapter, "pin_bounded_metadata_at", None)
        if not callable(pin_at):
            raise PinnedViewError("the underlying catalog lacks exact bounded snapshot pinning")
        return pin_at(table, self._bindings.get(table), storage=storage, limits=limits)

    @property
    def supports_bounded_metadata(self) -> bool:
        """Whether this view's underlying adapter provides the bounded pin capability."""
        return callable(getattr(self._adapter, "pin_bounded_metadata_at", None))

    def table_info_from_bounded(self, bounded: Any) -> TableInfo:
        """Bridge selected table metadata through nested pinned views without eager reloads."""
        table = getattr(bounded, "name", None)
        selected = getattr(bounded, "selected_snapshot_id", object())
        if not isinstance(table, str) or self._bindings.get(table) != selected:
            raise PinnedViewError("bounded metadata differs from the PIT binding")
        describe = getattr(self._adapter, "table_info_from_bounded", None)
        if not callable(describe):
            raise PinnedViewError("the underlying catalog cannot describe bounded metadata")
        return cast(TableInfo, describe(bounded))

    def scan_bounded_batches(
        self,
        bounded: Any,
        *,
        snapshot_id: str,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
    ) -> Any:
        """Bridge a handle only when it belongs to this view's exact selected snapshot.

        ``snapshot_id`` may name any historical snapshot indexed by that immutable handle; the
        underlying bounded adapter performs that exact lookup and never substitutes a head.
        """
        table = getattr(bounded, "name", None)
        selected = getattr(bounded, "selected_snapshot_id", object())
        if not isinstance(table, str) or self._bindings.get(table) != selected:
            raise PinnedViewError("bounded metadata differs from the PIT binding")
        scan = getattr(self._adapter, "scan_bounded_batches", None)
        if not callable(scan):
            raise PinnedViewError("the underlying catalog lacks bounded snapshot scans")
        return scan(bounded, snapshot_id=snapshot_id, columns=columns, row_filter=row_filter)

    def scan_bounded_columns(
        self,
        bounded: Any,
        *,
        snapshot_id: str | None,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        limit: int | None = None,
    ) -> pa.Table:
        """Bridge a materializing bounded scan through nested pinned views.

        Accepted only when the handle belongs to this view's exact selected snapshot (an unbound
        table's handle selects ``None``). ``snapshot_id`` may name any historical snapshot the
        immutable handle indexes, or ``None`` for the explicit empty selection; the underlying
        bounded adapter performs that exact lookup and never substitutes a head.
        """
        table = getattr(bounded, "name", None)
        selected = getattr(bounded, "selected_snapshot_id", object())
        if not isinstance(table, str) or self._bindings.get(table) != selected:
            raise PinnedViewError("bounded metadata differs from the PIT binding")
        scan = getattr(self._adapter, "scan_bounded_columns", None)
        if not callable(scan):
            raise PinnedViewError("the underlying catalog lacks bounded table scans")
        return cast(
            pa.Table,
            scan(
                bounded,
                snapshot_id=snapshot_id,
                columns=columns,
                row_filter=row_filter,
                limit=limit,
            ),
        )

    def pin_bounded_metadata_at(
        self, table: str, snapshot_id: str | None, *, storage: Any, limits: Any
    ) -> Any:
        """Explicit internal variant for consumers that pass a PIT binding map directly."""
        if self._bindings.get(table) != snapshot_id:
            raise PinnedViewError("requested snapshot differs from the PIT binding")
        return self.pin_bounded_metadata(table, storage=storage, limits=limits)

    def scan_pinned_batches(
        self,
        bounded: Any,
        *,
        snapshot_id: str,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
    ) -> Any:
        """Stream from a bounded handle only when it matches this view's exact selection."""
        scan = getattr(self._adapter, "scan_bounded_batches", None)
        if not callable(scan):
            scan = getattr(self._adapter, "scan_pinned_batches", None)
        if not callable(scan):
            raise PinnedViewError("the underlying catalog lacks bounded snapshot scans")
        table = getattr(bounded, "name", None)
        if not isinstance(table, str) or self._bindings.get(table) != snapshot_id:
            raise PinnedViewError("scan snapshot differs from the PIT binding")
        if getattr(bounded, "selected_snapshot_id", object()) != snapshot_id:
            raise PinnedViewError("scan snapshot differs from the PIT binding")
        return scan(bounded, snapshot_id=snapshot_id, columns=columns, row_filter=row_filter)

    def _snapshot_info(self, table: str, snapshot: Any) -> SnapshotInfo:
        """Infrastructure bridge used by bounded replay to serialize indexed snapshots."""
        converter = getattr(self._adapter, "_snapshot_info", None)
        if not callable(converter):
            raise PinnedViewError("the underlying catalog lacks bounded SnapshotInfo conversion")
        return cast(SnapshotInfo, converter(table, snapshot))

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> pa.Table:
        bounded = self._bounded_metadata.get(table)
        if snapshot_id is not None:
            # An explicit historical snapshot (e.g. an edge batch's own commit): immutable, so
            # the read is reproducible whatever the bindings say.
            if bounded is not None:
                if self._bindings.get(table) != bounded.selected_snapshot_id:
                    raise PinnedViewError("bounded metadata differs from the PIT binding")
                scan = getattr(self._adapter, "scan_bounded_columns", None)
                if not callable(scan):
                    raise PinnedViewError("the underlying catalog lacks bounded table scans")
                return cast(
                    pa.Table,
                    scan(
                        bounded,
                        snapshot_id=snapshot_id,
                        columns=columns,
                        row_filter=row_filter,
                        limit=limit,
                    ),
                )
            return self._adapter.scan_columns(
                table, columns=columns, row_filter=row_filter, limit=limit, snapshot_id=snapshot_id
            )
        bound = self._bindings.get(table)
        if bound is None:
            # Unbound = empty, with the table's real column types.
            if bounded is not None:
                if bounded.selected_snapshot_id is not None:
                    raise PinnedViewError("bounded metadata differs from the empty PIT binding")
                scan = getattr(self._adapter, "scan_bounded_columns", None)
                if not callable(scan):
                    raise PinnedViewError("the underlying catalog lacks bounded table scans")
                return cast(
                    pa.Table,
                    scan(
                        bounded,
                        snapshot_id=None,
                        columns=columns,
                        row_filter=AlwaysFalse(),
                        limit=limit,
                    ),
                )
            return self._adapter.scan_columns(table, columns=columns, row_filter=AlwaysFalse())
        if bounded is not None:
            if bounded.selected_snapshot_id != bound:
                raise PinnedViewError("bounded metadata differs from the PIT binding")
            scan = getattr(self._adapter, "scan_bounded_columns", None)
            if not callable(scan):
                raise PinnedViewError("the underlying catalog lacks bounded table scans")
            return cast(
                pa.Table,
                scan(
                    bounded,
                    snapshot_id=bound,
                    columns=columns,
                    row_filter=row_filter,
                    limit=limit,
                ),
            )
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
            bounded = self._bounded_metadata.get(table)
            if bounded is not None:
                scan = getattr(self._adapter, "scan_bounded_batches", None)
                if not callable(scan):
                    raise PinnedViewError("the underlying catalog lacks bounded snapshot scans")
                return cast(
                    Iterator[pa.RecordBatch],
                    scan(bounded, snapshot_id=snapshot_id, columns=columns, row_filter=row_filter),
                )
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
            bounded = self._bounded_metadata.get(table)
            if bounded is not None:
                info = getattr(self._adapter, "table_info_from_bounded", None)
                if not callable(info):
                    raise PinnedViewError("the underlying catalog cannot describe bounded metadata")
                info(bounded)
            elif self._adapter.load_table(table) is None:
                raise TableNotFound(f"table {table} does not exist")
            return iter(())
        bounded = self._bounded_metadata.get(table)
        if bounded is not None:
            return cast(
                Iterator[pa.RecordBatch],
                self.scan_pinned_batches(
                    bounded, snapshot_id=bound, columns=columns, row_filter=row_filter
                ),
            )
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
