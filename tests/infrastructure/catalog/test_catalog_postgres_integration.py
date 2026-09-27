"""PostgreSQL integration evidence for the C2 PyIceberg catalog (roadmap Phase 1 #8, ADR-0021).

Runs only with ``HLENS_TEST_CATALOG_URI`` naming the dedicated ``*_test`` database; otherwise
skipped explicitly. Direct SQL here is read-only verification; every Iceberg metadata change
goes through PyIceberg. Row data is read back through PyIceberg scans of the local warehouse.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table import Table as IcebergTable

from core.contracts.catalog import (
    BatchConflict,
    BatchRejected,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    UnknownTableDefinition,
)
from infrastructure.catalog import (
    CatalogIntegrityError,
    PyIcebergCatalogAdapter,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    open_postgres_catalog_adapter,
)
from tests.infrastructure.catalog.catalog_support import (
    ALPHA,
    ALPHA_V110,
    BETA,
    RULE,
    PostgresCatalogHarness,
    make_batch,
    rows_of,
)

pytestmark = pytest.mark.postgres


def _request(table: str, batch_id: str, batch: pa.Table, parent: str | None) -> CommitRequest:
    return CommitRequest(
        table=table,
        batch_id=batch_id,
        batch_fingerprint=RULE.fingerprint(batch),
        row_count=batch.num_rows,
        expected_parent_snapshot_id=parent,
    )


def _commit(
    adapter: PyIcebergCatalogAdapter,
    table: str,
    batch_id: str,
    batch: pa.Table,
    parent: str | None,
) -> CommitResult:
    return adapter.commit_batch(_request(table, batch_id, batch, parent), batch)


def _scan(catalog: SqlCatalog, table: str, snapshot: SnapshotInfo | None = None) -> pa.Table:
    iceberg = catalog.load_table(tuple(table.split(".")))
    scan = (
        iceberg.scan() if snapshot is None else iceberg.scan(snapshot_id=int(snapshot.snapshot_id))
    )
    return scan.to_arrow()


def _sql(catalog: SqlCatalog, statement: str) -> list[tuple[Any, ...]]:
    """Read-only verification query on the catalog database."""
    with catalog.engine.connect() as connection:
        return [tuple(row) for row in connection.exec_driver_sql(statement).all()]


def test_runtime_factory_creates_appends_and_time_travels(
    pg_harness: PostgresCatalogHarness,
) -> None:
    adapter = open_postgres_catalog_adapter(pg_harness.settings(), pg_harness.registry)
    try:
        adapter.create_table(ALPHA.binding)
        batch_a, batch_b = make_batch(3, "a"), make_batch(2, "b")
        first = _commit(adapter, ALPHA.table, "batch-a", batch_a, None).snapshot
        second = _commit(adapter, ALPHA.table, "batch-b", batch_b, first.snapshot_id).snapshot
    finally:
        adapter.close()

    # Snapshot history metadata from real PyIceberg metadata.
    catalog = pg_harness.sql_catalog()
    iceberg = catalog.load_table(("c2test", "alpha"))
    history = [(s.snapshot_id, s.parent_snapshot_id) for s in iceberg.snapshots()]
    assert history == [
        (int(first.snapshot_id), None),
        (int(second.snapshot_id), int(first.snapshot_id)),
    ]
    for info in (first, second):
        raw = iceberg.metadata.snapshot_by_id(int(info.snapshot_id))
        assert raw is not None and raw.summary is not None
        assert info.committed_at.tzinfo is UTC
        assert int(info.committed_at.timestamp() * 1000) == raw.timestamp_ms
        assert raw.summary["added-records"] == str(info.added_rows)
        assert raw.summary["total-records"] == str(info.total_rows)
        assert raw.summary["hlens.batch.id"] == info.batch_id
        assert raw.summary["hlens.batch.fingerprint"] == info.batch_fingerprint
    assert (first.added_rows, first.total_rows, second.added_rows, second.total_rows) == (
        3,
        3,
        2,
        5,
    )

    # Time travel over real row data: old snapshot sees only batch-a, new sees both.
    assert rows_of(_scan(catalog, ALPHA.table, first)) == rows_of(batch_a)
    assert rows_of(_scan(catalog, ALPHA.table, second)) == rows_of(
        pa.concat_tables([batch_a, batch_b])
    )
    assert rows_of(_scan(catalog, ALPHA.table)) == rows_of(_scan(catalog, ALPHA.table, second))


def test_restarted_adapter_recovers_tables_snapshots_and_batches(
    pg_harness: PostgresCatalogHarness,
) -> None:
    writer = pg_harness.open_adapter()
    created = writer.create_table(BETA.binding)
    batch_a, batch_b, batch_c = make_batch(4, "a"), make_batch(3, "b"), make_batch(2, "c")
    first = _commit(writer, BETA.table, "batch-a", batch_a, None).snapshot
    second = _commit(writer, BETA.table, "batch-b", batch_b, first.snapshot_id).snapshot
    writer.close()

    restarted = pg_harness.open_adapter()
    info = restarted.load_table(BETA.table)
    assert info is not None and info.definition == created.definition == BETA.binding
    assert info.current_snapshot == second
    assert restarted.get_snapshot(BETA.table, first.snapshot_id) == first
    # Idempotency is recovered from snapshot history, with a stale parent, after restart.
    replay = _commit(restarted, BETA.table, "batch-a", batch_a, None)
    assert replay.outcome is CommitOutcome.ALREADY_COMMITTED and replay.snapshot == first
    third = _commit(restarted, BETA.table, "batch-c", batch_c, second.snapshot_id).snapshot
    assert third.parent_snapshot_id == second.snapshot_id and third.total_rows == 9
    expected = pa.concat_tables([batch_a, batch_b, batch_c])
    assert rows_of(_scan(pg_harness.sql_catalog(), BETA.table)) == rows_of(expected)


def test_replay_after_later_snapshots_returns_first_commit_and_checks_content(
    pg_harness: PostgresCatalogHarness,
) -> None:
    adapter = pg_harness.open_adapter()
    adapter.create_table(ALPHA.binding)
    batch_a = make_batch(3, "a")
    first = _commit(adapter, ALPHA.table, "batch-a", batch_a, None).snapshot
    parent = first.snapshot_id
    for index in range(3):
        later = _commit(adapter, ALPHA.table, f"later-{index}", make_batch(2, f"l{index}"), parent)
        parent = later.snapshot.snapshot_id
    current = adapter.load_table(ALPHA.table)
    adapter.close()

    restarted = pg_harness.open_adapter()
    for stale in (None, first.snapshot_id, parent):
        again = _commit(restarted, ALPHA.table, "batch-a", batch_a, stale)
        assert again.outcome is CommitOutcome.ALREADY_COMMITTED and again.snapshot == first
    # Swapped content with the original declared fingerprint: rejected by the content check.
    swapped = make_batch(3, "a-swapped")
    original_request = _request(ALPHA.table, "batch-a", batch_a, parent)
    with pytest.raises(BatchRejected):
        restarted.commit_batch(original_request, swapped)
    # Swapped content declared honestly: passes the content check, conflicts with batch-a.
    with pytest.raises(BatchConflict):
        _commit(restarted, ALPHA.table, "batch-a", swapped, parent)
    assert restarted.load_table(ALPHA.table) == current
    assert len(pg_harness.sql_catalog().load_table(("c2test", "alpha")).snapshots()) == 4
    assert len(_scan(pg_harness.sql_catalog(), ALPHA.table)) == 3 + 3 * 2


def _race(
    writers: list[PyIcebergCatalogAdapter],
    batch_ids: list[str],
    batches: list[pa.Table],
    parent: str | None,
) -> list[object]:
    """Start both commits behind a barrier; return each result or raised exception."""
    barrier = threading.Barrier(len(writers))
    outcomes: list[object] = [None] * len(writers)

    def run(i: int) -> None:
        barrier.wait()
        try:
            outcomes[i] = _commit(writers[i], ALPHA.table, batch_ids[i], batches[i], parent)
        except Exception as exc:  # recorded and asserted by the caller
            outcomes[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(writers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


@pytest.mark.parametrize("rounds", [4])
def test_concurrent_writers_exactly_one_wins_then_retry_succeeds(
    pg_harness: PostgresCatalogHarness, rounds: int
) -> None:
    pg_harness.open_adapter().create_table(ALPHA.binding)
    parent: str | None = None
    expected: list[pa.Table] = []
    for round_index in range(rounds):
        writers = [pg_harness.open_adapter(), pg_harness.open_adapter()]
        batch_ids = [f"r{round_index}-w{i}" for i in range(2)]
        batches = [make_batch(2 + i, batch_ids[i]) for i in range(2)]
        outcomes = _race(writers, batch_ids, batches, parent)
        winners = [i for i, o in enumerate(outcomes) if isinstance(o, CommitResult)]
        losers = [i for i, o in enumerate(outcomes) if isinstance(o, CommitConflict)]
        assert len(winners) == 1 and len(losers) == 1, outcomes
        won = outcomes[winners[0]]
        assert isinstance(won, CommitResult) and won.outcome is CommitOutcome.COMMITTED
        assert won.snapshot.parent_snapshot_id == parent
        loser = losers[0]
        retried = _commit(
            writers[loser], ALPHA.table, batch_ids[loser], batches[loser], won.snapshot.snapshot_id
        )
        assert retried.outcome is CommitOutcome.COMMITTED
        assert retried.snapshot.parent_snapshot_id == won.snapshot.snapshot_id
        expected += [batches[winners[0]], batches[loser]]
        parent = retried.snapshot.snapshot_id

    catalog = pg_harness.sql_catalog()
    assert len(catalog.load_table(("c2test", "alpha")).snapshots()) == 2 * rounds
    assert rows_of(_scan(catalog, ALPHA.table)) == rows_of(pa.concat_tables(expected))


@pytest.mark.parametrize("stale_loads", [1, 2], ids=["requirement-check", "metadata-pointer-cas"])
def test_real_pyiceberg_commit_failure_maps_to_commit_conflict(
    pg_harness: PostgresCatalogHarness, monkeypatch: pytest.MonkeyPatch, stale_loads: int
) -> None:
    """Writer two read the table before writer one committed (the race window, made explicit).

    ``stale_loads=1``: only the adapter's own read is stale, PyIceberg's commit re-read sees the
    new head and fails ``AssertRefSnapshotId``. ``stale_loads=2``: PyIceberg's commit re-read is
    stale too, so the PostgreSQL compare-and-swap on ``metadata_location`` matches zero rows.
    """
    winner_adapter = pg_harness.open_adapter()
    winner_adapter.create_table(ALPHA.binding)
    loser_catalog = pg_harness.sql_catalog()
    loser_adapter = PyIcebergCatalogAdapter(loser_catalog, pg_harness.registry)
    stale = loser_catalog.load_table(("c2test", "alpha"))
    batch_x, batch_y = make_batch(2, "x"), make_batch(3, "y")
    winner = _commit(winner_adapter, ALPHA.table, "batch-x", batch_x, None).snapshot

    original_load = loser_catalog.load_table
    original_commit = loser_catalog.commit_table
    loads = {"count": 0}
    failures: list[BaseException] = []

    def load_table(identifier: Any) -> IcebergTable:
        loads["count"] += 1
        if loads["count"] <= stale_loads:
            return IcebergTable(
                identifier=stale.name(),
                metadata=stale.metadata,
                metadata_location=stale.metadata_location,
                io=stale.io,
                catalog=loser_catalog,
            )
        return original_load(identifier)

    def commit_table(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_commit(*args, **kwargs)
        except BaseException as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(loser_catalog, "load_table", load_table)
    monkeypatch.setattr(loser_catalog, "commit_table", commit_table)
    with pytest.raises(CommitConflict):
        _commit(loser_adapter, ALPHA.table, "batch-y", batch_y, None)
    assert len(failures) == 1 and isinstance(failures[0], CommitFailedException)
    marker = "updated by another process" if stale_loads == 2 else "Requirement failed"
    assert marker in str(failures[0])
    monkeypatch.undo()

    checker = pg_harness.sql_catalog()
    assert [s.snapshot_id for s in checker.load_table(("c2test", "alpha")).snapshots()] == [
        int(winner.snapshot_id)
    ]
    retried = _commit(loser_adapter, ALPHA.table, "batch-y", batch_y, winner.snapshot_id)
    assert retried.outcome is CommitOutcome.COMMITTED
    assert rows_of(_scan(checker, ALPHA.table)) == rows_of(pa.concat_tables([batch_x, batch_y]))


def test_catalog_database_holds_only_iceberg_metadata(pg_harness: PostgresCatalogHarness) -> None:
    marker = f"PAYLOAD-{uuid.uuid4().hex}"
    adapter = pg_harness.open_adapter()
    for definition in (ALPHA, BETA):
        adapter.create_table(definition.binding)
        batch = make_batch(5, marker)
        _commit(adapter, definition.table, f"{marker}-batch", batch, None)
    catalog = pg_harness.sql_catalog()

    user_tables = _sql(
        catalog,
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2",
    )
    assert user_tables == [("public", "iceberg_namespace_properties"), ("public", "iceberg_tables")]
    everything = _sql(catalog, "SELECT * FROM iceberg_tables") + _sql(
        catalog, "SELECT * FROM iceberg_namespace_properties"
    )
    assert everything, "catalog database should hold the metadata pointers"
    assert not [row for row in everything if marker in repr(row)], "payload / batch id leaked"
    pointers = _sql(
        catalog,
        "SELECT table_namespace, table_name, metadata_location FROM iceberg_tables "
        f"WHERE catalog_name = '{pg_harness.catalog_name}' ORDER BY 1, 2",
    )
    assert [row[:2] for row in pointers] == [("c2test", "alpha"), ("c2test", "beta")]
    for _, _, location in pointers:
        assert location.startswith(pg_harness.warehouse_uri + "/")
        assert location.endswith(".metadata.json")

    # Parquet data lives only in the local file:// warehouse.
    for definition in (ALPHA, BETA):
        iceberg = catalog.load_table(tuple(definition.table.split(".")))
        paths = [task.file.file_path for task in iceberg.scan().plan_files()]
        assert paths and all(p.startswith(pg_harness.warehouse_uri + "/") for p in paths)
        assert all(p.endswith(".parquet") for p in paths)
    assert list(pg_harness.warehouse.rglob("*.parquet"))


def test_catalog_role_is_least_privilege_and_isolated(pg_harness: PostgresCatalogHarness) -> None:
    catalog = pg_harness.sql_catalog()
    [(database, role, *flags)] = _sql(
        catalog,
        "SELECT current_database(), current_user, rolsuper, rolcreatedb, rolcreaterole, "
        "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user",
    )
    assert database.endswith("_test") and role.startswith("hlens_iceberg_catalog")
    assert flags == [False, False, False, False, False]
    connectable = {
        name
        for (name,) in _sql(
            catalog,
            "SELECT datname FROM pg_database "
            "WHERE has_database_privilege(current_user, datname, 'CONNECT')",
        )
    }
    assert {name for name in connectable if name.startswith("hlens")} == {database}
    assert _sql(catalog, "SELECT datname FROM pg_database WHERE datname = 'hlens_control'") == []


def test_persisted_binding_is_rechecked_after_restart(pg_harness: PostgresCatalogHarness) -> None:
    writer = pg_harness.open_adapter()
    writer.create_table(ALPHA.binding)
    _commit(writer, ALPHA.table, "batch-a", make_batch(2, "a"), None)
    writer.close()

    # A restarted process whose registry lacks the stored binding cannot use the table.
    without_alpha = TableDefinitionRegistry((BETA, ALPHA_V110))
    restarted = pg_harness.open_adapter(without_alpha)
    with pytest.raises(UnknownTableDefinition):
        restarted.load_table(ALPHA.table)
    with pytest.raises(UnknownTableDefinition):
        _commit(restarted, ALPHA.table, "batch-b", make_batch(2, "b"), None)

    # Same id / version but different registered content: the derived hash no longer matches.
    drifted = RegisteredTableDefinition(
        table=ALPHA.table,
        definition_id=ALPHA.definition_id,
        version=ALPHA.version,
        schema=ALPHA_V110.schema,
        fingerprint_rule=RULE,
    )
    with pytest.raises(UnknownTableDefinition):
        pg_harness.open_adapter(TableDefinitionRegistry((drifted,))).load_table(ALPHA.table)

    # Stored metadata tampered through PyIceberg (retries re-enabled): fail closed.
    iceberg = pg_harness.sql_catalog().load_table(("c2test", "alpha"))
    with iceberg.transaction() as transaction:
        transaction.set_properties({"commit.retry.num-retries": "4"})
    with pytest.raises(CatalogIntegrityError):
        pg_harness.open_adapter().load_table(ALPHA.table)
