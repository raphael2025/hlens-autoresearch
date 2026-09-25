"""Builders for the D3E tests: real collector checkpoints, real catalog, real stores.

Nothing here fakes the pieces under test. REST pages are served by the D3D mock venue and
committed by the real ``BinanceSpotRestCollector``; archives are real ZIP bytes ingested by the
real D2 store; the D3E store and reconciler then run against the real C2/C3 catalog adapter
(SQLite for unit tests, PostgreSQL for the integration evidence). The only extra piece is a
catalog *proxy* that can crash or interleave another writer around a commit — the injection
points the recovery and race tests need.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import AlwaysTrue, BooleanExpression

from core.contracts.catalog import CommitRequest, CommitResult, SnapshotInfo, TableInfo
from core.contracts.collector import CollectionFailed, CollectionRequest, CollectionResult
from core.domain.base import canonical_json
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import (
    SUMMARY_BATCH_FINGERPRINT,
    SUMMARY_BATCH_ID,
    SUMMARY_BATCH_ROW_COUNT,
    SUMMARY_FINGERPRINT_RULE,
)
from infrastructure.collector.binance_rest import CHECKPOINT_PREFIX
from infrastructure.revision import RawRevisionStore
from infrastructure.revision.channel_reconcile import ChannelReconciler
from infrastructure.revision.rest_store import RestRevisionStore
from infrastructure.settings import local_file_uri_to_path
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    SqliteCatalogHarness,
)
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.parser.rest_support import agg_item as agg_item
from tests.infrastructure.parser.rest_support import body as body
from tests.infrastructure.parser.rest_support import kline_item as kline_item
from tests.infrastructure.revision import revision_support as rs

ORIGIN: Final = cs.ORIGIN
SYMBOL: Final = cs.SYMBOL
MINUTE_MS: Final = cs.MINUTE_MS
#: 2023-11-14T22:14:00Z: the archive for this UTC day is declared in milliseconds.
T0: Final = cs.T0
DAY: Final = date(2023, 11, 14)
#: 2025-01-01T00:05:00Z: the archive for this UTC day is declared in microseconds.
T0_2025: Final = 1_735_689_900_000
DAY_2025: Final = date(2025, 1, 1)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def at_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class Crash(Exception):
    """A simulated process death at an injection point."""


# =========================================================================================
# catalog proxy: crash / interleave injection around commits
# =========================================================================================


@dataclass
class ProxyCatalog:
    """Delegates to a real adapter; ``before`` / ``after`` hooks run around each commit."""

    inner: PyIcebergCatalogAdapter
    before: Callable[[CommitRequest], None] | None = None
    after: Callable[[CommitRequest, CommitResult], None] | None = None
    commits: list[str] = field(default_factory=list)

    def load_table(self, table: str) -> TableInfo | None:
        return self.inner.load_table(table)

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        return self.inner.get_snapshot(table, snapshot_id)

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult:
        if self.before is not None:
            self.before(request)
        result = self.inner.commit_batch(request, batch)
        self.commits.append(request.batch_id)
        if self.after is not None:
            self.after(request, result)
        return result

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        limit: int | None = None,
    ) -> pa.Table:
        return self.inner.scan_columns(table, columns=columns, row_filter=row_filter, limit=limit)

    def max_int64(
        self,
        table: str,
        column: str,
        *,
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        check: Callable[[int], None] | None = None,
    ) -> int | None:
        return self.inner.max_int64(table, column, row_filter=row_filter, check=check)


def crash_after_commits(count: int, *, table: str | None = None) -> Callable[..., None]:
    """An ``after`` hook that dies right after the ``count``-th matching commit succeeded."""
    seen = [0]

    def hook(request: CommitRequest, result: CommitResult) -> None:
        if table is not None and request.table != table:
            return
        seen[0] += 1
        if seen[0] == count:
            raise Crash(f"crash after commit {count} ({request.batch_id})")

    return hook


# =========================================================================================
# harness
# =========================================================================================


@dataclass
class RestHarness:
    """One catalog with the 12 Phase 1 tables, one warehouse, one mock venue."""

    tmp_path: Path
    catalog: SqliteCatalogHarness | PostgresCatalogHarness
    storage: LocalFileStorageAdapter
    adapter: PyIcebergCatalogAdapter
    venue: cs.RestVenue

    # ---------------------------------------------------------------- collection (D3D)

    def collect(
        self, request: CollectionRequest, *, start_ms: int = cs.RETRIEVED_AT_MS
    ) -> CollectionResult | CollectionFailed:
        """Run the real collector; a stable failure is returned rather than raised."""
        collector = cs.make_collector(self.storage, self.venue, clock=cs.StepClock(start_ms))
        try:
            return collector.collect(request)
        except CollectionFailed as exc:
            return exc
        finally:
            collector.close()

    # ---------------------------------------------------------------- stores

    def store(
        self,
        *,
        clock: Callable[[], datetime],
        adapter: Any = None,
        element_microbatch_rows: int | None = None,
    ) -> RestRevisionStore:
        kwargs: dict[str, Any] = {}
        if element_microbatch_rows is not None:
            kwargs["element_microbatch_rows"] = element_microbatch_rows
        return RestRevisionStore(
            self.adapter if adapter is None else adapter,
            self.storage,
            market_data_base_url=ORIGIN,
            clock=clock,
            **kwargs,
        )

    def archive_store(self, *, clock: Callable[[], datetime]) -> RawRevisionStore:
        return RawRevisionStore(self.adapter, self.storage, clock=clock)

    def reconciler(
        self, *, clock: Callable[[], datetime], adapter: Any = None, **kwargs: Any
    ) -> ChannelReconciler:
        return ChannelReconciler(
            self.adapter if adapter is None else adapter, self.storage, clock=clock, **kwargs
        )

    def ingest_archive(
        self,
        data_type: str,
        lines: list[str],
        *,
        day: date = DAY,
        clock: Callable[[], datetime],
        retrieved_at: datetime,
        request_id: str = "archive-1",
    ) -> Any:
        archive = rs.archive(
            self.storage,
            data_type=data_type,
            symbol=SYMBOL,
            day=day,
            rows=lines,
            retrieved_at=retrieved_at,
            request_id=request_id,
        )
        return self.archive_store(clock=clock).ingest(archive.collected, archive.context)

    # ---------------------------------------------------------------- catalog reads

    def reopen(self) -> PyIcebergCatalogAdapter:
        """A fresh adapter on the same catalog: what a process restart sees."""
        self.adapter = self.catalog.open_adapter()
        return self.adapter

    def rows(self, definition: RegisteredTableDefinition) -> list[dict[str, Any]]:
        columns = tuple(item.name for item in definition.arrow_schema)
        rows: list[dict[str, Any]] = self.adapter.scan_columns(
            definition.table, columns=columns
        ).to_pylist()
        return rows

    def head(self, table: str) -> str | None:
        info = self.adapter.load_table(table)
        assert info is not None
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def history(self, table: str) -> list[SnapshotInfo]:
        """Main-branch snapshots, oldest first."""
        info = self.adapter.load_table(table)
        assert info is not None
        found: list[SnapshotInfo] = []
        snapshot = info.current_snapshot
        while snapshot is not None:
            found.append(snapshot)
            parent = snapshot.parent_snapshot_id
            snapshot = None if parent is None else self.adapter.get_snapshot(table, parent)
        return list(reversed(found))

    def rows_at(self, table: str, snapshot_id: str) -> list[dict[str, Any]]:
        """Time travel through PyIceberg itself (independent of the adapter's current reads)."""
        namespace, name = table.split(".")
        iceberg = self.catalog.sql_catalog().load_table((namespace, name))
        rows: list[dict[str, Any]] = (
            iceberg.scan(snapshot_id=int(snapshot_id)).to_arrow().to_pylist()
        )
        return rows

    # ---------------------------------------------------------------- tampering

    def forge_rows(
        self, definition: RegisteredTableDefinition, rows: list[dict[str, Any]], batch_id: str
    ) -> None:
        """Commit rows through the adapter, bypassing the stores (a hostile / buggy writer)."""
        batch = pa.Table.from_pylist(rows, schema=definition.arrow_schema)
        request = CommitRequest(
            table=definition.table,
            batch_id=batch_id,
            batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
            row_count=len(rows),
            expected_parent_snapshot_id=self.head(definition.table),
        )
        self.adapter.commit_batch(request, batch)

    def forge_snapshot(
        self,
        definition: RegisteredTableDefinition,
        rows: list[dict[str, Any]],
        *,
        batch_id: str,
        fingerprint: str | None = None,
    ) -> None:
        """Append through raw PyIceberg with arbitrary batch metadata (catalog corruption)."""
        batch = pa.Table.from_pylist(rows, schema=definition.arrow_schema)
        namespace, name = definition.table.split(".")
        iceberg = self.catalog.sql_catalog().load_table((namespace, name))
        iceberg.append(
            batch,
            snapshot_properties={
                SUMMARY_BATCH_ID: batch_id,
                SUMMARY_BATCH_FINGERPRINT: fingerprint
                or definition.fingerprint_rule.fingerprint(batch),
                SUMMARY_BATCH_ROW_COUNT: str(len(rows)),
                SUMMARY_FINGERPRINT_RULE: definition.fingerprint_rule.rule_id,
            },
        )

    def overwrite_rows(
        self, definition: RegisteredTableDefinition, rows: list[dict[str, Any]], *, batch_id: str
    ) -> None:
        """Replace a table's content through raw PyIceberg (catalog corruption, not a store)."""
        batch = pa.Table.from_pylist(rows, schema=definition.arrow_schema)
        namespace, name = definition.table.split(".")
        iceberg = self.catalog.sql_catalog().load_table((namespace, name))
        iceberg.overwrite(
            batch,
            snapshot_properties={
                SUMMARY_BATCH_ID: batch_id,
                SUMMARY_BATCH_FINGERPRINT: definition.fingerprint_rule.fingerprint(batch),
                SUMMARY_BATCH_ROW_COUNT: str(len(rows)),
                SUMMARY_FINGERPRINT_RULE: definition.fingerprint_rule.rule_id,
            },
        )

    def delete_rows(
        self, definition: RegisteredTableDefinition, row_filter: BooleanExpression
    ) -> None:
        """Delete rows through raw PyIceberg (a hostile maintenance writer, no batch metadata).

        Unlike ``overwrite_rows`` the resulting snapshot is metadata-consistent, so what
        catches the tamper is the row / lineage / batch verification, not the snapshot reader.
        """
        namespace, name = definition.table.split(".")
        iceberg = self.catalog.sql_catalog().load_table((namespace, name))
        iceberg.delete(delete_filter=row_filter)

    def object_path(self, key: str) -> Path:
        return local_file_uri_to_path(self.storage.warehouse_uri, field_name="warehouse_uri") / key

    def read_object(self, key: str) -> bytes:
        ref = self.storage.lookup(key)
        assert ref is not None, key
        with self.storage.open_read(ref) as handle:
            data: bytes = handle.read()
        return data

    def tamper_json(self, key: str, mutate: Callable[[dict[str, Any]], None]) -> None:
        document = json.loads(self.read_object(key))
        mutate(document)
        self.object_path(key).write_bytes(canonical_json(document).encode("utf-8"))

    def cleanup(self) -> None:
        self.storage.close()
        self.catalog.cleanup()


def checkpoint_root(request_id: str) -> str:
    return f"{CHECKPOINT_PREFIX}/{hashlib.sha256(request_id.encode('utf-8')).hexdigest()}"


def page_checkpoint_key(request_id: str, page_index: int, symbol: str = SYMBOL) -> str:
    return f"{checkpoint_root(request_id)}/{symbol}/{page_index:08d}.page.json"


@contextmanager
def sqlite_harness(tmp_path: Path) -> Iterator[RestHarness]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    catalog = SqliteCatalogHarness(tmp_path, PHASE1_REGISTRY)
    with _harness(tmp_path, catalog) as harness:
        yield harness


@contextmanager
def postgres_harness(tmp_path: Path, uri: str) -> Iterator[RestHarness]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    catalog = PostgresCatalogHarness(tmp_path, PHASE1_REGISTRY, uri=uri)
    with _harness(tmp_path, catalog) as harness:
        yield harness


@contextmanager
def _harness(
    tmp_path: Path, catalog: SqliteCatalogHarness | PostgresCatalogHarness
) -> Iterator[RestHarness]:
    adapter = catalog.open_adapter()
    ensure_phase1_tables(adapter)
    harness = RestHarness(
        tmp_path=tmp_path,
        catalog=catalog,
        storage=cs.make_storage(tmp_path),
        adapter=adapter,
        venue=cs.RestVenue(),
    )
    try:
        yield harness
    finally:
        harness.cleanup()


# =========================================================================================
# payload builders shared by both channels
# =========================================================================================


def agg_items(
    count: int, *, first_id: int = 100, first_ms: int = T0, ms_step: int = 1
) -> list[dict[str, Any]]:
    return cs.agg_page(count, first_id=first_id, first_ms=first_ms, ms_step=ms_step)


def kline_items(count: int, *, first_ms: int = T0) -> list[list[Any]]:
    return cs.kline_page(count, first_ms=first_ms)


def archive_agg_lines(items: list[dict[str, Any]], *, factor: int = 1) -> list[str]:
    """The same aggTrades as archive CSV lines (``factor`` = archive ticks per REST tick)."""
    return [
        f"{item['a']},{item['p']},{item['q']},{item['f']},{item['l']},{item['T'] * factor},"
        f"{item['m']},{item['M']}"
        for item in items
    ]


def archive_kline_lines(items: list[list[Any]], *, factor: int = 1) -> list[str]:
    """The same 1m klines as archive CSV lines; the close tick is rescaled to the unit."""
    lines = []
    for item in items:
        opened = int(item[0]) * factor
        closed = (int(item[6]) + 1) * factor - 1
        fields = [str(opened), *[str(value) for value in item[1:6]], str(closed)]
        fields += [str(value) for value in item[7:]]
        lines.append(",".join(fields))
    return lines


def agg_request(request_id: str, *, start_ms: int = T0, minutes: int = 5) -> CollectionRequest:
    return cs.agg_request(
        request_id=request_id, start_ms=start_ms, end_ms=start_ms + minutes * MINUTE_MS
    )


def kline_request(request_id: str, *, start_ms: int = T0, minutes: int = 5) -> CollectionRequest:
    return cs.kline_request(
        request_id=request_id, start_ms=start_ms, end_ms=start_ms + minutes * MINUTE_MS
    )


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


StepClock = rs.StepClock
