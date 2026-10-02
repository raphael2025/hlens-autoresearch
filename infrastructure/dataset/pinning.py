"""Pin the Point-in-Time spec of one v3 Dataset build to the catalog's current heads (ADR-0101 §5).

Same semantics as the capacity probe's Dataset spec: a fixed set of bound tables (every Raw,
Canonical, precedence, listing and Quality input of both first-slice data types, and never a
Dataset table: a build writes its own tables, which must not move its own inputs), each pinned at
its **current** snapshot, and only when it has one. The knowledge and simulation instants are the
caller's; nothing is read from a clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from core.contracts.revision import PointInTimeSpec
from core.domain.base import FrozenMapping
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_EXCHANGE_INFO,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
    CANONICAL_BARS_1M,
    CANONICAL_INSTRUMENT_LISTINGS,
    CANONICAL_TRADES,
    DATA_QUALITY_REPORT_MANIFESTS,
    DATA_QUALITY_REPORTS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.pit.selector import PIT_BINDING
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe import listing_assumption as backfill

__all__ = ["PINNED_TABLES", "pin_dataset_pit_spec"]

#: The fixed binding set: a table is pinned when it has a current snapshot, skipped otherwise.
PINNED_TABLES: Final[tuple[str, ...]] = (
    BINANCE_SPOT_ARCHIVES.table,
    BINANCE_SPOT_AGG_TRADES.table,
    BINANCE_SPOT_KLINES_1M.table,
    BINANCE_SPOT_REST_RESPONSES.table,
    BINANCE_SPOT_REST_AGG_TRADES.table,
    BINANCE_SPOT_REST_KLINES_1M.table,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
    BINANCE_SPOT_EXCHANGE_INFO.table,
    CANONICAL_TRADES.table,
    CANONICAL_BARS_1M.table,
    CANONICAL_INSTRUMENT_LISTINGS.table,
    DATA_QUALITY_REPORT_MANIFESTS.table,
    DATA_QUALITY_REPORTS.table,
    QUALITY_EVIDENCE_GAPS.table,
)


def _head(adapter: RevisionCatalog, table: str) -> str | None:
    info = adapter.load_table(table)
    if info is None or info.current_snapshot is None:
        return None
    return info.current_snapshot.snapshot_id


def pin_dataset_pit_spec(
    adapter: RevisionCatalog,
    *,
    name: str,
    version: str,
    simulation_time: datetime,
    knowledge_cutoff: datetime,
    listing_assumption: bool,
) -> PointInTimeSpec:
    """A point-in-time spec over ``PINNED_TABLES`` at their current heads.

    ``listing_assumption`` additionally binds the current version of the ADR-0051 listing
    backfill assumption in ``availability_bindings``; it is never bound implicitly.
    """
    availability = [rules.AVAILABILITY_BINDING, EXCHANGE_INFO_AVAILABILITY_BINDING]
    if listing_assumption:
        availability.append(backfill.ASSUMPTION_BINDING)
    bindings = {
        table: head for table in PINNED_TABLES if (head := _head(adapter, table)) is not None
    }
    return PointInTimeSpec(
        name=name,
        version=version,
        simulation_time=simulation_time,
        knowledge_cutoff=knowledge_cutoff,
        snapshot_bindings=FrozenMapping(bindings),
        point_in_time_binding=PIT_BINDING,
        availability_bindings=tuple(availability),
        precedence_bindings=(
            DELIVERY_CHANNEL_BINDING,
            rules.PRECEDENCE_MAP_BINDING,
            lr.LISTING_OBSERVATION_BINDING,
        ),
        parser_bindings=(rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING),
    )
