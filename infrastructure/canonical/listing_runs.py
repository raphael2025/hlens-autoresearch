"""Bounded listing-observation projection runs.

The legacy listing deriver accepts arbitrary row iterables but materializes each symbol's full
observation history. This module turns the already-proven Raw row run into a disk-backed,
deterministically ordered observation run so bounded listing consumers can replay one symbol at a
time without retaining all observations.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.contracts.storage import StorageAdapter
from infrastructure.canonical import listing_rules as lr
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = ["listing_observations_run"]


def _observation_key(row: Mapping[str, Any]) -> tuple[str, Any, str]:
    return (row["venue_symbol"], row["retrieved_at"], row["snapshot_revision_id"])


def listing_observations_run(
    raw_rows: RunRef | None,
    *,
    storage: StorageAdapter,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
) -> RunRef | None:
    """Build the sorted observation run from a verified Raw proof run.

    ``raw_rows`` must be the result of ``ExchangeInfoRowVerifier.verify_table_bounded``. Its
    records bind each row to the actual verified Raw ``snapshot_id`` and ``snapshot_ordinal``.
    Every resource bound is required. The output order is venue symbol, retrieved time, then
    snapshot revision ID (Unicode code-point order for the final text key). Each output record
    retains the verified snapshot membership needed for listing batch replay.
    """
    if raw_rows is None:
        return None
    for name, value, minimum in (
        ("capacity", capacity, 1),
        ("merge_fanout", merge_fanout, 2),
        ("max_record_bytes", max_record_bytes, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if not isinstance(limits, RunLimits):
        raise ValueError("limits must be RunLimits")

    builder = RunSetBuilder(
        storage,
        key=_observation_key,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    with builder, iter_run(storage, raw_rows) as source:
        for item in source:
            if not isinstance(item, Mapping):
                raise CatalogIntegrityError("Raw proof run contains a non-mapping record")
            row = item.get("row")
            if not isinstance(row, Mapping):
                raise CatalogIntegrityError("Raw proof run record has no row mapping")
            snapshot_id = item.get("snapshot_id")
            ordinal = item.get("snapshot_ordinal")
            if not isinstance(snapshot_id, str) or not snapshot_id:
                raise CatalogIntegrityError("Raw proof run record has no snapshot membership")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise CatalogIntegrityError("Raw proof run record has invalid snapshot ordinal")
            requested = row.get("requested_symbols")
            symbols = row.get("symbols")
            if not isinstance(requested, list) or not isinstance(symbols, list):
                raise CatalogIntegrityError("Raw proof row has invalid listing symbol arrays")
            if any(not isinstance(symbol, str) for symbol in requested):
                raise CatalogIntegrityError("Raw proof row has a non-text requested symbol")
            if len(set(requested)) != len(requested):
                raise CatalogIntegrityError("Raw proof row requests a symbol more than once")
            if any(symbol not in lr.FIRST_SLICE_ASSETS for symbol in requested):
                raise CatalogIntegrityError("Raw proof row requests a symbol outside listing scope")
            revision_id = row.get("revision_id")
            if not isinstance(revision_id, str) or not revision_id:
                raise CatalogIntegrityError("Raw proof row has no revision_id")
            for venue_symbol in requested:
                matches = [
                    symbol_row
                    for symbol_row in symbols
                    if isinstance(symbol_row, Mapping) and symbol_row.get("symbol") == venue_symbol
                ]
                if len(matches) > 1:
                    raise CatalogIntegrityError(
                        f"Raw proof row repeats {venue_symbol} in its symbol payload"
                    )
                symbol_row = matches[0] if matches else None
                observation = {
                    "snapshot_id": snapshot_id,
                    "snapshot_ordinal": ordinal,
                    "venue_symbol": venue_symbol,
                    "snapshot_revision_id": revision_id,
                    "requested_at": row["requested_at"],
                    "retrieved_at": row["retrieved_at"],
                    "raw_knowledge_time": row["knowledge_time"],
                    "status": None if symbol_row is None else symbol_row.get("status"),
                    "base_asset": None if symbol_row is None else symbol_row.get("base_asset"),
                    "quote_asset": None if symbol_row is None else symbol_row.get("quote_asset"),
                }
                ExchangeInfoRowVerifier._check_bounded_row(observation, max_record_bytes)
                builder.add(observation)
        return builder.finish()
