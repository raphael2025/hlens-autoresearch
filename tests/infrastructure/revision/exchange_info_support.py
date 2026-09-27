"""Builders for the E2 tests: a mock exchangeInfo venue, injected time, real catalog and stores.

Nothing here touches the network, the wall clock or real sleep. Every body is hand-built synthetic
JSON shaped like the official ``GET /api/v3/exchangeInfo`` answer (evidence L4); no market data is
committed. The venue is the strict D3D mock: an answer must be queued for the exact canonical URL
and an unexpected request blows the test up, so "zero network on replay" is a real assertion.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from infrastructure.canonical.listings import ListingDeriver
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.collector.binance_exchange_info import (
    BinanceSpotExchangeInfoCollector,
    ExchangeInfoRequest,
    ExchangeInfoSnapshot,
)
from infrastructure.revision.exchange_info_identity import ExchangeInfoQuery
from infrastructure.revision.exchange_info_store import (
    ExchangeInfoSnapshotStore,
    ExchangeInfoStored,
)
from infrastructure.revision.store import RevisionCatalog
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness
from tests.infrastructure.collector.rest_support import Answer, FakeTime, RestVenue
from tests.infrastructure.revision.revision_support import storage_adapter

ORIGIN: Final = "https://market.test"
MS: Final = timedelta(milliseconds=1)
#: Local observation instants of the tests (2026 UTC).
T1: Final = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
T2: Final = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
T3: Final = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
T4: Final = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
#: The store / deriver clock starts after every observation instant above.
KNOWLEDGE: Final = datetime(2026, 10, 1, tzinfo=UTC)
EXCHANGE_INFO: Final = BINANCE_SPOT_EXCHANGE_INFO
LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS
TRADING: Final = {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}


def url_key() -> str:
    query = ExchangeInfoQuery()
    return f"{query.path}?{query.query_string()}"


def symbol_entry(symbol: str, status: str, *, base: str | None = None) -> dict[str, Any]:
    """One ``symbols[]`` object with the official fields (L4), extra fields included."""
    base_asset = base if base is not None else symbol.removesuffix("USDT")
    return {
        "symbol": symbol,
        "status": status,
        "baseAsset": base_asset,
        "baseAssetPrecision": 8,
        "quoteAsset": "USDT",
        "quotePrecision": 8,
        "quoteAssetPrecision": 8,
        "orderTypes": ["LIMIT", "MARKET"],
        "icebergAllowed": True,
        "isSpotTradingAllowed": True,
        "isMarginTradingAllowed": False,
        "filters": [{"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "tickSize": "0.01"}],
        "permissions": [],
        "permissionSets": [["SPOT"]],
        "defaultSelfTradePreventionMode": "EXPIRE_MAKER",
    }


def body(
    statuses: Mapping[str, str | None],
    *,
    server_time: int = 1_788_000_000_000,
    bases: dict[str, str] | None = None,
) -> bytes:
    """A synthetic answer: ``None`` omits the symbol (a missing symbol)."""
    entries = [
        symbol_entry(symbol, status, base=(bases or {}).get(symbol))
        for symbol, status in sorted(statuses.items())
        if status is not None
    ]
    document = {
        "timezone": "UTC",
        "serverTime": server_time,
        "rateLimits": [
            {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1}
        ],
        "exchangeFilters": [],
        "symbols": entries,
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


class SetClock:
    """A UTC wall clock the test places before each collection; +1 ms per reading."""

    def __init__(self, start: datetime = T1) -> None:
        self.now = start
        self.readings = 0

    def at(self, retrieved_at: datetime) -> None:
        """The next collection reads ``retrieved_at - 1 ms`` (requested), then ``retrieved_at``."""
        self.now = retrieved_at - MS

    def __call__(self) -> datetime:
        value = self.now
        self.now = value + MS
        self.readings += 1
        return value


class StepClock:
    """The store / deriver clock: ``start``, then ``+ step`` per reading."""

    def __init__(self, start: datetime = KNOWLEDGE, step: timedelta = timedelta(seconds=1)) -> None:
        self.now = start
        self.step = step
        self.readings = 0

    def __call__(self) -> datetime:
        value = self.now
        self.now = value + self.step
        self.readings += 1
        return value


@dataclass
class Harness:
    tmp_path: Path
    catalog: SqliteCatalogHarness
    adapter: PyIcebergCatalogAdapter
    storage: LocalFileStorageAdapter
    venue: RestVenue
    wire_clock: SetClock
    clock: StepClock
    time: FakeTime

    def collector(self, **kwargs: Any) -> BinanceSpotExchangeInfoCollector:
        options: dict[str, Any] = {
            "market_data_base_url": ORIGIN,
            "http_connect_timeout_seconds": 1.0,
            "http_read_timeout_seconds": 1.0,
            "http_max_retries": 2,
            "http_user_agent": "hlens-e2-test/0.0.0",
            "min_request_interval_ms": 50,
            "max_retry_after_seconds": 5,
            "max_response_bytes": 1_048_576,
            "http_transport": self.venue.transport(),
            "clock": self.wire_clock,
            "monotonic": self.time.monotonic,
            "sleeper": self.time.sleep,
        }
        options.update(kwargs)
        return BinanceSpotExchangeInfoCollector(self.storage, **options)

    def serve(self, payload: bytes, **kwargs: Any) -> None:
        self.venue.answers.setdefault(url_key(), []).append(Answer(payload=payload, **kwargs))

    def collect(
        self, request_id: str, statuses: Mapping[str, str | None], at: datetime, **kwargs: Any
    ) -> ExchangeInfoSnapshot:
        self.serve(body(statuses, **kwargs))
        self.wire_clock.at(at)
        with self.collector() as collector:
            return collector.collect(ExchangeInfoRequest(request_id))

    def store(self, adapter: RevisionCatalog | None = None) -> ExchangeInfoSnapshotStore:
        return ExchangeInfoSnapshotStore(
            adapter or self.adapter, self.storage, market_data_base_url=ORIGIN, clock=self.clock
        )

    def ingest(self, request_id: str) -> ExchangeInfoStored:
        with self.store() as store:
            return store.ingest_snapshot(ExchangeInfoRequest(request_id))

    def observe(
        self, request_id: str, statuses: Mapping[str, str | None], at: datetime, **kwargs: Any
    ) -> ExchangeInfoStored:
        self.collect(request_id, statuses, at, **kwargs)
        return self.ingest(request_id)

    def deriver(self, adapter: RevisionCatalog | None = None) -> ListingDeriver:
        return ListingDeriver(
            adapter or self.adapter, self.storage, market_data_base_url=ORIGIN, clock=self.clock
        )

    def rows(self, table: str) -> list[dict[str, Any]]:
        definition = EXCHANGE_INFO if table == EXCHANGE_INFO.table else LISTINGS
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[dict[str, Any]] = self.adapter.scan_columns(table, columns=columns).to_pylist()
        return rows

    def head(self, table: str) -> str | None:
        info = self.adapter.load_table(table)
        assert info is not None
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def reopen(self) -> PyIcebergCatalogAdapter:
        self.adapter = self.catalog.open_adapter()
        return self.adapter


@contextmanager
def harness(tmp_path: Path) -> Iterator[Harness]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    catalog = SqliteCatalogHarness(tmp_path, PHASE1_REGISTRY)
    adapter = catalog.open_adapter()
    ensure_phase1_tables(adapter)
    storage = storage_adapter(tmp_path)
    opened = Harness(
        tmp_path=tmp_path,
        catalog=catalog,
        adapter=adapter,
        storage=storage,
        venue=RestVenue(origin=ORIGIN),
        wire_clock=SetClock(),
        clock=StepClock(),
        time=FakeTime(),
    )
    try:
        yield opened
    finally:
        storage.close()
        catalog.cleanup()
