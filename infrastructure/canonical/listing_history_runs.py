"""Bounded joins between listing batch history and Raw prefix-root references.

The listing catalog yields history newest-first while a Raw prefix-root run is ordered by
snapshot ordinal. This module externally sorts each side by the Raw snapshot ID and joins them,
then writes the joined batches in ordinal order for exact historical replay.

When a pinned bounded metadata view is supplied, ancestry comes from its capped disk-backed
history index. The legacy fallback still uses ``history_from``. Backend scan planning and total
process RSS remain outside this helper's bound, so this is not an E1-CAP-1 claim.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from core.contracts.catalog import SnapshotInfo
from core.contracts.storage import StorageAdapter
from infrastructure.canonical import listing_rules as lr
from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import CANONICAL_INSTRUMENT_LISTINGS
from infrastructure.revision.row_integrity import history_from
from infrastructure.revision.store import RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = ["listing_history_prefix_run"]


def listing_history_prefix_run(
    catalog: RevisionCatalog,
    listing_head: str | None,
    raw_prefix_roots: RunRef,
    *,
    bounded_metadata: BoundedIcebergMetadata | None = None,
    storage: StorageAdapter,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
) -> RunRef | None:
    """Join every persisted Listing batch to the exact Raw snapshot prefix it names.

    Result records are sorted by ``snapshot_ordinal`` and contain the serialized immutable
    ``SnapshotInfo``, Raw snapshot identity, prefix-root document, and observation count. All
    caller limits are required. Catalog ancestry metadata and backend scan batches retain their
    adapter-defined bounds; this helper bounds its own scratch streams and readers.
    """
    for name, value, minimum in (
        ("capacity", capacity, 1),
        ("merge_fanout", merge_fanout, 2),
        ("max_record_bytes", max_record_bytes, 1),
        ("max_run_object_bytes", max_run_object_bytes, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if not isinstance(limits, RunLimits):
        raise ValueError("limits must be RunLimits")
    if bounded_metadata is not None:
        if bounded_metadata.name != CANONICAL_INSTRUMENT_LISTINGS.table:
            raise CatalogIntegrityError("bounded Listing metadata is pinned to another table")
        current = bounded_metadata.metadata.current_snapshot_id
        pinned_head = None if current is None else str(current)
        if listing_head != pinned_head:
            raise CatalogIntegrityError("Listing head differs from its pinned metadata pointer")

    history_by_raw_id = RunSetBuilder(
        storage,
        key=lambda row: row["raw_snapshot_id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    roots_by_raw_id = RunSetBuilder(
        storage,
        key=lambda row: row["snapshot_id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    joined_by_ordinal = RunSetBuilder(
        storage,
        key=lambda row: row["snapshot_ordinal"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    with history_by_raw_id, roots_by_raw_id, joined_by_ordinal:
        if bounded_metadata is None:
            history_reader = iter(
                history_from(catalog, CANONICAL_INSTRUMENT_LISTINGS.table, listing_head)
            )
        else:
            snapshot_info = getattr(catalog, "_snapshot_info", None)
            if not callable(snapshot_info):
                raise CatalogIntegrityError(
                    "bounded Listing join requires the adapter SnapshotInfo converter"
                )
            raw_history = (
                iter(()) if listing_head is None else bounded_metadata.iter_history(listing_head)
            )

            def pinned_history() -> Iterator[SnapshotInfo]:
                try:
                    for item in raw_history:
                        yield snapshot_info(CANONICAL_INSTRUMENT_LISTINGS.table, item)
                finally:
                    close = getattr(raw_history, "close", None)
                    if callable(close):
                        close()

            history_reader = iter(pinned_history())
        try:
            for snapshot in history_reader:
                raw_snapshot_id = lr.parse_batch_id(snapshot.batch_id)
                if raw_snapshot_id is None:
                    raise CatalogIntegrityError(
                        f"listing snapshot {snapshot.snapshot_id} is not a derivation batch"
                    )
                row = {
                    "raw_snapshot_id": raw_snapshot_id,
                    "snapshot": snapshot.model_dump(mode="python"),
                }
                _check_row(row, max_record_bytes)
                history_by_raw_id.add(row)
        finally:
            close = getattr(history_reader, "close", None)
            if callable(close):
                close()
        history_ref = history_by_raw_id.finish()

        with iter_run(storage, raw_prefix_roots, max_object_bytes=max_run_object_bytes) as roots:
            for root in roots:
                if not isinstance(root, Mapping):
                    raise CatalogIntegrityError("Raw prefix-root run contains a non-mapping")
                snapshot_id = root.get("snapshot_id")
                ordinal = root.get("snapshot_ordinal")
                if not isinstance(snapshot_id, str) or not snapshot_id:
                    raise CatalogIntegrityError("Raw prefix-root row has no snapshot ID")
                if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                    raise CatalogIntegrityError("Raw prefix-root row has invalid ordinal")
                roots_by_raw_id.add(dict(root))
        roots_ref = roots_by_raw_id.finish()
        if roots_ref is None:
            raise CatalogIntegrityError("Raw prefix-root run is empty")

        if history_ref is None:
            return None
        with (
            iter_run(storage, history_ref, max_object_bytes=max_run_object_bytes) as history,
            iter_run(storage, roots_ref, max_object_bytes=max_run_object_bytes) as roots,
        ):
            sentinel = object()
            history_item = next(history, sentinel)
            root_item = next(roots, sentinel)
            previous_history_id: str | None = None
            previous_root_id: str | None = None
            while history_item is not sentinel:
                history_row = _mapping(history_item, "listing history row")
                raw_id = history_row["raw_snapshot_id"]
                if raw_id == previous_history_id:
                    raise CatalogIntegrityError("listing history repeats a Raw batch snapshot ID")
                previous_history_id = raw_id
                while root_item is not sentinel:
                    root_row = _mapping(root_item, "Raw prefix-root row")
                    root_id = root_row["snapshot_id"]
                    if root_id == previous_root_id:
                        raise CatalogIntegrityError("Raw prefix-root run repeats a snapshot ID")
                    previous_root_id = root_id
                    if root_id >= raw_id:
                        break
                    root_item = next(roots, sentinel)
                if root_item is sentinel:
                    raise CatalogIntegrityError(
                        f"listing batch names Raw snapshot {raw_id} absent from its pinned history"
                    )
                root_row = _mapping(root_item, "Raw prefix-root row")
                if root_row["snapshot_id"] != raw_id:
                    raise CatalogIntegrityError(
                        f"listing batch names Raw snapshot {raw_id} absent from its pinned history"
                    )
                snapshot = history_row["snapshot"]
                ordinal = root_row["snapshot_ordinal"]
                if snapshot.get("total_rows") != ordinal + 1:
                    raise CatalogIntegrityError(
                        "listing batch Raw snapshot ordinal disagrees with its total_rows"
                    )
                joined = {
                    "snapshot_ordinal": ordinal,
                    "raw_snapshot_id": raw_id,
                    "snapshot": snapshot,
                    "root": root_row.get("root"),
                    "observation_count": root_row.get("observation_count"),
                    "raw_knowledge_floor": root_row.get("raw_knowledge_floor"),
                }
                _check_row(joined, max_record_bytes)
                joined_by_ordinal.add(joined)
                history_item = next(history, sentinel)
                root_item = next(roots, sentinel)
            while root_item is not sentinel:
                root_row = _mapping(root_item, "Raw prefix-root row")
                root_id = root_row["snapshot_id"]
                if root_id == previous_root_id:
                    raise CatalogIntegrityError("Raw prefix-root run repeats a snapshot ID")
                previous_root_id = root_id
                root_item = next(roots, sentinel)
            result = joined_by_ordinal.finish()
            if result is not None:
                previous_ordinal = -1
                with iter_run(storage, result, max_object_bytes=max_run_object_bytes) as ordered:
                    for item in ordered:
                        ordinal = item["snapshot_ordinal"]
                        if ordinal <= previous_ordinal:
                            raise CatalogIntegrityError(
                                "listing batches do not map to strictly increasing Raw prefixes"
                            )
                        previous_ordinal = ordinal
            return result


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogIntegrityError(f"{label} is not a mapping")
    return value


def _check_row(row: Mapping[str, Any], maximum: int) -> None:
    from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier

    try:
        ExchangeInfoRowVerifier._check_bounded_row(row, maximum)
    except Exception as exc:
        raise CatalogIntegrityError("listing history join row exceeds its byte limit") from exc
