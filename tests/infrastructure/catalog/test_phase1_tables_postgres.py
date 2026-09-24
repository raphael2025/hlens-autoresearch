"""PostgreSQL evidence for C3 (roadmap Phase 1 #9): eight tables, batches, evolution, isolation.

Runs only with ``HLENS_TEST_CATALOG_URI`` naming the dedicated ``*_test`` database (explicit skip
otherwise). Each test uses its own PyIceberg ``catalog_name`` and ``tmp_path`` warehouse; cleanup
drops the tables / namespaces through PyIceberg and deletes the warehouse files. Direct SQL is
read-only verification.

Writes to ``day(...)``-partitioned tables need PyIceberg's optional ``pyiceberg-core`` extension,
which is not in the locked dependency set (decision D-32, ``PROJECT_STATUS.md`` §6). Those paths
first assert that the failed write left the table unchanged, then report ``xfail``; once the
extension is locked they run in full.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import CommitFailedException, NotInstalledError
from pyiceberg.table import Table as IcebergTable
from pyiceberg.transforms import IdentityTransform

from core.contracts.catalog import (
    BatchRejected,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    TableDefinitionConflict,
    UnknownTableDefinition,
)
from infrastructure.catalog import (
    PHASE1_REGISTRY,
    PHASE1_TABLES,
    PYARROW_BATCH_FINGERPRINT,
    CatalogIntegrityError,
    DefinitionEvolutionError,
    EvolutionOutcome,
    PyIcebergCatalogAdapter,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    ensure_phase1_tables,
    open_postgres_catalog_adapter,
)
from infrastructure.catalog.definitions import (
    PROPERTY_DEFINITION_HASH,
    PROPERTY_DEFINITION_ID,
    PROPERTY_DEFINITION_VERSION,
    PROPERTY_FINGERPRINT_RULE,
)
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_ARCHIVES, CANONICAL_BARS_1M
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    postgres_test_catalog_uri,
)
from tests.infrastructure.catalog.phase1_support import (
    ARCHIVES_BY_SYMBOL,
    BARS_MONTH,
    EVOLUTION_REGISTRY,
    archive_row,
    archives_by_symbol_target,
    batch_for,
    logical_rows,
    minimal_batch,
)
from tests.infrastructure.catalog.test_phase1_tables import FROZEN_PARTITIONS, FROZEN_TABLES

pytestmark = pytest.mark.postgres

HAS_PYICEBERG_CORE = importlib.util.find_spec("pyiceberg_core") is not None
DAY_PARTITIONED = {table for table, spec in FROZEN_PARTITIONS.items() if spec}
D32 = "D-32: day() partition writes need pyiceberg-core, which is not in the locked dependencies"
fingerprint = PYARROW_BATCH_FINGERPRINT.fingerprint


def _identifier(table: str) -> tuple[str, str]:
    namespace, name = table.split(".")
    return namespace, name


def _request(table: str, batch_id: str, batch: pa.Table, parent: str | None) -> CommitRequest:
    return CommitRequest(
        table=table,
        batch_id=batch_id,
        batch_fingerprint=fingerprint(batch),
        row_count=batch.num_rows,
        expected_parent_snapshot_id=parent,
    )


def _commit(
    adapter: PyIcebergCatalogAdapter, table: str, batch_id: str, batch: pa.Table, parent: str | None
) -> CommitResult:
    return adapter.commit_batch(_request(table, batch_id, batch, parent), batch)


def _scan(
    catalog: SqlCatalog, table: str, snapshot: SnapshotInfo | None = None, row_filter: Any = None
) -> pa.Table:
    iceberg = catalog.load_table(_identifier(table))
    kwargs: dict[str, Any] = {}
    if snapshot is not None:
        kwargs["snapshot_id"] = int(snapshot.snapshot_id)
    if row_filter is not None:
        kwargs["row_filter"] = row_filter
    return iceberg.scan(**kwargs).to_arrow()


def _sql(catalog: SqlCatalog, statement: str) -> list[tuple[Any, ...]]:
    with catalog.engine.connect() as connection:
        return [tuple(row) for row in connection.exec_driver_sql(statement).all()]


def _state(iceberg: IcebergTable) -> tuple[Any, ...]:
    """Everything an evolution may change; equal states mean the table did not change."""
    metadata = iceberg.metadata
    return (
        iceberg.metadata_location,
        metadata.default_spec_id,
        sorted(metadata.specs()),
        dict(iceberg.properties),
        [s.snapshot_id for s in iceberg.snapshots()],
    )


@pytest.fixture
def harness_factory(tmp_path: Path) -> Iterator[Callable[[str], PostgresCatalogHarness]]:
    """Extra PostgreSQL harnesses (unique catalog names / warehouses), cleaned up afterwards."""
    created: list[PostgresCatalogHarness] = []

    def make(label: str) -> PostgresCatalogHarness:
        harness = PostgresCatalogHarness(tmp_path / label, uri=postgres_test_catalog_uri())
        created.append(harness)
        return harness

    yield make
    for harness in created:
        harness.cleanup()


# --------------------------------------------------------------------------- creation


def test_eight_tables_are_created_idempotently_with_the_frozen_layout(
    pg_harness: PostgresCatalogHarness,
) -> None:
    adapter = open_postgres_catalog_adapter(pg_harness.settings(), PHASE1_REGISTRY)
    try:
        states = ensure_phase1_tables(adapter)
    finally:
        adapter.close()
    assert [s.table for s in states] == list(FROZEN_TABLES)
    assert all(s.created and s.current_snapshot_id is None for s in states)

    catalog = pg_harness.sql_catalog()
    assert sorted(catalog.list_namespaces()) == [
        ("canonical",),
        ("quality",),
        ("raw",),
        ("research",),
    ]
    locations = {}
    for definition in PHASE1_TABLES:
        iceberg = catalog.load_table(_identifier(definition.table))
        props = iceberg.properties
        assert props[PROPERTY_DEFINITION_ID] == definition.table
        assert props[PROPERTY_DEFINITION_VERSION] == "1.0.0"
        assert props[PROPERTY_DEFINITION_HASH] == definition.definition_hash
        assert props[PROPERTY_FINGERPRINT_RULE] == "hlens.pyarrow-batch-sha256@1.0.0"
        assert props["commit.retry.num-retries"] == "0"
        assert props["write.parquet.compression-codec"] == "zstd"
        assert iceberg.metadata.format_version == 2
        assert iceberg.schema().model_dump_json() == definition.schema.model_dump_json()
        spec = iceberg.spec()
        assert spec.spec_id == 0
        assert [
            (f.field_id, str(f.transform), iceberg.schema().find_column_name(f.source_id), f.name)
            for f in spec.fields
        ] == FROZEN_PARTITIONS[definition.table]
        assert iceberg.snapshots() == []
        locations[definition.table] = iceberg.metadata_location

    # Same definitions again, including from a restarted process: verified, nothing changes.
    restarted = pg_harness.open_adapter(PHASE1_REGISTRY)
    again = ensure_phase1_tables(restarted)
    assert [s.definition for s in again] == [s.definition for s in states]
    assert not any(s.created for s in again)
    checker = pg_harness.sql_catalog()
    for table, location in locations.items():
        assert checker.load_table(_identifier(table)).metadata_location == location

    # Drift in an existing table fails closed and changes nothing else.
    tampered = checker.load_table(_identifier("canonical.trades"))
    with tampered.transaction() as transaction:
        transaction.set_properties({"write.parquet.compression-codec": "snappy"})
    with pytest.raises(CatalogIntegrityError):
        ensure_phase1_tables(pg_harness.open_adapter(PHASE1_REGISTRY))
    # A different registered binding for an existing table is a conflict, not a replacement.
    other = RegisteredTableDefinition(
        table=BINANCE_SPOT_ARCHIVES.table,
        definition_id=BINANCE_SPOT_ARCHIVES.definition_id,
        version="9.0.0",
        schema=BINANCE_SPOT_ARCHIVES.schema,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        properties={"write.parquet.compression-codec": "snappy"},
    )
    conflicting = pg_harness.open_adapter(TableDefinitionRegistry((*PHASE1_TABLES, other)))
    with pytest.raises(TableDefinitionConflict):
        conflicting.create_table(other.binding)
    final = pg_harness.sql_catalog()
    for table, location in locations.items():
        if table != "canonical.trades":
            assert final.load_table(_identifier(table)).metadata_location == location


# --------------------------------------------------------------------------- batches


@pytest.mark.parametrize("table", FROZEN_TABLES)
def test_each_table_appends_replays_restarts_and_time_travels(
    pg_harness: PostgresCatalogHarness, table: str
) -> None:
    definition = next(d for d in PHASE1_TABLES if d.table == table)
    adapter = pg_harness.open_adapter(PHASE1_REGISTRY)
    adapter.create_table(definition.binding)
    batch_a, batch_b = minimal_batch(definition, "a"), minimal_batch(definition, "b")
    assert fingerprint(batch_a) != fingerprint(batch_b)

    # Caller-supplied fake fingerprint: rejected on the first commit, nothing written.
    fake = CommitRequest(
        table=table,
        batch_id="batch-a",
        batch_fingerprint=fingerprint(batch_b),
        row_count=1,
        expected_parent_snapshot_id=None,
    )
    with pytest.raises(BatchRejected):
        adapter.commit_batch(fake, batch_a)
    assert pg_harness.sql_catalog().load_table(_identifier(table)).snapshots() == []

    if table in DAY_PARTITIONED and not HAS_PYICEBERG_CORE:
        before = _state(pg_harness.sql_catalog().load_table(_identifier(table)))
        with pytest.raises(NotInstalledError, match="pyiceberg_core"):
            _commit(adapter, table, "batch-a", batch_a, None)
        assert _state(pg_harness.sql_catalog().load_table(_identifier(table))) == before
        assert adapter.load_table(table) is not None
        pytest.xfail(D32)

    first = _commit(adapter, table, "batch-a", batch_a, None)
    assert first.outcome is CommitOutcome.COMMITTED and first.snapshot.added_rows == 1
    replay = _commit(adapter, table, "batch-a", batch_a, None)
    assert replay.outcome is CommitOutcome.ALREADY_COMMITTED and replay.snapshot == first.snapshot
    # The replay path verifies the actual batch too: a fake claim is still rejected.
    with pytest.raises(BatchRejected):
        adapter.commit_batch(fake, batch_a)
    second = _commit(adapter, table, "batch-b", batch_b, first.snapshot.snapshot_id).snapshot
    adapter.close()

    restarted = pg_harness.open_adapter(PHASE1_REGISTRY)
    info = restarted.load_table(table)
    assert info is not None and info.definition == definition.binding
    assert info.current_snapshot == second
    assert restarted.get_snapshot(table, first.snapshot.snapshot_id) == first.snapshot
    again = _commit(restarted, table, "batch-a", batch_a, second.snapshot_id)
    assert again.outcome is CommitOutcome.ALREADY_COMMITTED and again.snapshot == first.snapshot
    with pytest.raises(BatchRejected):
        restarted.commit_batch(fake, batch_a)

    catalog = pg_harness.sql_catalog()
    assert logical_rows(_scan(catalog, table, first.snapshot)) == logical_rows(batch_a)
    both = pa.concat_tables([batch_a, batch_b])
    assert logical_rows(_scan(catalog, table, second)) == logical_rows(both)
    assert logical_rows(_scan(catalog, table)) == logical_rows(both)
    assert len(catalog.load_table(_identifier(table)).snapshots()) == 2


def test_catalog_database_holds_only_iceberg_metadata(pg_harness: PostgresCatalogHarness) -> None:
    marker = f"C3PAYLOAD{uuid.uuid4().hex}"
    adapter = pg_harness.open_adapter(PHASE1_REGISTRY)
    ensure_phase1_tables(adapter)
    written = [d for d in PHASE1_TABLES if d.table not in DAY_PARTITIONED or HAS_PYICEBERG_CORE]
    for definition in written:
        batch = minimal_batch(definition, marker)
        assert marker in repr(batch.to_pylist())
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
    assert everything and not [row for row in everything if marker in repr(row)]
    pointers = _sql(
        catalog,
        "SELECT table_namespace || '.' || table_name, metadata_location FROM iceberg_tables "
        f"WHERE catalog_name = '{pg_harness.catalog_name}' ORDER BY 1",
    )
    assert [row[0] for row in pointers] == sorted(FROZEN_TABLES)
    for _, location in pointers:
        assert location.startswith(pg_harness.warehouse_uri + "/")
    for definition in written:
        iceberg = catalog.load_table(_identifier(definition.table))
        paths = [task.file.file_path for task in iceberg.scan().plan_files()]
        assert paths and all(p.startswith(pg_harness.warehouse_uri + "/") for p in paths)
        assert all(p.endswith(".parquet") for p in paths)
        assert marker in repr(iceberg.scan().to_arrow().to_pylist())


def test_cleanup_leaves_no_catalog_rows_or_files(
    harness_factory: Callable[[str], PostgresCatalogHarness],
) -> None:
    scratch = harness_factory("scratch")
    adapter = scratch.open_adapter(PHASE1_REGISTRY)
    ensure_phase1_tables(adapter)
    batch = minimal_batch(BINANCE_SPOT_ARCHIVES)
    _commit(adapter, BINANCE_SPOT_ARCHIVES.table, "b", batch, None)
    adapter.close()
    assert list(scratch.warehouse.rglob("*.parquet"))
    scratch.cleanup()

    observer = harness_factory("observer").sql_catalog()
    name = scratch.catalog_name
    for statement in (
        f"SELECT count(*) FROM iceberg_tables WHERE catalog_name = '{name}'",
        f"SELECT count(*) FROM iceberg_namespace_properties WHERE catalog_name = '{name}'",
    ):
        assert _sql(observer, statement) == [(0,)]
    assert not scratch.warehouse.exists()


# --------------------------------------------------------------------------- evolution


def _archive_batch(tag: str, days: list[int], symbols: tuple[str, ...]) -> pa.Table:
    start = datetime(2024, 12, 28, tzinfo=UTC)
    rows = [
        archive_row(f"{tag}-{symbol}-{day}", symbol=symbol, start=start + timedelta(days=day))
        for day in days
        for symbol in symbols
    ]
    return batch_for(BINANCE_SPOT_ARCHIVES, rows)


def test_partition_evolution_keeps_old_and_new_specs_query_equivalent(
    harness_factory: Callable[[str], PostgresCatalogHarness],
) -> None:
    """Old-spec data, evolution, new-spec data; compared against a never-evolved control."""
    table = BINANCE_SPOT_ARCHIVES.table
    symbols = ("BTCUSDT", "ETHUSDT")
    batch_a = _archive_batch("a", [0, 1, 2], symbols)
    batch_b = _archive_batch("b", [2, 3, 4], symbols)

    control_h, evolved_h = harness_factory("control"), harness_factory("evolved")
    control = control_h.open_adapter(EVOLUTION_REGISTRY)
    evolved = evolved_h.open_adapter(EVOLUTION_REGISTRY)
    snapshots: dict[str, list[SnapshotInfo]] = {}
    for label, adapter in (("control", control), ("evolved", evolved)):
        adapter.create_table(BINANCE_SPOT_ARCHIVES.binding)
        first = _commit(adapter, table, "batch-a", batch_a, None).snapshot
        if label == "evolved":
            result = adapter.evolve_partition_spec(
                BINANCE_SPOT_ARCHIVES.binding, ARCHIVES_BY_SYMBOL.binding
            )
            assert result.outcome is EvolutionOutcome.EVOLVED and result.spec_id == 1
            info = adapter.load_table(table)
            assert info is not None and info.current_snapshot == first  # no data rewrite
        second = _commit(adapter, table, "batch-b", batch_b, first.snapshot_id).snapshot
        snapshots[label] = [first, second]
    control.close()
    evolved.close()

    iceberg = evolved_h.sql_catalog().load_table(_identifier(table))
    specs = iceberg.metadata.specs()
    assert sorted(specs) == [0, 1] and iceberg.metadata.default_spec_id == 1
    assert specs[0].fields == () and specs[1].fields == ARCHIVES_BY_SYMBOL.partition_spec.fields
    assert isinstance(specs[1].fields[0].transform, IdentityTransform)
    files_by_spec: dict[int, set[str]] = {}
    for task in iceberg.scan().plan_files():
        files_by_spec.setdefault(task.file.spec_id, set()).add(repr(task.file.partition))
    assert files_by_spec == {0: {"Record[]"}, 1: {"Record[BTCUSDT]", "Record[ETHUSDT]"}}
    assert iceberg.properties[PROPERTY_DEFINITION_VERSION] == "1.1.0"
    assert iceberg.properties[PROPERTY_DEFINITION_HASH] == ARCHIVES_BY_SYMBOL.definition_hash

    control_catalog, evolved_catalog = control_h.sql_catalog(), evolved_h.sql_catalog()
    cutoff = "'2024-12-30T00:00:00+00:00'"
    filters = [
        None,
        "symbol == 'ETHUSDT'",
        f"coverage_start >= {cutoff}",
        f"symbol == 'BTCUSDT' and coverage_start >= {cutoff}",
    ]
    everything = logical_rows(pa.concat_tables([batch_a, batch_b]))
    for row_filter in filters:
        for index in (0, 1):
            left = _scan(control_catalog, table, snapshots["control"][index], row_filter)
            right = _scan(evolved_catalog, table, snapshots["evolved"][index], row_filter)
            assert logical_rows(left) == logical_rows(right)
        current = logical_rows(_scan(evolved_catalog, table, None, row_filter))
        assert current == logical_rows(_scan(control_catalog, table, None, row_filter))
    eth = [row for row in everything if row["symbol"] == "ETHUSDT"]
    assert logical_rows(_scan(evolved_catalog, table, None, filters[1])) == eth

    # Restart with the target registry: loads, time-travels, replays batches across the change.
    restarted = evolved_h.open_adapter(EVOLUTION_REGISTRY)
    info = restarted.load_table(table)
    assert info is not None and info.definition == ARCHIVES_BY_SYMBOL.binding
    first, second = snapshots["evolved"]
    assert restarted.get_snapshot(table, first.snapshot_id) == first
    replay = _commit(restarted, table, "batch-a", batch_a, second.snapshot_id)
    assert replay.outcome is CommitOutcome.ALREADY_COMMITTED and replay.snapshot == first
    again = restarted.evolve_partition_spec(
        BINANCE_SPOT_ARCHIVES.binding, ARCHIVES_BY_SYMBOL.binding
    )
    assert again.outcome is EvolutionOutcome.ALREADY_EVOLVED
    # The old source binding no longer opens or re-creates the table.
    with pytest.raises(TableDefinitionConflict):
        restarted.create_table(BINANCE_SPOT_ARCHIVES.binding)
    with pytest.raises(UnknownTableDefinition):
        evolved_h.open_adapter(PHASE1_REGISTRY).load_table(table)


def test_day_partitioned_table_evolves_to_month_in_metadata(
    pg_harness: PostgresCatalogHarness,
) -> None:
    table = CANONICAL_BARS_1M.table
    adapter = pg_harness.open_adapter(EVOLUTION_REGISTRY)
    adapter.create_table(CANONICAL_BARS_1M.binding)
    result = adapter.evolve_partition_spec(CANONICAL_BARS_1M.binding, BARS_MONTH.binding)
    assert result.outcome is EvolutionOutcome.EVOLVED
    iceberg = pg_harness.sql_catalog().load_table(_identifier(table))
    specs = iceberg.metadata.specs()
    assert specs[0].fields == CANONICAL_BARS_1M.partition_spec.fields
    assert specs[1].fields == BARS_MONTH.partition_spec.fields
    assert [f.field_id for f in specs[1].fields] == [1000, 1002]
    assert iceberg.metadata.last_partition_id == 1002
    restarted = pg_harness.open_adapter(EVOLUTION_REGISTRY)
    info = restarted.load_table(table)
    assert info is not None and info.definition == BARS_MONTH.binding
    if not HAS_PYICEBERG_CORE:
        pytest.xfail(D32)
    rows = minimal_batch(CANONICAL_BARS_1M, "after-evolution")
    assert _commit(restarted, table, "b", rows, None).outcome is CommitOutcome.COMMITTED


def _evolvable(pg_harness: PostgresCatalogHarness, registry: TableDefinitionRegistry) -> Any:
    adapter = pg_harness.open_adapter(registry)
    adapter.create_table(BINANCE_SPOT_ARCHIVES.binding)
    _commit(adapter, BINANCE_SPOT_ARCHIVES.table, "a", _archive_batch("a", [0], ("BTCUSDT",)), None)
    return adapter


def _archives_state(pg_harness: PostgresCatalogHarness) -> tuple[Any, ...]:
    return _state(pg_harness.sql_catalog().load_table(_identifier(BINANCE_SPOT_ARCHIVES.table)))


def test_failed_commit_leaves_the_source_definition(
    pg_harness: PostgresCatalogHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = pg_harness.sql_catalog()
    adapter = PyIcebergCatalogAdapter(catalog, EVOLUTION_REGISTRY)
    adapter.create_table(BINANCE_SPOT_ARCHIVES.binding)
    before = _archives_state(pg_harness)
    calls: list[int] = []

    def failing_commit(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise CommitFailedException("injected: table was updated by another process")

    monkeypatch.setattr(catalog, "commit_table", failing_commit)
    with pytest.raises(CommitConflict):
        adapter.evolve_partition_spec(BINANCE_SPOT_ARCHIVES.binding, ARCHIVES_BY_SYMBOL.binding)
    monkeypatch.undo()
    assert calls == [1]  # exactly one metadata commit attempt, no retry
    assert _archives_state(pg_harness) == before
    info = pg_harness.open_adapter(PHASE1_REGISTRY).load_table(BINANCE_SPOT_ARCHIVES.table)
    assert info is not None and info.definition == BINANCE_SPOT_ARCHIVES.binding
    # After the failure the same request succeeds as one commit.
    result = adapter.evolve_partition_spec(
        BINANCE_SPOT_ARCHIVES.binding, ARCHIVES_BY_SYMBOL.binding
    )
    assert result.outcome is EvolutionOutcome.EVOLVED


def test_invalid_evolutions_are_rejected_and_change_nothing(
    pg_harness: PostgresCatalogHarness,
) -> None:
    wrong_ids = archives_by_symbol_target(field_id=1001)
    other_source = RegisteredTableDefinition(
        table=BINANCE_SPOT_ARCHIVES.table,
        definition_id=BINANCE_SPOT_ARCHIVES.definition_id,
        version="1.0.5",
        schema=BINANCE_SPOT_ARCHIVES.schema,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        properties={"write.parquet.compression-codec": "snappy"},
    )
    other_target = RegisteredTableDefinition(
        table=other_source.table,
        definition_id=other_source.definition_id,
        version="1.1.5",
        schema=other_source.schema,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        partition_spec=ARCHIVES_BY_SYMBOL.partition_spec,
        properties=other_source.properties,
        evolves_from=other_source.binding,
    )
    registry = TableDefinitionRegistry(
        (*PHASE1_TABLES, BARS_MONTH, wrong_ids, other_source, other_target)
    )
    adapter = _evolvable(pg_harness, registry)
    before = _archives_state(pg_harness)
    source = BINANCE_SPOT_ARCHIVES.binding

    cases: list[tuple[type[Exception], Any, Any]] = [
        # Iceberg would assign partition field 1000, the target declares 1001.
        (DefinitionEvolutionError, source, wrong_ids.binding),
        # Stored table is not at the requested (registered) source.
        (TableDefinitionConflict, other_source.binding, other_target.binding),
        # Target of another table.
        (DefinitionEvolutionError, source, BARS_MONTH.binding),
        # Target that does not evolve from this source.
        (DefinitionEvolutionError, source, other_target.binding),
        # Unregistered target (e.g. a schema change the registry would refuse).
        (UnknownTableDefinition, source, ARCHIVES_BY_SYMBOL.binding),
        # Source and target swapped.
        (DefinitionEvolutionError, wrong_ids.binding, source),
    ]
    for error, requested_source, requested_target in cases:
        with pytest.raises(error):
            adapter.evolve_partition_spec(requested_source, requested_target)
        assert _archives_state(pg_harness) == before
    with pytest.raises(DefinitionEvolutionError):
        adapter.create_table(BARS_MONTH.binding)  # targets are never created directly
    assert pg_harness.sql_catalog().table_exists(_identifier(CANONICAL_BARS_1M.table)) is False


@pytest.mark.parametrize("tamper", ["property", "spec"])
def test_tampered_source_is_rejected(pg_harness: PostgresCatalogHarness, tamper: str) -> None:
    adapter = _evolvable(pg_harness, EVOLUTION_REGISTRY)
    iceberg = pg_harness.sql_catalog().load_table(_identifier(BINANCE_SPOT_ARCHIVES.table))
    with iceberg.transaction() as transaction:
        if tamper == "property":
            transaction.set_properties({"write.parquet.compression-codec": "snappy"})
        else:
            with transaction.update_spec() as update:
                update.add_identity("data_type")
    before = _archives_state(pg_harness)
    with pytest.raises(CatalogIntegrityError):
        adapter.evolve_partition_spec(BINANCE_SPOT_ARCHIVES.binding, ARCHIVES_BY_SYMBOL.binding)
    assert _archives_state(pg_harness) == before
