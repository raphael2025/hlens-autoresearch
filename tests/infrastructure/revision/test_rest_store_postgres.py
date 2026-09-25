"""PostgreSQL evidence for D3E (ADR-0027 acceptance #2 ~ #8, #15, #21 on the runtime catalog).

Runs only with ``HLENS_TEST_CATALOG_URI`` naming the dedicated ``*_test`` database (explicit skip
otherwise; a skip is not evidence). Every test uses its own PyIceberg ``catalog_name`` and
``tmp_path`` warehouse and drops what it created. The catalog goes through the runtime factory;
REST checkpoints come from the real D3D collector, archives from the real D2 store.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from core.contracts.catalog import CommitOutcome, CommitRequest
from core.contracts.collector import CollectionResult
from infrastructure.catalog import PHASE1_TABLES, ensure_phase1_tables
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.revision.channel_reconcile import (
    assemble_channel_graph,
    evidence_from_row,
    revision_record_from_row,
)
from infrastructure.revision.rest_store import RestRevisionStore
from tests.infrastructure.catalog.catalog_support import postgres_test_catalog_uri
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    T0,
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

pytestmark = pytest.mark.postgres

RESPONSES = BINANCE_SPOT_REST_RESPONSES
REST_AGGS = BINANCE_SPOT_REST_AGG_TRADES
REST_KLINES = BINANCE_SPOT_REST_KLINES_1M
ARCHIVE_AGGS = BINANCE_SPOT_AGG_TRADES
EVIDENCE = BINANCE_SPOT_PRECEDENCE_EVIDENCE
BASE = 1 << 62
STRIDE = 1 << 32
K_ARCHIVE = utc(2023, 12, 1)
K_REST = utc(2023, 12, 5)
K_EDGE = utc(2023, 12, 10)


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.postgres_harness(tmp_path, postgres_test_catalog_uri()) as opened:
        assert opened.adapter.load_table(RESPONSES.table) is not None
        yield opened


def _collect_agg(h: RestHarness, request_id: str, count: int = 3) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, T0, [ss.agg_items(count)])
    assert isinstance(h.collect(ss.agg_request(request_id)), CollectionResult)


def test_all_twelve_phase1_tables_exist_with_their_bindings(pg: RestHarness) -> None:
    ensure_phase1_tables(pg.adapter)  # idempotent on an existing catalog
    bound = {}
    for definition in PHASE1_TABLES:
        info = pg.adapter.load_table(definition.table)
        assert info is not None, definition.table
        bound[definition.table] = info.definition
    assert len(bound) == 12
    for definition in (RESPONSES, REST_AGGS, REST_KLINES, EVIDENCE):
        assert bound[definition.table] == definition.binding


def test_store_reconcile_replay_and_time_travel_on_postgres(pg: RestHarness) -> None:
    items = ss.agg_items(3)
    pg.ingest_archive(
        "agg_trades",
        ss.archive_agg_lines(items),
        clock=StepClock(start=K_ARCHIVE),
        retrieved_at=utc(2023, 11, 16),
    )
    cs.queue_agg_chain(pg.venue, SYMBOL, T0, [items])
    assert isinstance(pg.collect(ss.agg_request("pg-a")), CollectionResult)
    rest_clock = StepClock(start=K_REST)
    store = pg.store(clock=rest_clock)

    stored = store.ingest_collection(ss.agg_request("pg-a"))

    assert stored.pages[0].arrival_seq_base == BASE
    assert stored.pages[0].response_commit.outcome is CommitOutcome.COMMITTED
    [response] = pg.rows(RESPONSES)
    assert response["knowledge_time"] == K_REST and response["arrival_seq"] == BASE
    rest_rows = sorted(pg.rows(REST_AGGS), key=lambda row: row["arrival_seq"])
    assert [row["arrival_seq"] for row in rest_rows] == [BASE + 1, BASE + 2, BASE + 3]
    heads = {table: pg.head(table.table) for table in (RESPONSES, REST_AGGS)}

    # Replay: zero new revision, zero new snapshot, zero new clock reading.
    replay = pg.store(clock=rest_clock).ingest_collection(ss.agg_request("pg-a"))
    assert replay.replayed and rest_clock.calls == 1
    assert {table: pg.head(table.table) for table in (RESPONSES, REST_AGGS)} == heads

    rest_snapshot = pg.head(REST_AGGS.table)
    archive_snapshot = pg.head(ARCHIVE_AGGS.table)
    out = pg.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    assert len(out.edges) == 3 and out.findings == ()
    evidence = pg.rows(EVIDENCE)
    assert {row["revision_snapshot_id"] for row in evidence} == {archive_snapshot}
    assert {row["superseded_snapshot_id"] for row in evidence} == {rest_snapshot}
    assert {row["knowledge_time"] for row in evidence} == {K_EDGE}

    # Time travel: the snapshots each edge pinned hold exactly the compared revisions.
    assert rest_snapshot is not None and archive_snapshot is not None
    pinned_rest = {row["revision_id"] for row in pg.rows_at(REST_AGGS.table, rest_snapshot)}
    pinned_archive = {
        row["revision_id"] for row in pg.rows_at(ARCHIVE_AGGS.table, archive_snapshot)
    }
    assert {row["superseded_revision_id"] for row in evidence} == pinned_rest
    assert {row["revision_id"] for row in evidence} == pinned_archive
    first_response_snapshot = pg.history(RESPONSES.table)[0].snapshot_id
    assert [row["revision_id"] for row in pg.rows_at(RESPONSES.table, first_response_snapshot)] == [
        response["revision_id"]
    ]

    # The guard accepts every aggregated graph; each key selects the archive revision.
    for key in {row["observation_key"] for row in evidence}:
        archive = [
            revision_record_from_row(row)
            for row in pg.rows(ARCHIVE_AGGS)
            if row["observation_key"] == key
        ]
        rest = [revision_record_from_row(row) for row in rest_rows if row["observation_key"] == key]
        edges = [evidence_from_row(row) for row in evidence if row["observation_key"] == key]
        graph = assemble_channel_graph(archive, rest, edges)
        assert len(graph.revisions) == 2

    # Re-run: the first edge record is authoritative.
    evidence_head = pg.head(EVIDENCE.table)
    again = pg.reconciler(clock=StepClock(start=K_EDGE + timedelta(days=9))).reconcile(
        "agg_trades", SYMBOL, DAY
    )
    assert again.commits == () and all(item.reused for item in again.edges)
    assert pg.head(EVIDENCE.table) == evidence_head and len(pg.rows(EVIDENCE)) == 3


def test_restart_recovery_after_s1_on_postgres(pg: RestHarness) -> None:
    _collect_agg(pg, "pg-crash")
    clock = StepClock(start=K_REST)
    proxy = ProxyCatalog(pg.adapter, after=ss.crash_after_commits(1, table=RESPONSES.table))
    with pytest.raises(Crash):
        pg.store(clock=clock, adapter=proxy).ingest_collection(ss.agg_request("pg-crash"))
    assert len(pg.rows(RESPONSES)) == 1 and pg.rows(REST_AGGS) == []

    restarted = pg.reopen()  # a new PostgreSQL connection: what a new process sees
    with RestRevisionStore(
        restarted, pg.storage, market_data_base_url=ss.ORIGIN, clock=clock
    ) as store:
        out = store.ingest_collection(ss.agg_request("pg-crash"))

    assert clock.calls == 1  # the committed knowledge_time is reused, never re-stamped
    [page] = out.pages
    assert page.response_commit.outcome is CommitOutcome.ALREADY_COMMITTED
    assert [commit.outcome for commit in page.element_commits] == [CommitOutcome.COMMITTED]
    rows = pg.rows(REST_AGGS)
    assert sorted(row["arrival_seq"] for row in rows) == [BASE + 1, BASE + 2, BASE + 3]
    assert {row["knowledge_time"] for row in rows} == {K_REST}


def test_concurrent_writers_on_two_connections_never_share_a_block(pg: RestHarness) -> None:
    _collect_agg(pg, "pg-a")
    cs.queue_kline_chain(pg.venue, SYMBOL, T0, [ss.kline_items(2)])
    assert isinstance(pg.collect(ss.kline_request("pg-k")), CollectionResult)
    second_connection = pg.catalog.open_adapter()
    rival = pg.store(clock=StepClock(start=K_REST + timedelta(hours=1)), adapter=second_connection)
    fired: list[bool] = []

    def interleave(commit: CommitRequest) -> None:
        if commit.table == RESPONSES.table and not fired:
            fired.append(True)
            rival.ingest_collection(ss.kline_request("pg-k"))

    proxy = ProxyCatalog(pg.adapter, before=interleave)
    out = pg.store(clock=StepClock(start=K_REST), adapter=proxy).ingest_collection(
        ss.agg_request("pg-a")
    )

    assert fired and out.pages[0].arrival_seq_base == BASE + STRIDE
    assert sorted(row["arrival_seq"] for row in pg.rows(RESPONSES)) == [BASE, BASE + STRIDE]
    seqs = [row["arrival_seq"] for table in (REST_AGGS, REST_KLINES) for row in pg.rows(table)]
    assert len(seqs) == len(set(seqs)) == 5


def test_the_same_revision_raced_on_two_connections_is_committed_once(pg: RestHarness) -> None:
    _collect_agg(pg, "pg-a")
    rival = pg.store(
        clock=StepClock(start=K_REST + timedelta(minutes=3)), adapter=pg.catalog.open_adapter()
    )
    fired: list[bool] = []

    def interleave(commit: CommitRequest) -> None:
        if not fired:
            fired.append(True)
            rival.ingest_collection(ss.agg_request("pg-a"))

    proxy = ProxyCatalog(pg.adapter, before=interleave)
    out = pg.store(clock=StepClock(start=K_REST), adapter=proxy).ingest_collection(
        ss.agg_request("pg-a")
    )

    [row] = pg.rows(RESPONSES)
    assert row["knowledge_time"] == K_REST + timedelta(minutes=3) == out.pages[0].knowledge_time
    rows = pg.rows(REST_AGGS)
    assert len(rows) == len({item["revision_id"] for item in rows}) == 3


def test_a_raced_reconcile_on_two_connections_keeps_one_edge_time(pg: RestHarness) -> None:
    items = ss.agg_items(2)
    pg.ingest_archive(
        "agg_trades",
        ss.archive_agg_lines(items),
        clock=StepClock(start=K_ARCHIVE),
        retrieved_at=utc(2023, 11, 16),
    )
    cs.queue_agg_chain(pg.venue, SYMBOL, T0, [items])
    assert isinstance(pg.collect(ss.agg_request("pg-a")), CollectionResult)
    pg.store(clock=StepClock(start=K_REST)).ingest_collection(ss.agg_request("pg-a"))
    rival = pg.reconciler(
        clock=StepClock(start=K_EDGE + timedelta(minutes=1)), adapter=pg.catalog.open_adapter()
    )
    fired: list[bool] = []

    def interleave(commit: CommitRequest) -> None:
        if commit.table == EVIDENCE.table and not fired:
            fired.append(True)
            rival.reconcile("agg_trades", SYMBOL, DAY)

    proxy = ProxyCatalog(pg.adapter, before=interleave)
    out = pg.reconciler(clock=StepClock(start=K_EDGE), adapter=proxy).reconcile(
        "agg_trades", SYMBOL, DAY
    )

    assert fired and out.commits == () and all(item.reused for item in out.edges)
    rows = pg.rows(EVIDENCE)
    assert len(rows) == len({row["edge_id"] for row in rows}) == 2
    assert {row["knowledge_time"] for row in rows} == {K_EDGE + timedelta(minutes=1)}


def test_forged_lineage_and_drifted_competitor_are_refused_on_postgres(pg: RestHarness) -> None:
    """D3E-R1 on the runtime catalog: Codex counterexamples A and B fail closed."""
    items = ss.agg_items(3)
    changed = [dict(item) for item in items]
    changed[1]["p"] = "92792.06000000"
    cs.queue_agg_chain(pg.venue, SYMBOL, T0, [items])
    cs.queue_agg_chain(pg.venue, SYMBOL, T0, [changed])
    assert isinstance(pg.collect(ss.agg_request("pg-a")), CollectionResult)
    assert isinstance(
        pg.collect(ss.agg_request("pg-b"), start_ms=cs.RETRIEVED_AT_MS + 60_000), CollectionResult
    )
    pg.store(clock=StepClock(start=K_REST)).ingest_collection(ss.agg_request("pg-a"))
    [response_a] = pg.rows(RESPONSES)

    # A: element 100 (shared by both bodies) names a forged lineage with a later knowledge_time
    # → pg-b's own response row is lawful and committed, but no element of pg-b is written.
    shared = next(row for row in pg.rows(REST_AGGS) if row["agg_trade_id"] == 100)
    others = [row for row in pg.rows(REST_AGGS) if row["agg_trade_id"] != 100]
    forged_element = dict(shared)
    forged_element["response_revision_id"] = "rev1-" + "9" * 64
    forged_element["knowledge_time"] = shared["knowledge_time"] + timedelta(hours=1)
    pg.overwrite_rows(REST_AGGS, [*others, forged_element], batch_id="corruption")
    element_head = pg.head(REST_AGGS.table)
    with pytest.raises(CatalogIntegrityError, match="is committed 0 time"):
        pg.store(clock=StepClock(start=K_EDGE)).ingest_collection(ss.agg_request("pg-b"))
    assert pg.head(REST_AGGS.table) == element_head
    assert forged_element in pg.rows(REST_AGGS) and len(pg.rows(REST_AGGS)) == 3
    assert len(pg.rows(RESPONSES)) == 2

    # B: the competitor drifts (knowledge before ingest, zeroed policy hash) → re-running pg-b
    # is refused while reading its own page identity: nothing at all is written.
    response_b = next(
        row for row in pg.rows(RESPONSES) if row["revision_id"] != response_a["revision_id"]
    )
    forged_response = dict(response_a)
    forged_response["knowledge_time"] = response_a["ingest_time"] - timedelta(hours=1)
    forged_response["availability_policy_hash"] = "0" * 64
    pg.overwrite_rows(RESPONSES, [forged_response, response_b], batch_id="corruption")
    heads = {table.table: pg.head(table.table) for table in (RESPONSES, REST_AGGS, EVIDENCE)}
    with pytest.raises(CatalogIntegrityError, match="knowledge_time must not precede"):
        pg.store(clock=StepClock(start=K_EDGE)).ingest_collection(ss.agg_request("pg-b"))
    assert {table: pg.head(table) for table in heads} == heads
    assert pg.rows(EVIDENCE) == []
