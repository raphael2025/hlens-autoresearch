"""Operator capacity probe for the Phase 1 local data pipeline (G3-T / G3-C; PROJECT_STATUS.md §7).

``python -m infrastructure.tools.capacity_probe --rows N`` builds ``N`` synthetic aggTrades rows
spread evenly over one UTC day, publishes them as one archive object into a temporary SQLite
catalog + local warehouse (the same shape ``tests.infrastructure.revision.rest_store_support``
and ``tests.infrastructure.catalog.catalog_support`` build for tests), then runs the unmodified
production pipeline over that unit:

1. **ingest** — ``RawRevisionStore.ingest`` (D1 parse + D2 persist) of the synthetic archive;
2. **normalize** — ``CanonicalNormalizer.normalize_unit`` of that archive revision (E1);
3. **pit_select_1h** — ``PitSelector.select`` over one UTC hour of the day (F1);
4. **quality_report_day** — ``QualityReporter.report`` over the whole UTC day (E3), which itself
   re-runs ``PitSelector`` an hour at a time (G3-S3).

Three optional flags extend the same harness with more of the full chain (G3-C):

- ``--rest``: a synthetic REST tail (one page, at most ``PAGE_LIMIT`` rows) of the same trading
  day, ingested through the real D3D collector + D3E ``RestRevisionStore``, normalized (E1), then
  reconciled against the archive channel (D3E-R2 ``ChannelReconciler``, D-33);
- ``--dataset``: a mock ``exchangeInfo`` snapshot (E2) deriving the BTCUSDT / ETHUSDT listing
  history, then F2 ``UniverseBuilder`` and F3 ``DatasetBuilder`` building one Research Dataset
  into ``research.dataset_selections`` (ADR-0033, DS-1) plus its manifest;
- ``--feature``: a separate synthetic klines archive (added because the base run only ingests
  aggTrades), normalized into ``canonical.bars_1m``, PIT-selected over the whole day, then F4
  ``run_feature`` of the bar log-return provider (``plugins.features.bars.BarLogReturnProvider``).

Each stage is timed and memory-profiled; the tool prints one JSON summary to stdout. Nothing here
is imported by, or imports, the ``tests/`` tree: it reimplements the small amount of fixture
plumbing (a synthetic archive ZIP, a throwaway SQLite catalog + local warehouse, a synthetic REST
page / exchangeInfo body served over ``httpx.MockTransport``) directly against production
modules, so it stays runnable outside pytest and never becomes a load-bearing test double.

**Scope, read honestly**:

- this bypasses the real D0 archive collector and the D3D REST collector's real network I/O: the
  synthetic archive object is built and published in-process, not downloaded, and the REST /
  exchangeInfo pages are served by a fixed-response ``httpx.MockTransport`` that ignores the
  request URL, so no stage measures network or checksum-verification-against-a-remote-source cost;
- ``--rest`` exercises D3E's store and reconciler over exactly one committed REST page: it does
  not measure multi-page chains, retries, or the recovery paths D3E-R1 / D3E-R2 add for a crash
  mid-chain;
- ``--dataset`` writes fresh BTCUSDT / ETHUSDT day quality reports and a listing report
  immediately before building the Research Dataset, so ``universe_build`` / ``dataset_build``
  always see a dataset-ready catalog; it does not measure the cost of an *incremental* rebuild
  (D-MAN's known limit: a trades Research Dataset is built per UTC hour in production, never a
  whole day; this tool selects one hour to stay comparable with ``pit_select_1h``);
- ``--feature`` ingests its own klines archive (``min(rows, 1440)`` one-minute bars — a UTC day
  has only 1440 distinct minutes) and evaluates ``BarLogReturnProvider`` once, at a single
  far-future evaluation time that sees every bar; it does not measure a realistic evaluation-time
  grid or the realized-volatility / volume-sum providers;
- ``tracemalloc`` only tracks Python-level allocations; PyArrow and PyIceberg buffers are native
  and mostly invisible to it, so ``tracemalloc_peak_mb`` understates real memory pressure.
  ``ru_maxrss_delta_mb`` (the process' resident-set high-water mark) is the more trustworthy
  number, but it is cumulative and non-decreasing across the whole process: a later stage that
  never exceeds an earlier stage's peak reports a delta of ``0.0``, not its own usage.

Refuses ``--rows`` above 50 000 unless ``--i-know-memory`` is passed (WSL has crashed from memory
exhaustion at multi-million-row scale; see PROJECT_STATUS.md §7).
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
import tracemalloc
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import httpx
from pyiceberg.catalog.sql import SqlCatalog

from core.contracts.collector import CollectedObject, CollectionRequest
from core.contracts.feature import FeatureResult
from core.contracts.revision import PointInTimeSpec, PointInTimeStatus, PolicyBinding
from core.contracts.storage import StageRequest
from core.domain.base import FrozenMapping
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.canonical.listings import ListingDeriver
from infrastructure.canonical.normalizer import CanonicalNormalizer, CanonicalUnitNormalized
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
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
    DATA_QUALITY_REPORTS,
    DATASET_SELECTIONS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE, COLLECTOR_ID, COLLECTOR_VERSION
from infrastructure.collector.binance_exchange_info import (
    BinanceSpotExchangeInfoCollector,
    ExchangeInfoRequest,
)
from infrastructure.collector.binance_rest import REST_SOURCE, BinanceSpotRestCollector
from infrastructure.dataset.builder import DatasetBuilder, DatasetBuilt
from infrastructure.feature.observations import bar_observations, pit_feature_request
from infrastructure.feature.runner import run_feature
from infrastructure.parser.binance_archive import member_filename
from infrastructure.pit.selector import PIT_BINDING, REQUIRED_BINDINGS, PitSelection, PitSelector
from infrastructure.quality.listing_report import ListingQualityReporter
from infrastructure.quality.reporter import QualityReported, QualityReporter
from infrastructure.revision import ArchiveContext, ArchiveIngested, RawRevisionStore
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.channel_reconcile import ChannelReconciled, ChannelReconciler
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.exchange_info_store import ExchangeInfoSnapshotStore
from infrastructure.revision.identity import archive_object_key, archive_relative_path
from infrastructure.revision.rest_identity import PAGE_LIMIT
from infrastructure.revision.rest_store import RestCollectionStored, RestRevisionStore
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseBuilder, UniverseBuilt
from plugins.features.bars import BarLogReturnProvider

__all__ = ["main", "run_probe"]

#: Above this the tool refuses to run without an explicit override (WSL memory exhaustion).
MAX_ROWS_WITHOUT_OVERRIDE: Final = 50_000
ARCHIVE_BASE: Final = "https://data.binance.vision"
#: A structurally valid https origin for the mocked REST / exchangeInfo wire (RFC 2606 ``.invalid``
#: — never resolved: every request is answered by a fixed-response ``httpx.MockTransport``).
REST_BASE: Final = "https://market-data.capacity-probe.invalid"
SYMBOL: Final = "BTCUSDT"
OTHER_SYMBOL: Final = "ETHUSDT"
DATA_TYPE: Final = "agg_trades"
KLINES_DATA_TYPE: Final = "klines_1m"
#: On or after this day the D1 parser expects microsecond ticks (infrastructure/parser
#: /binance_archive.py ``MICROSECOND_FROM``); a fixed, arbitrary probe day.
DAY: Final = date(2025, 6, 1)
TICKS_PER_SECOND: Final = 1_000_000
_SECONDS_PER_DAY: Final = 86_400
#: A UTC day has exactly this many distinct one-minute bars; ``--feature`` never asks for more.
_MINUTES_PER_DAY: Final = 1_440

_DAY_START: Final = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

# ------------------------------------------------------------------------------------------
# the knowledge axis: retrieval, then each stage's clock reading, strictly in production order
# (every optional stage's constants are chained after the base stages' so the wall/knowledge
# clocks a real deployment would see keep advancing regardless of which flags are passed).
# ------------------------------------------------------------------------------------------
_RETRIEVED_AT: Final = _DAY_START + timedelta(days=1)
_KNOWLEDGE_INGEST: Final = _RETRIEVED_AT + timedelta(hours=1)
_KNOWLEDGE_NORMALIZE: Final = _KNOWLEDGE_INGEST + timedelta(hours=1)
_KNOWLEDGE_REPORT: Final = _KNOWLEDGE_NORMALIZE + timedelta(hours=1)
#: --rest
_REST_RETRIEVED_AT: Final = _KNOWLEDGE_REPORT + timedelta(hours=1)
_KNOWLEDGE_REST_INGEST: Final = _REST_RETRIEVED_AT + timedelta(hours=1)
_KNOWLEDGE_REST_NORMALIZE: Final = _KNOWLEDGE_REST_INGEST + timedelta(hours=1)
_KNOWLEDGE_RECONCILE: Final = _KNOWLEDGE_REST_NORMALIZE + timedelta(hours=1)
#: --dataset
_LISTING_RETRIEVED_AT: Final = _KNOWLEDGE_RECONCILE + timedelta(hours=1)
_KNOWLEDGE_LISTING_INGEST: Final = _LISTING_RETRIEVED_AT + timedelta(hours=1)
_KNOWLEDGE_LISTING_DERIVE: Final = _KNOWLEDGE_LISTING_INGEST + timedelta(hours=1)
_KNOWLEDGE_QUALITY_BTC: Final = _KNOWLEDGE_LISTING_DERIVE + timedelta(hours=1)
_KNOWLEDGE_QUALITY_ETH: Final = _KNOWLEDGE_QUALITY_BTC + timedelta(hours=1)
_KNOWLEDGE_QUALITY_LISTING: Final = _KNOWLEDGE_QUALITY_ETH + timedelta(hours=1)
#: --feature
_KLINES_RETRIEVED_AT: Final = _KNOWLEDGE_QUALITY_LISTING + timedelta(hours=1)
_KNOWLEDGE_KLINES_INGEST: Final = _KLINES_RETRIEVED_AT + timedelta(hours=1)
_KNOWLEDGE_KLINES_NORMALIZE: Final = _KNOWLEDGE_KLINES_INGEST + timedelta(hours=1)
#: A "we know everything now" point-in-time cutoff, always after every stage's knowledge time.
_FAR: Final = _KNOWLEDGE_KLINES_NORMALIZE + timedelta(days=3650)
#: The UTC hour selected by the ``pit_select_1h`` / ``dataset_build`` stages.
_SELECT_START: Final = _DAY_START + timedelta(hours=12)
_SELECT_END: Final = _SELECT_START + timedelta(hours=1)


# ============================================================================================
# measurement
# ============================================================================================


@dataclass(frozen=True, slots=True)
class StageMeasurement:
    wall_seconds: float
    tracemalloc_peak_mb: float
    ru_maxrss_delta_mb: float

    def as_dict(self) -> dict[str, float]:
        return {
            "wall_seconds": round(self.wall_seconds, 6),
            "tracemalloc_peak_mb": round(self.tracemalloc_peak_mb, 3),
            "ru_maxrss_delta_mb": round(self.ru_maxrss_delta_mb, 3),
        }


def _measure[T](fn: Callable[[], T]) -> tuple[T, StageMeasurement]:
    """Time ``fn`` and profile it; ``ru_maxrss`` is the process' cumulative high-water mark."""
    before_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    start = time.perf_counter()
    try:
        result = fn()
    finally:
        elapsed = time.perf_counter() - start
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    after_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    measurement = StageMeasurement(
        wall_seconds=elapsed,
        tracemalloc_peak_mb=peak / (1024 * 1024),
        ru_maxrss_delta_mb=(after_rss_kb - before_rss_kb) / 1024,
    )
    return result, measurement


# ============================================================================================
# injected time (mirrors tests.infrastructure.collector.rest_support, reimplemented locally)
# ============================================================================================


class _WireClock:
    """A UTC wall clock for the D3D-family wire: ``start``, then ``+1 ms`` per reading."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        value = self._now
        self._now = value + timedelta(milliseconds=1)
        return value


class _FakeMonotonic:
    """An injected monotonic clock + no-op sleeper: the min-request-interval throttle costs

    nothing real (the sleeper just advances the same fake monotonic clock it is measured by).
    """

    def __init__(self) -> None:
        self._now = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


def _fixed_response_transport(payload: bytes) -> httpx.MockTransport:
    """Answers every request with ``payload`` / HTTP 200, ignoring the request URL.

    The production collectors build and validate their own request URL before this transport
    ever sees it (D3D-R1 / E2 §"the request gate"); this fixture only needs to hand back bytes.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, request=request)

    return httpx.MockTransport(handler)


# ============================================================================================
# synthetic archive (reimplemented here, never imported from tests/)
# ============================================================================================


def _agg_trade_lines(rows: int) -> list[str]:
    """``rows`` structurally valid aggTrades CSV lines spread evenly over ``DAY`` (UTC)."""
    day_start_ticks = int(_DAY_START.timestamp()) * TICKS_PER_SECOND
    step = max(1, (_SECONDS_PER_DAY * TICKS_PER_SECOND) // rows)
    lines: list[str] = []
    for index in range(rows):
        agg_id = 1_000 + index
        first_id = 2_000 + index * 2
        last_id = first_id + 1
        ticks = day_start_ticks + index * step
        is_buyer_maker = "True" if index % 2 else "False"
        is_best_match = "True" if index % 3 else "False"
        lines.append(
            f"{agg_id},50000.00000000,0.01000000,{first_id},{last_id},{ticks},"
            f"{is_buyer_maker},{is_best_match}"
        )
    return lines


def _kline_lines(count: int) -> list[str]:
    """``count`` structurally valid 1m klines CSV lines, minute-aligned from ``DAY`` start.

    OHLC is held flat at 50000 (a valid, trivially-satisfied OHLC invariant): the point of this
    fixture is capacity under row count, not a realistic price path.
    """
    day_start_ticks = int(_DAY_START.timestamp()) * TICKS_PER_SECOND
    minute_ticks = 60 * TICKS_PER_SECOND
    lines: list[str] = []
    for index in range(count):
        open_ticks = day_start_ticks + index * minute_ticks
        close_ticks = open_ticks + minute_ticks - 1
        lines.append(
            f"{open_ticks},50000.00000000,50000.00000000,50000.00000000,50000.00000000,"
            f"1.00000000,{close_ticks},50000.00000000,10,0.50000000,25000.00000000,0"
        )
    return lines


def _build_archive_bytes(data_type: str, lines: list[str]) -> bytes:
    """One in-memory ZIP shaped exactly like a daily Binance archive (one CSV member)."""
    content = "".join(f"{line}\n" for line in lines).encode("utf-8")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_filename(data_type, SYMBOL, DAY), content)
    return buffer.getvalue()


def _publish_archive(
    storage: LocalFileStorageAdapter,
    lines: list[str],
    *,
    data_type: str = DATA_TYPE,
    retrieved_at: datetime = _RETRIEVED_AT,
) -> CollectedObject:
    """Build and publish a synthetic archive object (fixture setup, not a measured stage)."""
    data = _build_archive_bytes(data_type, lines)
    digest = sha256(data).hexdigest()
    key = archive_object_key(data_type, SYMBOL, DAY, digest)
    staged = storage.stage(StageRequest(key=key, expected_sha256=digest), [data])
    published = storage.publish(staged)
    relative = archive_relative_path(data_type, SYMBOL, DAY)
    return CollectedObject(
        ref=published.ref,
        symbol=SYMBOL,
        coverage_start=_DAY_START,
        coverage_end=_DAY_START + timedelta(days=1),
        source_uri=f"{ARCHIVE_BASE}/{relative}",
        retrieved_at=retrieved_at,
        source_sha256=digest,
    )


# ============================================================================================
# throwaway catalog + warehouse (same shape as tests.infrastructure.catalog.catalog_support)
# ============================================================================================


@contextmanager
def _harness(tmp_path: Path) -> Iterator[tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter]]:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(
        f"capacity_probe_{uuid.uuid4().hex[:16]}",
        uri=f"sqlite:///{tmp_path / 'catalog.sqlite'}",
        warehouse=warehouse.as_uri(),
    )
    storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
    try:
        adapter = PyIcebergCatalogAdapter(catalog, PHASE1_REGISTRY)
        ensure_phase1_tables(adapter)
        yield adapter, storage
    finally:
        storage.close()
        catalog.close()


def _head(adapter: PyIcebergCatalogAdapter, table: str) -> str | None:
    info = adapter.load_table(table)
    if info is None or info.current_snapshot is None:
        return None
    return info.current_snapshot.snapshot_id


# ============================================================================================
# the base four stages (archive channel only)
# ============================================================================================


def _ingest_archive(
    adapter: PyIcebergCatalogAdapter,
    storage: LocalFileStorageAdapter,
    collected: CollectedObject,
    *,
    data_type: str = DATA_TYPE,
    request_id: str,
    knowledge_clock: Callable[[], datetime],
) -> ArchiveIngested:
    context = ArchiveContext(
        request_id=request_id,
        data_type=data_type,
        collector_id=COLLECTOR_ID,
        collector_version=COLLECTOR_VERSION,
        source=ARCHIVE_SOURCE,
    )
    store = RawRevisionStore(adapter, storage, clock=knowledge_clock)
    outcome = store.ingest(collected, context)
    if not isinstance(outcome, ArchiveIngested):
        raise RuntimeError(f"the synthetic archive was rejected, not ingested: {outcome!r}")
    return outcome


def _normalize(
    adapter: PyIcebergCatalogAdapter,
    storage: LocalFileStorageAdapter,
    source_table: str,
    unit_revision_id: str,
    *,
    knowledge_clock: Callable[[], datetime],
) -> CanonicalUnitNormalized:
    normalizer = CanonicalNormalizer(adapter, storage, clock=knowledge_clock)
    return normalizer.normalize_unit(source_table, unit_revision_id)


def _pit_spec(
    adapter: PyIcebergCatalogAdapter,
    *,
    raw_table: str = BINANCE_SPOT_AGG_TRADES.table,
    canonical_table: str = CANONICAL_TRADES.table,
    extra_bound_tables: tuple[str, ...] = (),
    availability_bindings: tuple[PolicyBinding, ...] = REQUIRED_BINDINGS["availability_bindings"],
    precedence_bindings: tuple[PolicyBinding, ...] = (
        DELIVERY_CHANNEL_BINDING,
        rules.PRECEDENCE_MAP_BINDING,
    ),
    parser_bindings: tuple[PolicyBinding, ...] = REQUIRED_BINDINGS["parser_bindings"],
) -> PointInTimeSpec:
    tables = (
        BINANCE_SPOT_ARCHIVES.table,
        raw_table,
        canonical_table,
        BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
        # Bound whenever they have a snapshot (``_head`` returns None otherwise): a canonical
        # revision's lineage may point at either channel, so the selector's own provenance
        # re-verification needs both raw tables bound regardless of which one this call's own
        # ``raw_table`` names (e.g. --dataset combined with --rest).
        BINANCE_SPOT_REST_RESPONSES.table,
        BINANCE_SPOT_REST_AGG_TRADES.table,
        BINANCE_SPOT_REST_KLINES_1M.table,
        *extra_bound_tables,
    )
    bindings = {table: head for table in tables if (head := _head(adapter, table)) is not None}
    return PointInTimeSpec(
        name="hlens.tools.capacity-probe",
        version="1.0.0",
        simulation_time=_FAR,
        knowledge_cutoff=_FAR,
        snapshot_bindings=FrozenMapping(bindings),
        point_in_time_binding=PIT_BINDING,
        availability_bindings=availability_bindings,
        precedence_bindings=precedence_bindings,
        parser_bindings=parser_bindings,
    )


def _pit_select_one_hour(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> PitSelection:
    spec = _pit_spec(adapter)
    return PitSelector(adapter, storage).select(spec, DATA_TYPE, SYMBOL, _SELECT_START, _SELECT_END)


def _quality_report(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> QualityReported:
    reporter = QualityReporter(adapter, storage, clock=lambda: _KNOWLEDGE_REPORT)
    return reporter.report(DATA_TYPE, SYMBOL, DAY)


# ============================================================================================
# --rest: a REST tail of part of the day + channel reconciliation (D3D / D3E / D-33)
# ============================================================================================


def _epoch_ms(milliseconds: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=milliseconds)


def _tail_agg_items(rows: int, tail_n: int) -> list[dict[str, Any]]:
    """The last ``tail_n`` of ``rows`` archive-day trades, as REST aggTrades elements.

    Same ``agg_id`` / trade-id-range / timestamp / flag formulas as ``_agg_trade_lines`` (just
    milliseconds instead of the archive's microsecond ticks) so the REST tail describes the
    *same* observations as the tail of the archive: D-33 channel reconciliation finds them equal
    and records a precedence edge, rather than only exercising the "no comparable pair" path.
    """
    day_start_ticks = int(_DAY_START.timestamp()) * TICKS_PER_SECOND
    step = max(1, (_SECONDS_PER_DAY * TICKS_PER_SECOND) // rows)
    items: list[dict[str, Any]] = []
    for index in range(rows - tail_n, rows):
        ticks = day_start_ticks + index * step
        items.append(
            {
                "a": 1_000 + index,
                "p": "50000.00000000",
                "q": "0.01000000",
                "f": 2_000 + index * 2,
                "l": 2_000 + index * 2 + 1,
                "T": ticks // 1_000,
                "m": bool(index % 2),
                "M": bool(index % 3),
            }
        )
    return items


def _rest_tail_request(rows: int, items: list[dict[str, Any]]) -> CollectionRequest:
    start_ms = int(items[0]["T"])
    end_ms = int(items[-1]["T"])
    return CollectionRequest(
        request_id=f"capacity-probe-rest-{rows}",
        source=REST_SOURCE,
        data_type=DATA_TYPE,
        symbols=(SYMBOL,),
        coverage_start=_epoch_ms(start_ms),
        coverage_end=_epoch_ms(end_ms),
    )


def _collect_rest_tail(
    storage: LocalFileStorageAdapter, request: CollectionRequest, items: list[dict[str, Any]]
) -> None:
    """Commit the synthetic page's checkpoint through the real D3D collector (network mocked)."""
    payload = json.dumps(items, separators=(",", ":")).encode("utf-8")
    fake_time = _FakeMonotonic()
    with BinanceSpotRestCollector(
        storage,
        market_data_base_url=REST_BASE,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=1,
        http_user_agent="hlens-capacity-probe/0.0.0",
        max_pages_per_collect=1,
        min_request_interval_ms=50,
        max_retry_after_seconds=5,
        max_response_bytes=1_048_576,
        http_transport=_fixed_response_transport(payload),
        clock=_WireClock(_REST_RETRIEVED_AT - timedelta(milliseconds=1)),
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
    ) as collector:
        collector.collect(request)


def _ingest_rest_tail(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, request: CollectionRequest
) -> RestCollectionStored:
    with RestRevisionStore(
        adapter, storage, market_data_base_url=REST_BASE, clock=lambda: _KNOWLEDGE_REST_INGEST
    ) as store:
        return store.ingest_collection(request)


def _normalize_rest(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, stored: RestCollectionStored
) -> CanonicalUnitNormalized:
    if len(stored.pages) != 1:
        raise RuntimeError(f"expected exactly one REST page, got {len(stored.pages)}")
    [page] = stored.pages
    return _normalize(
        adapter,
        storage,
        BINANCE_SPOT_REST_AGG_TRADES.table,
        page.response_revision_id,
        knowledge_clock=lambda: _KNOWLEDGE_REST_NORMALIZE,
    )


def _reconcile_channels(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> ChannelReconciled:
    reconciler = ChannelReconciler(adapter, storage, clock=lambda: _KNOWLEDGE_RECONCILE)
    return reconciler.reconcile(DATA_TYPE, SYMBOL, DAY)


# ============================================================================================
# --dataset: mock exchangeInfo listings (E2) + F2 universe + F3 DatasetBuilder (ADR-0033)
# ============================================================================================


def _exchange_info_body() -> bytes:
    def entry(symbol: str, base: str) -> dict[str, Any]:
        return {"symbol": symbol, "status": "TRADING", "baseAsset": base, "quoteAsset": "USDT"}

    document = {
        "timezone": "UTC",
        "serverTime": int(_LISTING_RETRIEVED_AT.timestamp() * 1000),
        "symbols": [entry(SYMBOL, "BTC"), entry(OTHER_SYMBOL, "ETH")],
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def _prepare_listings(adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter) -> None:
    """Observe one exchangeInfo snapshot (both first-slice symbols TRADING) and derive listings.

    Fixture setup, not a measured stage: it stands in for the real E2 collector's answer so F2 /
    F3 have a listing history to read (ADR-0029 §1 / §2).
    """
    request = ExchangeInfoRequest("capacity-probe-exchange-info")
    fake_time = _FakeMonotonic()
    with BinanceSpotExchangeInfoCollector(
        storage,
        market_data_base_url=REST_BASE,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=1,
        http_user_agent="hlens-capacity-probe/0.0.0",
        min_request_interval_ms=50,
        max_retry_after_seconds=5,
        max_response_bytes=1_048_576,
        http_transport=_fixed_response_transport(_exchange_info_body()),
        clock=_WireClock(_LISTING_RETRIEVED_AT - timedelta(milliseconds=1)),
        monotonic=fake_time.monotonic,
        sleeper=fake_time.sleep,
    ) as collector:
        collector.collect(request)
    with ExchangeInfoSnapshotStore(
        adapter, storage, market_data_base_url=REST_BASE, clock=lambda: _KNOWLEDGE_LISTING_INGEST
    ) as store:
        store.ingest_snapshot(request)
    deriver = ListingDeriver(
        adapter, storage, market_data_base_url=REST_BASE, clock=lambda: _KNOWLEDGE_LISTING_DERIVE
    )
    try:
        deriver.derive()
    finally:
        deriver.close()


def _prepare_dataset_quality(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> None:
    """Commit the BTCUSDT / ETHUSDT day reports and the listing report F3 must find already there.

    Fixture setup, not a measured stage: F3 (ADR-0031 / ADR-0023 §6) only ever *re-derives* a
    report at the dataset's bound snapshots (``existing_only``), it never writes one.
    """
    QualityReporter(adapter, storage, clock=lambda: _KNOWLEDGE_QUALITY_BTC).report(
        DATA_TYPE, SYMBOL, DAY
    )
    QualityReporter(adapter, storage, clock=lambda: _KNOWLEDGE_QUALITY_ETH).report(
        DATA_TYPE, OTHER_SYMBOL, DAY
    )
    ListingQualityReporter(
        adapter, storage, market_data_base_url=REST_BASE, clock=lambda: _KNOWLEDGE_QUALITY_LISTING
    ).report()


def _dataset_pit_spec(adapter: PyIcebergCatalogAdapter) -> PointInTimeSpec:
    """A PIT spec bound to trades + the listing / quality tables F2 and F3 both require."""
    return _pit_spec(
        adapter,
        extra_bound_tables=(
            BINANCE_SPOT_EXCHANGE_INFO.table,
            CANONICAL_INSTRUMENT_LISTINGS.table,
            DATA_QUALITY_REPORTS.table,
            QUALITY_EVIDENCE_GAPS.table,
        ),
        availability_bindings=(rules.AVAILABILITY_BINDING, EXCHANGE_INFO_AVAILABILITY_BINDING),
        precedence_bindings=(
            DELIVERY_CHANNEL_BINDING,
            rules.PRECEDENCE_MAP_BINDING,
            lr.LISTING_OBSERVATION_BINDING,
        ),
        parser_bindings=(rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING),
    )


def _build_universe(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, pit: PointInTimeSpec
) -> UniverseBuilt:
    return UniverseBuilder(adapter, storage, market_data_base_url=REST_BASE).build(
        FIRST_SLICE_UNIVERSE, pit
    )


def _build_dataset(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, pit: PointInTimeSpec
) -> DatasetBuilt:
    builder = DatasetBuilder(
        adapter, storage, market_data_base_url=REST_BASE, dataset_table=DATASET_SELECTIONS
    )
    return builder.build(FIRST_SLICE_UNIVERSE, pit, DATA_TYPE, _SELECT_START, _SELECT_END)


# ============================================================================================
# --feature: a klines archive (added because the base run is aggTrades-only) + F4 run_feature
# ============================================================================================


def _ingest_klines_archive(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, klines_rows: int
) -> ArchiveIngested:
    collected = _publish_archive(
        storage,
        _kline_lines(klines_rows),
        data_type=KLINES_DATA_TYPE,
        retrieved_at=_KLINES_RETRIEVED_AT,
    )
    return _ingest_archive(
        adapter,
        storage,
        collected,
        data_type=KLINES_DATA_TYPE,
        request_id=f"capacity-probe-klines-{klines_rows}",
        knowledge_clock=lambda: _KNOWLEDGE_KLINES_INGEST,
    )


def _normalize_klines(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, archive_revision_id: str
) -> CanonicalUnitNormalized:
    return _normalize(
        adapter,
        storage,
        BINANCE_SPOT_KLINES_1M.table,
        archive_revision_id,
        knowledge_clock=lambda: _KNOWLEDGE_KLINES_NORMALIZE,
    )


def _run_bar_log_return(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> FeatureResult:
    pit = _pit_spec(
        adapter, raw_table=BINANCE_SPOT_KLINES_1M.table, canonical_table=CANONICAL_BARS_1M.table
    )
    selection = PitSelector(adapter, storage).select(
        pit, KLINES_DATA_TYPE, SYMBOL, _DAY_START, _DAY_START + timedelta(days=1)
    )
    selection.require_no_conflict()
    observations = bar_observations(selection, pit)
    spec = BarLogReturnProvider.spec()
    request = pit_feature_request(
        pit_spec=pit,
        observations=observations,
        feature=spec,
        evaluation_times=(_FAR,),
        manifest_content_hash=sha256(b"hlens.tools.capacity-probe.feature").hexdigest(),
    )
    provider = BarLogReturnProvider([spec])
    return run_feature(provider, spec, request)


# ============================================================================================
# orchestration
# ============================================================================================

_NOTES: Final[tuple[str, ...]] = (
    "ingest_archive times RawRevisionStore.ingest only (D1 parse + D2 persist); building and "
    "publishing the synthetic archive object happens before the measured window, so this never "
    "includes the real D0/D3D network collector.",
    "pit_select_1h and quality_report_day run the unmodified production PitSelector / "
    "QualityReporter over the archive channel of the base run.",
    "tracemalloc_peak_mb only sees Python-level allocations; PyArrow / PyIceberg buffers are "
    "native and mostly invisible to it, so it understates real memory pressure.",
    "ru_maxrss_delta_mb is the process' resident-set high-water mark, which only grows: it is "
    "cumulative across stages, so a later stage that never exceeds an earlier stage's peak can "
    "report 0.0 even though it allocated real memory that was then freed.",
)
_REST_NOTES: Final[tuple[str, ...]] = (
    "ingest_rest / normalize_rest / channel_reconcile cover exactly one committed REST page "
    "(min(rows, PAGE_LIMIT) elements, the tail of the same trading day the archive covers): D3D "
    "network I/O is mocked and multi-page chains, retries and D3E-R1/D3E-R2 crash-recovery paths "
    "are not exercised.",
)
_DATASET_NOTES: Final[tuple[str, ...]] = (
    "listing observation, listing derivation and the BTCUSDT/ETHUSDT/listing quality reports "
    "are fixture setup, not measured stages (mirrors how the base run's archive publish is not "
    "measured either); universe_build and dataset_build are each timed on their own, even though "
    "dataset_build internally repeats a universe build (F3 does not reuse F2's result object).",
    "dataset_build selects the same one UTC hour as pit_select_1h, not a whole day: production "
    "builds a trades Research Dataset per UTC hour, never per day (D-MAN, PROJECT_STATUS.md §6).",
)
_FEATURE_NOTES: Final[tuple[str, ...]] = (
    "feature_bar_log_return evaluates BarLogReturnProvider once, at a single far-future "
    "evaluation time that already sees every ingested bar; it does not measure a realistic "
    "evaluation-time grid, gaps, or the realized-volatility / volume-sum providers.",
    "the klines archive is capped at min(rows, 1440): a UTC day has only 1440 distinct "
    "one-minute bars, so --feature's own row count can differ from --rows above that cap.",
)


def run_probe(
    rows: int, *, rest: bool = False, dataset: bool = False, feature: bool = False
) -> dict[str, Any]:
    """Run the base four stages for ``rows`` synthetic aggTrades, plus any requested extension."""
    stages: dict[str, dict[str, Any]] = {}
    notes = list(_NOTES)
    with tempfile.TemporaryDirectory(prefix="hlens-capacity-probe-") as raw_tmp:
        with _harness(Path(raw_tmp)) as (adapter, storage):
            collected = _publish_archive(storage, _agg_trade_lines(rows))

            ingested, measurement = _measure(
                lambda: _ingest_archive(
                    adapter,
                    storage,
                    collected,
                    request_id=f"capacity-probe-{rows}",
                    knowledge_clock=lambda: _KNOWLEDGE_INGEST,
                )
            )
            stages["ingest_archive"] = measurement.as_dict() | {
                "archive_revision_id": ingested.archive_revision_id,
                "row_count": ingested.row_count,
            }

            normalized, measurement = _measure(
                lambda: _normalize(
                    adapter,
                    storage,
                    BINANCE_SPOT_AGG_TRADES.table,
                    ingested.archive_revision_id,
                    knowledge_clock=lambda: _KNOWLEDGE_NORMALIZE,
                )
            )
            stages["normalize"] = measurement.as_dict() | {
                "canonical_rows": normalized.revision_count,
            }

            selection, measurement = _measure(lambda: _pit_select_one_hour(adapter, storage))
            selected = sum(
                1 for item in selection.selections if item.status is PointInTimeStatus.SELECTED
            )
            stages["pit_select_1h"] = measurement.as_dict() | {
                "window_start": _SELECT_START.isoformat(),
                "window_end": _SELECT_END.isoformat(),
                "keys_evaluated": len(selection.records),
                "keys_selected": selected,
            }

            report, measurement = _measure(lambda: _quality_report(adapter, storage))
            stages["quality_report_day"] = measurement.as_dict() | {
                "event_count": len(report.row["events"]),
                "reused": report.reused,
            }

            if rest:
                tail_n = min(rows, PAGE_LIMIT)
                items = _tail_agg_items(rows, tail_n)
                request = _rest_tail_request(rows, items)
                _collect_rest_tail(storage, request, items)  # fixture setup, not measured

                stored, measurement = _measure(lambda: _ingest_rest_tail(adapter, storage, request))
                stages["ingest_rest"] = measurement.as_dict() | {
                    "page_count": len(stored.pages),
                    "element_count": len(items),
                    "collection_outcome": stored.collection_outcome,
                    "finding_count": len(stored.findings),
                }

                normalized_rest, measurement = _measure(
                    lambda: _normalize_rest(adapter, storage, stored)
                )
                stages["normalize_rest"] = measurement.as_dict() | {
                    "canonical_rows": normalized_rest.revision_count,
                }

                reconciled, measurement = _measure(lambda: _reconcile_channels(adapter, storage))
                stages["channel_reconcile"] = measurement.as_dict() | {
                    "edge_count": len(reconciled.edges),
                    "new_edge_count": len(reconciled.new_edge_ids),
                    "finding_count": len(reconciled.findings),
                }
                notes.extend(_REST_NOTES)

            if dataset:
                _prepare_listings(adapter, storage)  # fixture setup, not measured
                _prepare_dataset_quality(adapter, storage)  # fixture setup, not measured
                pit = _dataset_pit_spec(adapter)

                built_universe, measurement = _measure(
                    lambda: _build_universe(adapter, storage, pit)
                )
                stages["universe_build"] = measurement.as_dict() | {
                    "member_count": len(built_universe.members),
                    "exclusion_count": len(built_universe.exclusions),
                }

                built_dataset, measurement = _measure(lambda: _build_dataset(adapter, storage, pit))
                stages["dataset_build"] = measurement.as_dict() | {
                    "row_count": len(built_dataset.selection.rows),
                    "manifest_content_hash": built_dataset.manifest.content_hash(),
                    "replayed": built_dataset.replayed,
                }
                notes.extend(_DATASET_NOTES)

            if feature:
                klines_rows = min(rows, _MINUTES_PER_DAY)
                ingested_klines, measurement = _measure(
                    lambda: _ingest_klines_archive(adapter, storage, klines_rows)
                )
                stages["ingest_klines_archive"] = measurement.as_dict() | {
                    "row_count": ingested_klines.row_count,
                }

                normalized_klines, measurement = _measure(
                    lambda: _normalize_klines(adapter, storage, ingested_klines.archive_revision_id)
                )
                stages["normalize_klines"] = measurement.as_dict() | {
                    "canonical_rows": normalized_klines.revision_count,
                }

                result, measurement = _measure(lambda: _run_bar_log_return(adapter, storage))
                stages["feature_bar_log_return"] = measurement.as_dict() | {
                    "evaluation_count": len(result.values),
                    "non_null_count": sum(1 for item in result.values if item.value is not None),
                }
                notes.extend(_FEATURE_NOTES)

    return {
        "rows": rows,
        "symbol": SYMBOL,
        "data_type": DATA_TYPE,
        "day": DAY.isoformat(),
        "flags": {"rest": rest, "dataset": dataset, "feature": feature},
        "stages": stages,
        "notes": notes,
    }


# ============================================================================================
# CLI
# ============================================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.tools.capacity_probe",
        description=(
            "Generate N synthetic aggTrades spread over one UTC day into a temporary "
            "SQLite-catalog harness, then run ingest -> normalize -> a one-hour PIT selection "
            "-> a day quality report, timing and memory-profiling each stage. Optional flags "
            "extend the same harness with a REST tail + channel reconciliation, a Research "
            "Dataset build, and an F4 feature run over a klines archive."
        ),
    )
    parser.add_argument(
        "--rows", type=int, required=True, help="number of synthetic aggTrades rows to generate"
    )
    parser.add_argument(
        "--rest",
        action="store_true",
        help="add a synthetic REST tail (D3D/D3E) and channel reconciliation (D-33)",
    )
    parser.add_argument(
        "--dataset",
        action="store_true",
        help="add a mock exchangeInfo listing snapshot (E2), F2 universe and F3 DatasetBuilder",
    )
    parser.add_argument(
        "--feature",
        action="store_true",
        help="add a klines archive and an F4 run_feature of the bar log-return provider",
    )
    parser.add_argument(
        "--i-know-memory",
        action="store_true",
        help=(
            f"allow --rows above {MAX_ROWS_WITHOUT_OVERRIDE}; WSL has crashed from memory "
            "exhaustion at this scale, use with care"
        ),
    )
    args = parser.parse_args(argv)
    if args.rows < 1:
        parser.error("--rows must be at least 1")
    if args.rows > MAX_ROWS_WITHOUT_OVERRIDE and not args.i_know_memory:
        parser.error(
            f"--rows {args.rows} exceeds {MAX_ROWS_WITHOUT_OVERRIDE}; pass --i-know-memory to "
            "run it anyway (WSL has crashed from memory exhaustion at this scale; "
            "PROJECT_STATUS.md §7)"
        )
    summary = run_probe(args.rows, rest=args.rest, dataset=args.dataset, feature=args.feature)
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
