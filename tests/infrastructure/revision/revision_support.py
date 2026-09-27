"""Builders for the D2 revision store tests: real objects, real catalog, real parser.

Nothing here fakes the pieces under test. An archive fixture publishes real ZIP bytes through
``LocalFileStorageAdapter`` under the content-addressed key the D0 collector now builds, and the
store then runs the real D1 parser and the real C2/C3 catalog adapter. Only the catalog backend
differs between the SQLite unit harness and the PostgreSQL integration harness.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from core.contracts.collector import (
    CollectedObject,
    CollectionRequest,
    CollectionResult,
    SourceBinding,
)
from core.contracts.storage import PublishResult, StageRequest
from core.domain.base import FrozenMapping
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE, COLLECTOR_ID, COLLECTOR_VERSION
from infrastructure.revision import ArchiveContext, RawRevisionStore, identity
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    SqliteCatalogHarness,
)
from tests.infrastructure.parser import parser_support as ps

ARCHIVE_BASE: Final = "https://data.binance.vision"
#: A fixed knowledge-axis clock; tests that need another vintage inject their own.
INGEST: Final = datetime(2025, 1, 2, 10, 30, tzinfo=UTC)
#: The store's injected clock starts later than every `retrieved_at` used in the tests.
KNOWLEDGE: Final = INGEST + timedelta(days=45)


class StepClock:
    """Injected UTC clock: returns ``start``, then ``start + step`` on every further call."""

    def __init__(self, start: datetime = KNOWLEDGE, step: timedelta = timedelta(seconds=1)) -> None:
        self._now = start
        self._step = step
        self.calls = 0

    def __call__(self) -> datetime:
        now = self._now
        self._now = now + self._step
        self.calls += 1
        return now


def storage_adapter(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def publish(storage: LocalFileStorageAdapter, key: str, data: bytes) -> PublishResult:
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=hashlib.sha256(data).hexdigest()),
        [data],
    )
    return storage.publish(staged)


@dataclass(frozen=True)
class Archive:
    """One published archive plus everything the store needs about its collection."""

    collected: CollectedObject
    context: ArchiveContext
    data: bytes

    @property
    def sha256(self) -> str:
        return self.collected.ref.sha256

    def result(self) -> CollectionResult:
        request = CollectionRequest(
            request_id=self.context.request_id,
            source=self.context.source,
            data_type=self.context.data_type,
            symbols=(self.collected.symbol,),
            coverage_start=self.collected.coverage_start,
            coverage_end=self.collected.coverage_end,
        )
        return CollectionResult(
            request=request,
            collector_id=self.context.collector_id,
            collector_version=self.context.collector_version,
            objects=(self.collected,),
            gaps=(),
        )


def archive(
    storage: LocalFileStorageAdapter,
    *,
    data_type: str = "agg_trades",
    symbol: str = "BTCUSDT",
    day: date = ps.US_DAY,
    rows: list[str] | None = None,
    data: bytes | None = None,
    retrieved_at: datetime = INGEST,
    request_id: str = "req-1",
    source: SourceBinding = ARCHIVE_SOURCE,
    source_metadata: dict[str, str] | None = None,
) -> Archive:
    """Publish a synthetic but structurally real daily archive and describe its collection."""
    if data is None:
        lines = rows if rows is not None else _default_rows(data_type, day)
        data = ps.archive_for(data_type, symbol, day, ps.csv_bytes(lines))
    digest = hashlib.sha256(data).hexdigest()
    key = identity.archive_object_key(data_type, symbol, day, digest)
    published = publish(storage, key, data)
    relative = identity.archive_relative_path(data_type, symbol, day)
    collected = CollectedObject(
        ref=published.ref,
        symbol=symbol,
        coverage_start=ps.day_start(day),
        coverage_end=ps.day_start(day) + timedelta(days=1),
        source_uri=f"{ARCHIVE_BASE}/{relative}",
        retrieved_at=retrieved_at,
        source_sha256=digest,
        source_metadata=FrozenMapping(
            source_metadata or {"etag": '"abc"', "last-modified": "Thu, 02 Jan 2025 09:00:00 GMT"}
        ),
    )
    context = ArchiveContext(
        request_id=request_id,
        data_type=data_type,
        collector_id=COLLECTOR_ID,
        collector_version=COLLECTOR_VERSION,
        source=source,
    )
    return Archive(collected=collected, context=context, data=data)


def _default_rows(data_type: str, day: date) -> list[str]:
    if data_type == "agg_trades":
        return ps.agg_rows(day, count=5)
    return ps.kline_rows(day, count=5)


def replacement_rows(day: date, count: int = 5) -> list[str]:
    """Rows of a *different* archive for the same day (a replacement with a new checksum)."""
    rows = ps.agg_rows(day, count=count)
    return [row.replace("92792.05000000", "92792.06000000") for row in rows]


@dataclass
class StoreHarness:
    """A SQLite-backed catalog with the eight Phase 1 tables plus a real local warehouse."""

    tmp_path: Path
    catalog: SqliteCatalogHarness | PostgresCatalogHarness
    storage: LocalFileStorageAdapter
    adapter: PyIcebergCatalogAdapter
    clock: StepClock

    def store(self, **kwargs: Any) -> RawRevisionStore:
        return RawRevisionStore(self.adapter, self.storage, clock=self.clock, **kwargs)

    def reopen(self) -> PyIcebergCatalogAdapter:
        """A fresh adapter on the same catalog: what a process restart sees."""
        self.adapter = self.catalog.open_adapter()
        return self.adapter

    def rows(self, table: str, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self.adapter.scan_columns(table, columns=columns).to_pylist()
        return rows

    def snapshot_id(self, table: str) -> str | None:
        info = self.adapter.load_table(table)
        assert info is not None
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def total_rows(self, table: str) -> int:
        info = self.adapter.load_table(table)
        assert info is not None
        return 0 if info.current_snapshot is None else info.current_snapshot.total_rows

    def cleanup(self) -> None:
        self.storage.close()
        self.catalog.cleanup()


@contextmanager
def store_harness(tmp_path: Path) -> Iterator[StoreHarness]:
    """Fixture body shared by the unit tests (SQLite is never PostgreSQL evidence)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    catalog = SqliteCatalogHarness(tmp_path, PHASE1_REGISTRY)
    adapter = catalog.open_adapter()
    ensure_phase1_tables(adapter)
    harness = StoreHarness(
        tmp_path=tmp_path,
        catalog=catalog,
        storage=storage_adapter(tmp_path),
        adapter=adapter,
        clock=StepClock(),
    )
    try:
        yield harness
    finally:
        harness.cleanup()
