"""PostgreSQL evidence for D2 (roadmap Phase 1 #12 / #17).

Runs only with ``HLENS_TEST_CATALOG_URI`` naming the dedicated ``*_test`` database (explicit skip
otherwise; a skip is not evidence). Every test uses its own PyIceberg ``catalog_name`` and its own
``tmp_path`` warehouse, and drops what it created. The catalog goes through the runtime factory,
the archives are real ZIP bytes published through the real storage adapter, and the rows are
produced by the real D1 parser.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import CommitOutcome
from infrastructure.catalog import PHASE1_REGISTRY, ensure_phase1_tables
from infrastructure.revision import (
    ARCHIVE_TABLE,
    ROW_TABLES,
    ArchiveIngested,
    ArchiveRejected,
    RawRevisionStore,
    identity,
)
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    postgres_test_catalog_uri,
)
from tests.infrastructure.parser import parser_support as ps
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.revision_support import StoreHarness

pytestmark = pytest.mark.postgres

AGG_TABLE = ROW_TABLES["agg_trades"]
KLINE_TABLE = ROW_TABLES["klines_1m"]


@pytest.fixture
def pg_store(tmp_path: Path) -> Iterator[StoreHarness]:
    """The D2 store on the dedicated PostgreSQL catalog and a local ``file://`` warehouse."""
    catalog = PostgresCatalogHarness(tmp_path, PHASE1_REGISTRY, uri=postgres_test_catalog_uri())
    adapter = catalog.open_adapter()
    ensure_phase1_tables(adapter)
    harness = StoreHarness(
        tmp_path=tmp_path,
        catalog=catalog,
        storage=rs.storage_adapter(tmp_path),
        adapter=adapter,
        clock=rs.StepClock(),
    )
    try:
        yield harness
    finally:
        harness.cleanup()


def _ingested(outcome: Any) -> ArchiveIngested:
    assert isinstance(outcome, ArchiveIngested), outcome
    return outcome


def test_both_raw_tables_and_the_archive_table_are_written(pg_store: StoreHarness) -> None:
    agg = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=5))
    kline = rs.archive(pg_store.storage, data_type="klines_1m", rows=ps.kline_rows(ps.US_DAY, 4))
    first = _ingested(pg_store.store().ingest(agg.collected, agg.context))
    second = _ingested(pg_store.store().ingest(kline.collected, kline.context))

    assert pg_store.total_rows(ARCHIVE_TABLE) == 2
    assert pg_store.total_rows(AGG_TABLE) == 5
    assert pg_store.total_rows(KLINE_TABLE) == 4
    assert first.arrival_seq_base == 0
    assert second.arrival_seq_base == identity.ARRIVAL_SEQ_STRIDE
    seqs = [
        row["arrival_seq"]
        for table in (ARCHIVE_TABLE, AGG_TABLE, KLINE_TABLE)
        for row in pg_store.rows(table, ("arrival_seq",))
    ]
    assert len(seqs) == len(set(seqs)) == 11


def test_replay_after_restart_adds_no_snapshot_row_or_sequence(pg_store: StoreHarness) -> None:
    item = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=9))
    first = _ingested(pg_store.store(microbatch_rows=4).ingest(item.collected, item.context))
    archive_snapshot = pg_store.snapshot_id(ARCHIVE_TABLE)
    rows_snapshot = pg_store.snapshot_id(AGG_TABLE)
    seqs = sorted(row["arrival_seq"] for row in pg_store.rows(AGG_TABLE, ("arrival_seq",)))

    pg_store.reopen()
    second = _ingested(pg_store.store(microbatch_rows=4).ingest(item.collected, item.context))

    assert second.replayed
    assert second.snapshot_ids == first.snapshot_ids
    assert pg_store.snapshot_id(ARCHIVE_TABLE) == archive_snapshot
    assert pg_store.snapshot_id(AGG_TABLE) == rows_snapshot
    assert pg_store.total_rows(AGG_TABLE) == 9
    assert sorted(row["arrival_seq"] for row in pg_store.rows(AGG_TABLE, ("arrival_seq",))) == seqs


def test_crash_after_the_archive_commit_is_repaired_by_re_running(
    pg_store: StoreHarness,
) -> None:
    item = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=6))
    with pytest.raises(_Crash):
        RawRevisionStore(
            _CrashAfter(pg_store.adapter, commits=1), pg_store.storage, clock=pg_store.clock
        ).ingest(item.collected, item.context)
    assert pg_store.total_rows(ARCHIVE_TABLE) == 1
    assert pg_store.total_rows(AGG_TABLE) == 0

    pg_store.reopen()
    result = _ingested(pg_store.store().ingest(item.collected, item.context))

    assert result.archive_commit.outcome is CommitOutcome.ALREADY_COMMITTED
    assert result.arrival_seq_base == 0
    assert pg_store.total_rows(AGG_TABLE) == 6


def test_crash_between_microbatches_leaves_only_whole_batches(pg_store: StoreHarness) -> None:
    item = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=7))
    # ADR-0108: only pre-ADR-0108 history can hold a committed prefix; the production store
    # below completes it along that old per-microbatch path.
    with pytest.raises(_Crash):
        RawRevisionStore(
            _CrashAfter(pg_store.adapter, commits=3),
            pg_store.storage,
            clock=pg_store.clock,
            microbatch_rows=2,
            _legacy_batch_commits=True,
        ).ingest(item.collected, item.context)
    assert pg_store.total_rows(AGG_TABLE) == 4

    pg_store.reopen()
    result = _ingested(pg_store.store(microbatch_rows=2).ingest(item.collected, item.context))

    assert [commit.replayed for commit in result.row_commits] == [True, True, False, False]
    assert pg_store.total_rows(AGG_TABLE) == 7
    assert sorted(
        row["archive_line_number"] for row in pg_store.rows(AGG_TABLE, ("archive_line_number",))
    ) == [1, 2, 3, 4, 5, 6, 7]


def test_a_parent_snapshot_race_reallocates_the_block(pg_store: StoreHarness) -> None:
    """Another writer moves the archive head between the read and the commit."""
    other = rs.archive(pg_store.storage, symbol="ETHUSDT", rows=ps.agg_rows(ps.US_DAY, count=2))
    item = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=3))

    def interfere() -> None:
        pg_store.store().ingest(other.collected, other.context)

    racing = _RaceOnce(pg_store.adapter, table=ARCHIVE_TABLE, before_commit=interfere)
    result = _ingested(
        RawRevisionStore(racing, pg_store.storage, clock=pg_store.clock).ingest(
            item.collected, item.context
        )
    )

    assert racing.fired
    assert result.arrival_seq_base == identity.ARRIVAL_SEQ_STRIDE  # the loser re-allocated
    assert pg_store.total_rows(ARCHIVE_TABLE) == 2
    assert pg_store.total_rows(AGG_TABLE) == 5
    seqs = [row["arrival_seq"] for row in pg_store.rows(AGG_TABLE, ("arrival_seq",))]
    seqs += [row["arrival_seq"] for row in pg_store.rows(ARCHIVE_TABLE, ("arrival_seq",))]
    assert len(seqs) == len(set(seqs))


def test_replacement_keeps_both_revisions_and_reports_competing_heads(
    pg_store: StoreHarness,
) -> None:
    original = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    first = _ingested(pg_store.store().ingest(original.collected, original.context))
    replacement = rs.archive(
        pg_store.storage,
        rows=rs.replacement_rows(ps.US_DAY, count=3),
        retrieved_at=original.collected.retrieved_at + timedelta(days=10),
    )
    second = _ingested(pg_store.store().ingest(replacement.collected, replacement.context))

    assert second.has_competing_heads
    assert set(second.maximal_heads) == {first.archive_revision_id, second.archive_revision_id}
    assert second.supersedes == ()
    assert pg_store.total_rows(ARCHIVE_TABLE) == 2
    assert pg_store.total_rows(AGG_TABLE) == 6
    archives = pg_store.rows(ARCHIVE_TABLE, ("revision_id", "observation_key", "object_key"))
    assert len({row["observation_key"] for row in archives}) == 1
    assert len({row["object_key"] for row in archives}) == 2
    for ref in (original.collected.ref, replacement.collected.ref):
        with pg_store.storage.open_read(ref) as handle:
            assert handle.read()
    rows = pg_store.rows(AGG_TABLE, ("observation_key", "revision_id", "archive_revision_id"))
    # Each observation key now has two revisions, one per archive; nothing was overwritten.
    by_key: dict[str, set[str]] = {}
    for row in rows:
        by_key.setdefault(row["observation_key"], set()).add(row["revision_id"])
    assert all(len(revisions) == 2 for revisions in by_key.values())


def test_rejected_archive_produces_no_snapshot_at_all(pg_store: StoreHarness) -> None:
    broken = ps.csv_bytes(ps.agg_rows(ps.US_DAY, count=2)).replace(b"92792.05000000", b"-1")
    item = rs.archive(
        pg_store.storage, data=ps.archive_for("agg_trades", "BTCUSDT", ps.US_DAY, broken)
    )
    outcome = pg_store.store().ingest(item.collected, item.context)

    assert isinstance(outcome, ArchiveRejected)
    assert pg_store.snapshot_id(ARCHIVE_TABLE) is None
    assert pg_store.snapshot_id(AGG_TABLE) is None


def test_evidence_gaps_are_persisted_on_every_row(pg_store: StoreHarness) -> None:
    item = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=4))
    result = _ingested(pg_store.store().ingest(item.collected, item.context))

    gaps = {summary.table: summary for summary in result.availability_gaps}
    assert gaps[ARCHIVE_TABLE].revision_count == 1
    assert gaps[AGG_TABLE].revision_count == 4
    rows = pg_store.rows(AGG_TABLE, ("available_time", "ingest_time", "availability_evidence_gap"))
    assert all(row["available_time"] == row["ingest_time"] for row in rows)
    assert all(row["availability_evidence_gap"] == gaps[AGG_TABLE].gap for row in rows)


def test_catalog_database_still_holds_only_iceberg_metadata(pg_store: StoreHarness) -> None:
    item = rs.archive(pg_store.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    result = _ingested(pg_store.store().ingest(item.collected, item.context))

    catalog = pg_store.catalog.sql_catalog()
    with catalog.engine.connect() as connection:
        tables = [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2"
            ).all()
        ]
        rows = [
            repr(tuple(row))
            for row in connection.exec_driver_sql("SELECT * FROM iceberg_tables").all()
        ]
    assert tables == [("public", "iceberg_namespace_properties"), ("public", "iceberg_tables")]
    assert not any(result.archive_revision_id in row for row in rows), "revision data leaked"
    assert not any("arrival_seq" in row for row in rows), "sequence state leaked into PostgreSQL"
    assert list(pg_store.tmp_path.rglob("*.parquet")), "market data must live in the warehouse"


class _Crash(RuntimeError):
    """Injected process death between two commits."""


class _CrashAfter:
    """Adapter proxy that dies after ``commits`` successful commits."""

    def __init__(self, adapter: Any, *, commits: int) -> None:
        self._adapter = adapter
        self._left = commits

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def commit_batch(self, request: Any, batch: Any) -> Any:
        if self._left <= 0:
            raise _Crash("simulated crash before the next commit")
        self._left -= 1
        return self._adapter.commit_batch(request, batch)

    def commit_unit(self, request: Any, batches: Any, **kwargs: Any) -> Any:
        if self._left <= 0:
            raise _Crash("simulated crash before the next commit")
        self._left -= 1
        return self._adapter.commit_unit(request, batches, **kwargs)


class _RaceOnce:
    """Adapter proxy that lets another writer commit first, exactly once, for one table."""

    def __init__(self, adapter: Any, *, table: str, before_commit: Any) -> None:
        self._adapter = adapter
        self._table = table
        self._before_commit = before_commit
        self.fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def commit_batch(self, request: Any, batch: Any) -> Any:
        if not self.fired and request.table == self._table:
            self.fired = True
            self._before_commit()
        return self._adapter.commit_batch(request, batch)
