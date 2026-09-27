"""Builders for the E1 tests: real Raw rows (D2 / D3E), real catalog, the real normalizer.

The Raw inputs come from the accepted D2 archive store and the D3E REST store over the D3D mock
venue; the D-33 edge from the real reconciler. Nothing below fakes the normalizer or its inputs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Final

from core.contracts.revision import (
    PointInTimeStatus,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalNormalizer
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
    CANONICAL_BARS_1M,
    CANONICAL_TRADES,
)
from infrastructure.revision.channel_reconcile import evidence_from_row, revision_record_from_row
from infrastructure.revision.precedence import maximal_heads
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    T0,
    RestHarness,
    StepClock,
    utc,
)

ARCHIVES = BINANCE_SPOT_ARCHIVES
ARCHIVE_AGGS = BINANCE_SPOT_AGG_TRADES
ARCHIVE_KLINES = BINANCE_SPOT_KLINES_1M
RESPONSES = BINANCE_SPOT_REST_RESPONSES
REST_AGGS = BINANCE_SPOT_REST_AGG_TRADES
REST_KLINES = BINANCE_SPOT_REST_KLINES_1M
EVIDENCE = BINANCE_SPOT_PRECEDENCE_EVIDENCE
TRADES = CANONICAL_TRADES
BARS = CANONICAL_BARS_1M
STRIDE: Final = 1 << 32
ARCHIVE_RETRIEVED: Final = utc(2023, 11, 16)
TICK: Final = timedelta(microseconds=1)


def ingest_archive(
    h: RestHarness,
    data_type: str,
    lines: list[str],
    *,
    knowledge: datetime,
    request_id: str = "archive-1",
) -> str:
    """Commit one archive through the real D2 store; returns its archive revision id."""
    outcome = h.ingest_archive(
        data_type,
        lines,
        day=DAY,
        clock=StepClock(start=knowledge),
        retrieved_at=ARCHIVE_RETRIEVED,
        request_id=request_id,
    )
    assert type(outcome).__name__ == "ArchiveIngested", outcome
    revision: str = outcome.archive_revision_id
    return revision


def ingest_rest(
    h: RestHarness,
    data_type: str,
    items: list[Any],
    *,
    knowledge: datetime,
    request_id: str = "req-rest",
    retrieved_ms: int = cs.RETRIEVED_AT_MS,
) -> list[str]:
    """Collect + store one REST chain through D3D / D3E; returns its response revision ids."""
    if data_type == "agg_trades":
        cs.queue_agg_chain(h.venue, SYMBOL, T0, [items])
        request = ss.agg_request(request_id)
    else:
        cs.queue_kline_chain(h.venue, SYMBOL, T0, [items], retrieved_at_ms=retrieved_ms)
        request = ss.kline_request(request_id)
    collected = h.collect(request, start_ms=retrieved_ms)
    assert not isinstance(collected, Exception), collected
    stored = h.store(clock=StepClock(start=knowledge)).ingest_collection(request)
    return [page.response_revision_id for page in stored.pages]


def normalizer(
    h: RestHarness,
    *,
    clock: Callable[[], datetime],
    adapter: Any = None,
    microbatch_rows: int | None = None,
) -> CanonicalNormalizer:
    kwargs: dict[str, Any] = {}
    if microbatch_rows is not None:
        kwargs["microbatch_rows"] = microbatch_rows
    return CanonicalNormalizer(
        h.adapter if adapter is None else adapter, h.storage, clock=clock, **kwargs
    )


def records(h: RestHarness, key: str, table: Any = TRADES) -> list[RevisionRecord]:
    return [revision_record_from_row(row) for row in h.rows(table) if row["observation_key"] == key]


def mapped_edges(h: RestHarness, key: str, table: Any = TRADES) -> list[PrecedenceEvidence]:
    """Every Raw edge of ``key`` mapped onto its two Canonical endpoints (the F1 join)."""
    canonical = [row for row in h.rows(table) if row["observation_key"] == key]
    by_lineage = {
        (row["lineage_raw_table"], row["lineage_raw_revision_id"]): row for row in canonical
    }
    mapped: list[PrecedenceEvidence] = []
    for row in h.rows(EVIDENCE):
        if row["observation_key"] != key:
            continue
        archive = by_lineage.get((row["revision_table"], row["revision_id"]))
        rest = by_lineage.get((row["superseded_table"], row["superseded_revision_id"]))
        if archive is None or rest is None:
            continue  # an endpoint not yet normalized: no Canonical edge in this snapshot
        snapshot = h.head(EVIDENCE.table)
        assert snapshot is not None
        mapped.append(
            rules.map_channel_edge(
                evidence_from_row(row),
                row["edge_id"],
                snapshot,
                revision_record_from_row(archive),
                revision_record_from_row(rest),
            )
        )
    return mapped


def select(
    revisions: list[RevisionRecord], evidence: list[PrecedenceEvidence], cutoff: datetime
) -> tuple[PointInTimeStatus, tuple[str, ...]]:
    """PIT at ``knowledge_cutoff`` with the real contracts and maximal-head helper."""
    visible = [item for item in revisions if item.availability.times.knowledge_time <= cutoff]
    edges = [item for item in evidence if item.knowledge_time <= cutoff]
    RevisionGraph(revisions=tuple(visible), precedence_evidence=tuple(edges))
    heads = maximal_heads(visible, edges) if visible else ()
    if not heads:
        return PointInTimeStatus.ABSENT, ()
    if len(heads) == 1:
        return PointInTimeStatus.SELECTED, heads
    return PointInTimeStatus.CONFLICT, heads
