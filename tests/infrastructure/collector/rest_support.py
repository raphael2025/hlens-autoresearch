"""Builders for the D3D REST collector tests: a programmable mock venue and injected time.

Nothing here touches the network, the wall clock or real sleep. Every response is hand-built
synthetic JSON shaped like the official payloads (evidence R3 / R8); no market data is committed.

The venue is deliberately strict: an answer must be queued for the exact canonical page URL, and
an unexpected request blows the test up. That is what makes "zero network on replay" a real
assertion rather than a hope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from core.contracts.collector import CollectionRequest
from core.contracts.storage import StorageAdapter
from infrastructure.collector.binance_rest import REST_SOURCE, BinanceSpotRestCollector
from infrastructure.revision.rest_identity import PAGE_LIMIT, RestPageQuery
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.parser.rest_support import agg_item, body, kline_item

ORIGIN = "https://market.test"
SYMBOL = "BTCUSDT"
OTHER_SYMBOL = "ETHUSDT"
MINUTE_MS = 60_000
#: 2023-11-14T22:14:00Z, minute-aligned; every window in these tests derives from it.
T0 = 1_700_000_040_000
#: Far enough ahead that a whole page of data is closed and in the past.
RETRIEVED_AT_MS = T0 + 10_000 * MINUTE_MS
MAX_BODY = 8_388_608

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def at_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


# ======================================================================================
# injected time
# ======================================================================================


class StepClock:
    """A UTC wall clock that advances a fixed number of milliseconds per reading."""

    def __init__(self, start_ms: int = RETRIEVED_AT_MS, step_ms: int = 1) -> None:
        self.now_ms = start_ms
        self.step_ms = step_ms
        self.readings = 0

    def __call__(self) -> datetime:
        value = at_ms(self.now_ms)
        self.now_ms += self.step_ms
        self.readings += 1
        return value


class FrozenClock:
    """A clock that never advances; used to prove the collector refuses a stalled clock."""

    def __init__(self, at: int = RETRIEVED_AT_MS) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return at_ms(self.at)


class FakeTime:
    """An injected monotonic clock plus the only sleeper the collector is allowed to use."""

    def __init__(self) -> None:
        self.monotonic_seconds = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError("the collector asked to sleep for a negative duration")
        self.slept.append(seconds)
        self.monotonic_seconds += seconds


# ======================================================================================
# the mock venue
# ======================================================================================


class _FailAfterStream(httpx.SyncByteStream):
    """Delivers ``data`` in chunks, then fails mid-stream after ``fail_after`` bytes."""

    def __init__(self, data: bytes, *, fail_after: int, chunk_size: int = 8) -> None:
        self._data = data
        self._fail_after = fail_after
        self._chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        sent = 0
        while sent < len(self._data):
            if sent >= self._fail_after:
                break
            end = min(sent + self._chunk_size, len(self._data), self._fail_after)
            if end == sent:
                break
            yield self._data[sent:end]
            sent = end
        raise httpx.ReadError("mid-stream read failure")

    def close(self) -> None:
        return None


@dataclass
class Answer:
    """One queued answer for one exact page URL."""

    status: int = 200
    payload: bytes = b"[]"
    headers: dict[str, str] = field(default_factory=dict)
    #: Raise a retryable transport error instead of answering at all.
    transport_error: bool = False
    #: Fail the body stream after this many bytes.
    fail_after: int | None = None
    #: Omit ``Content-Length`` (it is sent by default for a 200).
    omit_content_length: bool = False
    #: Send this literal ``Content-Length`` instead of the true entity length.
    content_length: str | None = None
    chunk_size: int | None = None


@dataclass
class RestVenue:
    """An in-memory market-data venue driven by ``httpx.MockTransport``."""

    origin: str = ORIGIN
    answers: dict[str, list[Answer]] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    #: The exact headers of every request that reached the transport, in order.
    headers_seen: list[dict[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------ queueing

    @staticmethod
    def url_key(query: RestPageQuery) -> str:
        return f"{query.path}?{query.query_string()}"

    def serve(self, query: RestPageQuery, answer: Answer) -> None:
        self.answers.setdefault(self.url_key(query), []).append(answer)

    def serve_items(self, query: RestPageQuery, items: Any, **kwargs: Any) -> None:
        self.serve(query, Answer(payload=body(items), **kwargs))

    def pending(self) -> int:
        return sum(len(queue) for queue in self.answers.values())

    # ------------------------------------------------------------------ transport

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        self.headers_seen.append(dict(request.headers))
        if f"{request.url.scheme}://{request.url.netloc.decode()}" != self.origin:
            raise AssertionError(f"the collector left its origin: {url}")
        key = f"{request.url.path}?{request.url.query.decode()}"
        queue = self.answers.get(key)
        if not queue:
            raise AssertionError(f"unexpected request: {url}")
        answer = queue.pop(0)
        if answer.transport_error:
            raise httpx.ConnectError("simulated connect failure", request=request)
        headers = dict(answer.headers)
        if answer.status == 200:
            if answer.content_length is not None:
                headers.setdefault("content-length", answer.content_length)
            elif not answer.omit_content_length:
                headers.setdefault("content-length", str(len(answer.payload)))
        if answer.fail_after is not None:
            return httpx.Response(
                answer.status,
                stream=_FailAfterStream(answer.payload, fail_after=answer.fail_after),
                headers=headers,
                request=request,
            )
        if answer.status != 200 and not answer.payload:
            return httpx.Response(answer.status, headers=headers, request=request)
        if answer.chunk_size is not None:
            size = answer.chunk_size
            data = answer.payload

            def stream() -> Iterator[bytes]:
                for start in range(0, len(data), size):
                    yield data[start : start + size]

            return httpx.Response(answer.status, content=stream(), headers=headers, request=request)
        return httpx.Response(
            answer.status, content=answer.payload, headers=headers, request=request
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ======================================================================================
# chains
# ======================================================================================


def agg_page(count: int, *, first_id: int, first_ms: int, ms_step: int = 1) -> list[dict[str, Any]]:
    """``count`` contiguous aggTrades; ids and trade-id ranges never overlap across pages."""
    return [
        agg_item(first_id + index, first_ms + index * ms_step, first=(first_id + index) * 10)
        for index in range(count)
    ]


def kline_page(count: int, *, first_ms: int) -> list[list[Any]]:
    return [kline_item(first_ms + index * MINUTE_MS) for index in range(count)]


def queue_agg_chain(
    venue: RestVenue, symbol: str, t0: int, pages: list[list[dict[str, Any]]]
) -> list[RestPageQuery]:
    """Queue an aggTrades chain: page 0 is ``startTime = t0``, later pages ``fromId = last a+1``."""
    query = RestPageQuery.agg_trades_from_start(symbol, t0)
    queries = []
    for items in pages:
        queries.append(query)
        venue.serve_items(query, items)
        if not items:
            break
        query = RestPageQuery.agg_trades_from_id(symbol, int(items[-1]["a"]) + 1)
    return queries


def queue_kline_chain(
    venue: RestVenue,
    symbol: str,
    t0: int,
    pages: list[list[list[Any]]],
    *,
    retrieved_at_ms: int = RETRIEVED_AT_MS,
) -> list[RestPageQuery]:
    """Queue a 1m klines chain: continuation ``startTime = last closed open + 60_000``."""
    query = RestPageQuery.klines_from_start(symbol, t0)
    queries = []
    for items in pages:
        queries.append(query)
        venue.serve_items(query, items)
        closed = [item for item in items if int(item[0]) + MINUTE_MS <= retrieved_at_ms]
        if not closed:
            break
        query = RestPageQuery.klines_from_start(symbol, int(closed[-1][0]) + MINUTE_MS)
    return queries


# ======================================================================================
# assembly
# ======================================================================================


_UNSET: Final = object()


def make_storage(tmp_path: Path, name: str = "warehouse") -> LocalFileStorageAdapter:
    warehouse = tmp_path / name
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def make_collector(
    storage: StorageAdapter,
    venue: RestVenue,
    *,
    base_url: str | object = _UNSET,
    max_retries: int = 2,
    max_pages: int = 200,
    min_interval_ms: int = 250,
    max_retry_after: int = 60,
    max_response_bytes: int = MAX_BODY,
    clock: Callable[[], datetime] | None = None,
    fake_time: FakeTime | None = None,
) -> BinanceSpotRestCollector:
    timing = fake_time or FakeTime()
    return BinanceSpotRestCollector(
        storage,
        market_data_base_url=venue.origin if base_url is _UNSET else str(base_url),
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=max_retries,
        http_user_agent="hlens-d3d-test/0.0.0",
        max_pages_per_collect=max_pages,
        min_request_interval_ms=min_interval_ms,
        max_retry_after_seconds=max_retry_after,
        max_response_bytes=max_response_bytes,
        http_transport=venue.transport(),
        clock=clock or StepClock(),
        monotonic=timing.monotonic,
        sleeper=timing.sleep,
    )


def agg_request(
    *,
    request_id: str = "d3d-agg",
    symbols: tuple[str, ...] = (SYMBOL,),
    start_ms: int = T0,
    end_ms: int = T0 + 5 * MINUTE_MS,
) -> CollectionRequest:
    return CollectionRequest(
        request_id=request_id,
        source=REST_SOURCE,
        data_type="agg_trades",
        symbols=symbols,
        coverage_start=at_ms(start_ms),
        coverage_end=at_ms(end_ms),
    )


def kline_request(
    *,
    request_id: str = "d3d-kline",
    symbols: tuple[str, ...] = (SYMBOL,),
    start_ms: int = T0,
    end_ms: int = T0 + 5 * MINUTE_MS,
) -> CollectionRequest:
    return CollectionRequest(
        request_id=request_id,
        source=REST_SOURCE,
        data_type="klines_1m",
        symbols=symbols,
        coverage_start=at_ms(start_ms),
        coverage_end=at_ms(end_ms),
    )


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


FULL_PAGE = PAGE_LIMIT
