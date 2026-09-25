"""Builders for the F2 / F3 tests: one catalog, real stores, real listing history, real reports.

A ``World`` joins the D3E harness (archives, REST pages, normalizer, reconciler over the mock
venue) and the E2 harness (exchangeInfo snapshots, listing derivation) on **one** catalog and
warehouse.
Nothing under test is faked: only transports and clocks are injected.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import DegradedEpisodeKey, UniverseExclusion, UniverseMember
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS, PHASE1_REGISTRY, PHASE1_TABLES
from infrastructure.pit.selector import PIT_BINDING
from infrastructure.quality.listing_report import ListingQualityReporter
from infrastructure.quality.reporter import QualityReporter
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.universe.builder import UniverseBuilder
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    SqliteCatalogHarness,
)
from tests.infrastructure.collector.rest_support import FakeTime, RestVenue
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    RestHarness,
    StepClock,
    utc,
)

ORIGIN: Final = xs.ORIGIN
REGISTRY: Final = PHASE1_REGISTRY

#: Listing observations (exchangeInfo retrieved_at) before the market data of 2023-11-14.
L1: Final = utc(2023, 11, 10)
L2: Final = utc(2023, 11, 12)
L3: Final = utc(2023, 11, 13)
LISTING_CLOCK: Final = utc(2023, 11, 25)
#: Raw / Canonical / edge knowledge (as in the F1 tests) and the reports' clock.
K_A, K_R, N_A, N_R, K_E = (utc(2023, 12, d) for d in (1, 5, 6, 7, 10))
K_Q: Final = utc(2023, 12, 15)
SIM: Final = utc(2023, 12, 20)
#: The event window: two agg_trades hour slices around the 22:14 trades of DAY.
START, END = utc(2023, 11, 14, 21), utc(2023, 11, 14, 23)
TRADING: Final = xs.TRADING
ETH_HALT: Final = {"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}


def episode_of(entry: UniverseMember | UniverseExclusion) -> DegradedEpisodeKey:
    """The first slice has no stable product id: every episode is a degraded key (ADR-0029)."""
    episode = entry.episode
    assert isinstance(episode, DegradedEpisodeKey)
    return episode


def symbol_of(entry: UniverseMember | UniverseExclusion) -> str:
    return episode_of(entry).symbol


@dataclass
class World:
    h: RestHarness
    x: xs.Harness

    # ------------------------------------------------------------------ listings

    def observe(self, request_id: str, statuses: Mapping[str, str | None], at: datetime) -> None:
        self.x.observe(request_id, statuses, at, server_time=int(at.timestamp() * 1000))

    def derive(self) -> Any:
        deriver = self.x.deriver(self.h.adapter)
        try:
            return deriver.derive()
        finally:
            deriver.close()

    def listed(self, statuses: Mapping[str, str | None] = TRADING, at: datetime = L1) -> None:
        self.observe(f"snap-{at.isoformat()}", statuses, at)
        self.derive()

    # ------------------------------------------------------------------ market data

    def trades(self, *, count: int = 3, reconcile: bool = True) -> None:
        """Archive + REST copies of ``count`` BTCUSDT trades at 22:14 of DAY (F1 fixture)."""
        items = ss.agg_items(count)
        archive = c.ingest_archive(self.h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_A)
        [response] = c.ingest_rest(self.h, "agg_trades", items, knowledge=K_R)
        c.normalizer(self.h, clock=StepClock(start=N_A)).normalize_unit(
            c.ARCHIVE_AGGS.table, archive
        )
        c.normalizer(self.h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, response)
        if reconcile:
            self.h.reconciler(clock=StepClock(start=K_E)).reconcile("agg_trades", SYMBOL, DAY)

    def more_trades(self) -> None:
        """One more BTCUSDT trade (id 110) from a new REST page, normalized: heads move."""
        [later] = c.ingest_rest(
            self.h,
            "agg_trades",
            ss.agg_items(1, first_id=110, first_ms=ss.T0 + 10),
            knowledge=K_R,
            request_id="req-110",
        )
        c.normalizer(self.h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, later)

    def bars(self, *, count: int = 3) -> None:
        items = ss.kline_items(count)
        archive = c.ingest_archive(
            self.h, "klines_1m", ss.archive_kline_lines(items), knowledge=K_A
        )
        [response] = c.ingest_rest(self.h, "klines_1m", items, knowledge=K_R)
        c.normalizer(self.h, clock=StepClock(start=N_A)).normalize_unit(
            c.ARCHIVE_KLINES.table, archive
        )
        c.normalizer(self.h, clock=StepClock(start=N_R)).normalize_unit(
            c.REST_KLINES.table, response
        )
        self.h.reconciler(clock=StepClock(start=K_E)).reconcile("klines_1m", SYMBOL, DAY)

    # ------------------------------------------------------------------ reports and specs

    def report(
        self,
        data_type: str = "agg_trades",
        symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
        days: tuple[date, ...] = (DAY,),
        *,
        listing: bool = True,
    ) -> list[str]:
        ids = []
        reporter = QualityReporter(self.h.adapter, self.h.storage, clock=StepClock(start=K_Q))
        for symbol in symbols:
            for day in days:
                ids.append(reporter.report(data_type, symbol, day).report_id)
        if listing:
            ids.append(
                ListingQualityReporter(
                    self.h.adapter,
                    self.h.storage,
                    market_data_base_url=ORIGIN,
                    clock=StepClock(start=K_Q),
                )
                .report()
                .report_id
            )
        return ids

    def bindings(self, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
        """Current heads of every Phase 1 input table with a snapshot (not the manifests)."""
        return {
            table.table: head
            for table in PHASE1_TABLES
            if table.table not in (DATASET_MANIFESTS.table, *skip)
            and (head := self.h.head(table.table)) is not None
        }

    def spec(
        self,
        *,
        at: datetime = SIM,
        cutoff: datetime = SIM,
        interval: tuple[datetime, datetime] | None = None,
        skip: tuple[str, ...] = (),
        **overrides: Any,
    ) -> PointInTimeSpec:
        fields: dict[str, Any] = {
            "name": "test.dataset",
            "version": "1.0.0",
            "knowledge_cutoff": cutoff,
            "snapshot_bindings": self.bindings(skip=skip),
            "point_in_time_binding": PIT_BINDING,
            "availability_bindings": (
                rules.AVAILABILITY_BINDING,
                EXCHANGE_INFO_AVAILABILITY_BINDING,
            ),
            "precedence_bindings": (
                DELIVERY_CHANNEL_BINDING,
                rules.PRECEDENCE_MAP_BINDING,
                lr.LISTING_OBSERVATION_BINDING,
            ),
            "parser_bindings": (rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING),
        }
        if interval is None:
            fields["simulation_time"] = at
        else:
            fields["simulation_start"], fields["simulation_end"] = interval
        fields.update(overrides)
        return PointInTimeSpec(**fields)

    def universe(self) -> UniverseBuilder:
        return UniverseBuilder(self.h.adapter, self.h.storage, market_data_base_url=ORIGIN)


@contextmanager
def sqlite_world(tmp_path: Path) -> Iterator[World]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with _world(tmp_path, SqliteCatalogHarness(tmp_path, REGISTRY)) as world:
        yield world


@contextmanager
def postgres_world(tmp_path: Path, uri: str) -> Iterator[World]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with _world(tmp_path, PostgresCatalogHarness(tmp_path, REGISTRY, uri=uri)) as world:
        yield world


@contextmanager
def _world(
    tmp_path: Path, catalog: SqliteCatalogHarness | PostgresCatalogHarness
) -> Iterator[World]:
    with ss._harness(tmp_path, catalog) as h:
        x = xs.Harness(
            tmp_path=tmp_path,
            catalog=cast(SqliteCatalogHarness, catalog),
            adapter=h.adapter,
            storage=h.storage,
            venue=RestVenue(origin=ORIGIN),
            wire_clock=xs.SetClock(),
            clock=xs.StepClock(start=LISTING_CLOCK, step=timedelta(seconds=1)),
            time=FakeTime(),
        )
        yield World(h, x)
