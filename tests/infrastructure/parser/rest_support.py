"""Builders for D3C decoder tests: synthetic REST pages, decode requests and outcome asserts.

Every payload here is hand-built synthetic JSON shaped like the official responses (evidence R3 /
R8). No fixture is downloaded and no market-data file is committed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from infrastructure.parser.binance_rest import (
    RestPageDecoded,
    RestPageDecodeRequest,
    RestPageRejection,
    RestPageSummary,
    RestRejectionCode,
)
from infrastructure.revision.rest_identity import PAGE_LIMIT, RestPageQuery

SYMBOL = "BTCUSDT"
MINUTE_MS = 60_000
#: A minute-aligned epoch millisecond (2023-11-14T22:14:00Z); every window here derives from it.
T0 = 1_700_000_040_000
#: Wide enough that a full 1000-item page never reaches it on its own.
TARGET_END = T0 + 5_000 * MINUTE_MS
RETRIEVED_AT_MS = T0 + 10_000 * MINUTE_MS
MAX_BODY = 8_388_608

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def at_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


RETRIEVED_AT = at_ms(RETRIEVED_AT_MS)


# --------------------------------------------------------------------------- payloads


def agg_item(
    agg_id: int,
    timestamp_ms: int,
    *,
    first: int | None = None,
    last: int | None = None,
    price: Any = "92792.05000000",
    quantity: Any = "0.00150000",
    buyer_maker: Any = True,
    best_match: Any = True,
) -> dict[str, Any]:
    """One aggTrades element; trade-id ranges default to non-overlapping ``[a*10, a*10+2]``."""
    return {
        "a": agg_id,
        "p": price,
        "q": quantity,
        "f": agg_id * 10 if first is None else first,
        "l": agg_id * 10 + 2 if last is None else last,
        "T": timestamp_ms,
        "m": buyer_maker,
        "M": best_match,
    }


def agg_items(
    count: int, *, first_id: int = 500, first_ms: int = T0, id_step: int = 1, ms_step: int = 1
) -> list[dict[str, Any]]:
    return [
        agg_item(first_id + index * id_step, first_ms + index * ms_step) for index in range(count)
    ]


def kline_item(
    open_ms: int,
    *,
    close_ms: int | None = None,
    open_: Any = "92792.05000000",
    high: Any = "92832.25000000",
    low: Any = "92782.12000000",
    close: Any = "92782.13000000",
    volume: Any = "6.45789000",
    quote_volume: Any = "599298.29174060",
    trades: Any = 1660,
    taker_base: Any = "4.36611000",
    taker_quote: Any = "405183.17952120",
    ignore: Any = "0",
) -> list[Any]:
    """One 12-slot klines element (evidence R8)."""
    return [
        open_ms,
        open_,
        high,
        low,
        close,
        volume,
        open_ms + MINUTE_MS - 1 if close_ms is None else close_ms,
        quote_volume,
        trades,
        taker_base,
        taker_quote,
        ignore,
    ]


def kline_items(count: int, *, first_ms: int = T0, minute_step: int = 1) -> list[list[Any]]:
    return [kline_item(first_ms + index * minute_step * MINUTE_MS) for index in range(count)]


def body(items: Any) -> bytes:
    """Compact JSON bytes, exactly as the exchange delivers them."""
    return json.dumps(items, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- requests


def agg_request(
    *,
    query: RestPageQuery | None = None,
    retrieved_at: datetime = RETRIEVED_AT,
    target_start_ms: int = T0,
    target_end_ms: int = TARGET_END,
    max_body_bytes: int = MAX_BODY,
    page_index: int = 0,
    previous: RestPageSummary | None = None,
) -> RestPageDecodeRequest:
    return RestPageDecodeRequest(
        query=query or RestPageQuery.agg_trades_from_start(SYMBOL, target_start_ms),
        retrieved_at=retrieved_at,
        target_start_ms=target_start_ms,
        target_end_ms=target_end_ms,
        max_body_bytes=max_body_bytes,
        page_index=page_index,
        previous=previous,
    )


def kline_request(
    *,
    query: RestPageQuery | None = None,
    retrieved_at: datetime = RETRIEVED_AT,
    target_start_ms: int = T0,
    target_end_ms: int = TARGET_END,
    max_body_bytes: int = MAX_BODY,
    page_index: int = 0,
    previous: RestPageSummary | None = None,
) -> RestPageDecodeRequest:
    return RestPageDecodeRequest(
        query=query or RestPageQuery.klines_from_start(SYMBOL, target_start_ms),
        retrieved_at=retrieved_at,
        target_start_ms=target_start_ms,
        target_end_ms=target_end_ms,
        max_body_bytes=max_body_bytes,
        page_index=page_index,
        previous=previous,
    )


def continuation(previous: RestPageSummary, **changes: Any) -> RestPageDecodeRequest:
    """The only legal follow-up request for ``previous``: its exact next query."""
    if previous.next_query is None:
        raise AssertionError("the previous page stopped the chain")
    builder = agg_request if previous.data_type == "agg_trades" else kline_request
    arguments: dict[str, Any] = {
        "query": previous.next_query,
        "retrieved_at": previous.retrieved_at,
        "target_start_ms": previous.target_start_ms,
        "target_end_ms": previous.target_end_ms,
        "page_index": previous.page_index + 1,
        "previous": previous,
    }
    arguments.update(changes)
    return builder(**arguments)


# --------------------------------------------------------------------------- assertions


def decoded(outcome: object) -> RestPageDecoded:
    if isinstance(outcome, RestPageRejection):
        raise AssertionError(f"expected an accepted page, got {outcome.code.value}")
    if not isinstance(outcome, RestPageDecoded):
        raise AssertionError(f"expected a decoded page, got {outcome!r}")
    return outcome


def rejected(outcome: object, code: RestRejectionCode | None = None) -> RestPageRejection:
    if not isinstance(outcome, RestPageRejection):
        raise AssertionError(f"expected a rejection, got {outcome!r}")
    if outcome.elements != ():
        raise AssertionError("a rejection released elements")
    if code is not None and outcome.code is not code:
        raise AssertionError(f"expected {code.value}, got {outcome.code.value}: {outcome.detail}")
    return outcome


def full_page_agg() -> list[dict[str, Any]]:
    """Exactly ``limit`` valid aggTrades that stay inside the target window."""
    return agg_items(PAGE_LIMIT)


def full_page_klines() -> list[list[Any]]:
    """Exactly ``limit`` valid closed 1m klines starting at ``T0``."""
    return kline_items(PAGE_LIMIT)
