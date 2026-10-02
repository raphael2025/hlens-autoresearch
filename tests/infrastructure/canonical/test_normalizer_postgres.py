"""PostgreSQL evidence for E1 (ADR-0028 on the runtime catalog).

Runs only with ``HLENS_TEST_CATALOG_URI`` naming the dedicated ``*_test`` database (explicit skip
otherwise; a skip is not evidence).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.revision import PointInTimeStatus
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    SYMBOL,
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

pytestmark = pytest.mark.postgres

KEY = f"binance:spot:agg_trade:{SYMBOL}:100"


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.postgres_harness(tmp_path, postgres_test_catalog_uri()) as opened:
        yield opened


def test_dual_lineage_normalization_recovery_and_mapping_on_postgres(pg: RestHarness) -> None:
    items = ss.agg_items(3)
    archive = c.ingest_archive(
        pg, "agg_trades", ss.archive_agg_lines(items), knowledge=utc(2023, 12, 1)
    )
    [response] = c.ingest_rest(pg, "agg_trades", items, knowledge=utc(2023, 12, 5))
    proxy = ProxyCatalog(pg.adapter, after=ss.crash_after_commits(1, table=c.TRADES.table))
    with pytest.raises(Crash):
        c.normalizer(pg, clock=StepClock(start=utc(2023, 12, 6)), adapter=proxy, microbatch_rows=1)\
            .normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    restarted = pg.reopen()  # a fresh adapter: what a process restart sees
    later = StepClock(start=utc(2024, 1, 1))
    resumed = c.normalizer(pg, clock=later, adapter=restarted, microbatch_rows=1).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert later.calls == 0
    assert resumed.knowledge_time == utc(2023, 12, 6)
    c.normalizer(pg, clock=StepClock(start=utc(2023, 12, 7))).normalize_unit(
        c.REST_AGGS.table, response
    )
    pg.reconciler(clock=StepClock(start=utc(2023, 12, 10))).reconcile("agg_trades", SYMBOL, ss.DAY)
    revisions = c.records(pg, KEY)
    assert len(revisions) == 2
    [mapped] = c.mapped_edges(pg, KEY)
    assert mapped.knowledge_time == utc(2023, 12, 10)
    assert c.select(revisions, [mapped], utc(2023, 12, 8))[0] is PointInTimeStatus.CONFLICT
    status, heads = c.select(revisions, [mapped], utc(2023, 12, 10))
    assert status is PointInTimeStatus.SELECTED and heads == (mapped.revision_id,)
    # Time travel: the snapshot after the archive unit holds exactly its three revisions.
    history = pg.history(c.TRADES.table)
    assert len(history) == 2  # One snapshot for each of the archive and REST units.
    [archive_snapshot] = [snapshot for snapshot in history if archive in (snapshot.batch_id or "")]
    assert archive_snapshot.added_rows == 3
    archive_rows = pg.rows_at(c.TRADES.table, archive_snapshot.snapshot_id)
    assert {row["lineage_source_revision_id"] for row in archive_rows} == {archive}
    assert len(archive_rows) == 3


def test_a_forged_raw_row_is_refused_on_postgres(pg: RestHarness) -> None:
    items = ss.agg_items(1)
    archive = c.ingest_archive(
        pg, "agg_trades", ss.archive_agg_lines(items), knowledge=utc(2023, 12, 1)
    )
    [row] = pg.rows(c.ARCHIVE_AGGS)
    pg.delete_rows(c.ARCHIVE_AGGS, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    pg.forge_rows(
        c.ARCHIVE_AGGS,
        [dict(row, knowledge_time=row["knowledge_time"] + timedelta(hours=1))],
        "corruption",
    )
    clock = StepClock(start=utc(2023, 12, 6))
    with pytest.raises(CatalogIntegrityError, match=r"\['knowledge_time'\]"):
        c.normalizer(pg, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and pg.rows(c.TRADES) == []
