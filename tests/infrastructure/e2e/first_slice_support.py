"""Symbol-parametrized ingestion for the G1 first-slice fixture (roadmap #20).

``World.trades`` / ``World.bars`` (``dataset_support.py``) and ``RestHarness.ingest_archive`` /
``canonical_support.ingest_rest`` hard-code ``BTCUSDT`` and a handful of rows. The end-to-end walk
needs a second symbol (ETHUSDT) and a klines run long enough to resample into 5-minute bars, so
this module calls the same underlying building blocks (``revision_support.archive``,
``rest_support.queue_*_chain`` / ``*_request``, ``RestHarness.collect`` / ``store``) with an
explicit symbol instead. Shared by the SQLite and PostgreSQL variants of the G1 test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Final

from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.parser.rest_support import kline_item
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision import revision_support as rs

__all__ = [
    "BTC",
    "DAY_END",
    "DAY_START",
    "ETH",
    "KLINE_COUNT",
    "KLINE_START_MS",
    "ingest_archive_for",
    "ingest_bars_for",
    "ingest_rest_for",
    "ingest_trades_for",
    "klines",
]

BTC: Final = ss.SYMBOL
ETH: Final = cs.OTHER_SYMBOL
DAY_START: Final = ss.utc(2023, 11, 14)
DAY_END: Final = ss.utc(2023, 11, 15)
#: 12 lawful minutes (22:05..22:16): two complete 5-minute buckets, one partial (E4 fixture).
KLINE_COUNT: Final = 12
KLINE_START_MS: Final = ss.T0 - 9 * ss.MINUTE_MS


def klines(count: int, first_ms: int, *, base: str) -> list[list[Any]]:
    """``count`` lawful one-minute klines with varying closes (a non-trivial log return)."""
    base_price = Decimal(base)
    out: list[list[Any]] = []
    for index in range(count):
        opened = base_price + index
        closed = opened + Decimal("0.5") + Decimal(index % 3) / 4
        out.append(
            kline_item(
                first_ms + index * ss.MINUTE_MS,
                open_=f"{opened:.8f}",
                high=f"{closed + 2:.8f}",
                low=f"{opened - 1:.8f}",
                close=f"{closed:.8f}",
                volume=f"{10 + index:.8f}",
                quote_volume="1000.00000000",
                trades=5 + index,
                taker_base="4.00000000",
                taker_quote="400.00000000",
            )
        )
    return out


def ingest_archive_for(
    h: ss.RestHarness,
    data_type: str,
    symbol: str,
    lines: list[str],
    *,
    knowledge: Any,
    request_id: str,
    day: date = ss.DAY,
) -> str:
    """The D2 archive store (real), for an explicit ``symbol`` (D2 / roadmap #20 step 1)."""
    archive = rs.archive(
        h.storage,
        data_type=data_type,
        symbol=symbol,
        day=day,
        rows=lines,
        retrieved_at=c.ARCHIVE_RETRIEVED,
        request_id=request_id,
    )
    outcome: Any = h.archive_store(clock=ss.StepClock(start=knowledge)).ingest(
        archive.collected, archive.context
    )
    assert type(outcome).__name__ == "ArchiveIngested", outcome
    revision: str = outcome.archive_revision_id
    return revision


def ingest_rest_for(
    h: ss.RestHarness,
    data_type: str,
    symbol: str,
    items: Any,
    *,
    knowledge: Any,
    request_id: str,
    start_ms: int,
    end_ms: int,
) -> list[str]:
    """The D3D collector + D3E REST store (real), for an explicit ``symbol`` (roadmap #20)."""
    if data_type == "agg_trades":
        cs.queue_agg_chain(h.venue, symbol, start_ms, [items])
        request = cs.agg_request(
            request_id=request_id, symbols=(symbol,), start_ms=start_ms, end_ms=end_ms
        )
    else:
        cs.queue_kline_chain(h.venue, symbol, start_ms, [items], retrieved_at_ms=cs.RETRIEVED_AT_MS)
        request = cs.kline_request(
            request_id=request_id, symbols=(symbol,), start_ms=start_ms, end_ms=end_ms
        )
    collected = h.collect(request, start_ms=cs.RETRIEVED_AT_MS)
    assert not isinstance(collected, Exception), collected
    stored = h.store(clock=ss.StepClock(start=knowledge)).ingest_collection(request)
    return [page.response_revision_id for page in stored.pages]


def ingest_trades_for(w: ds.World, symbol: str, *, tag: str) -> None:
    """Archive + REST aggTrades, normalized on both channels, reconciled (D-33 edge)."""
    items = ss.agg_items(3, first_id=100, first_ms=ss.T0)
    archive_revision = ingest_archive_for(
        w.h,
        "agg_trades",
        symbol,
        ss.archive_agg_lines(items),
        knowledge=ds.K_A,
        request_id=f"archive-agg-{tag}",
    )
    [response_revision] = ingest_rest_for(
        w.h,
        "agg_trades",
        symbol,
        items,
        knowledge=ds.K_R,
        request_id=f"rest-agg-{tag}",
        start_ms=ss.T0,
        end_ms=ss.T0 + 5 * ss.MINUTE_MS,
    )
    c.normalizer(w.h, clock=ss.StepClock(start=ds.N_A)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive_revision
    )
    c.normalizer(w.h, clock=ss.StepClock(start=ds.N_R)).normalize_unit(
        c.REST_AGGS.table, response_revision
    )
    w.h.reconciler(clock=ss.StepClock(start=ds.K_E)).reconcile("agg_trades", symbol, ss.DAY)


def ingest_bars_for(
    w: ds.World,
    symbol: str,
    *,
    tag: str,
    base: str,
    items: list[list[Any]] | None = None,
    day: date = ss.DAY,
) -> None:
    """Archive + REST 1m klines, normalized on both channels, reconciled (D-33 edge).

    ``items`` (contiguous 1m klines of ``day``, default ``ss.DAY``) replaces the default
    ``KLINE_COUNT`` run.
    """
    if items is None:
        items = klines(KLINE_COUNT, KLINE_START_MS, base=base)
    first_ms: int = items[0][0]
    archive_revision = ingest_archive_for(
        w.h,
        "klines_1m",
        symbol,
        ss.archive_kline_lines(items),
        knowledge=ds.K_A,
        request_id=f"archive-klines-{tag}",
        day=day,
    )
    [response_revision] = ingest_rest_for(
        w.h,
        "klines_1m",
        symbol,
        items,
        knowledge=ds.K_R,
        request_id=f"rest-klines-{tag}",
        start_ms=first_ms,
        end_ms=first_ms + len(items) * ss.MINUTE_MS,
    )
    c.normalizer(w.h, clock=ss.StepClock(start=ds.N_A)).normalize_unit(
        c.ARCHIVE_KLINES.table, archive_revision
    )
    c.normalizer(w.h, clock=ss.StepClock(start=ds.N_R)).normalize_unit(
        c.REST_KLINES.table, response_revision
    )
    w.h.reconciler(clock=ss.StepClock(start=ds.K_E)).reconcile("klines_1m", symbol, day)
