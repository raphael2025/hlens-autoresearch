"""Temporary-catalog tests for the additive State table (ADR-0089)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError
from pyiceberg.transforms import DayTransform

from core.contracts.catalog import CommitRequest, TableNotFound, UnknownTableDefinition
from core.contracts.state import StateResult
from infrastructure.catalog.iceberg_adapter import PyIcebergCatalogAdapter
from infrastructure.state.iceberg import (
    StateTable,
    StateTableConflict,
    StateTableCorrupted,
    StateTableError,
    state_batch_id,
    state_rows,
    state_rows_batch,
)
from infrastructure.state.table_definition import (
    PHASE2_REGISTRY,
    PHASE2_TABLES,
    STATE_STATES,
    ensure_state_tables,
)
from infrastructure.state.table import STATE_TABLE_SCHEMA
from infrastructure.state.runner import run_state, state_request
from plugins.states import VolatilityRegimeProvider
from tests.fake_states import TEST_CUTS, TEST_MIN_HISTORY, TEST_SEED, TEST_WINDOW, VOL_FEATURE, at, level_inputs
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness


def _answer() -> tuple[Any, Any, Any, Any]:
    spec = VolatilityRegimeProvider.spec(
        VOL_FEATURE,
        cuts=TEST_CUTS,
        min_history=TEST_MIN_HISTORY,
        training_window=TEST_WINDOW,
        seed=TEST_SEED,
    )
    provider = VolatilityRegimeProvider((spec,))
    request = state_request(spec, (at(4), at(5), at(6)), level_inputs(VOL_FEATURE, 7))
    return spec, request, run_state(provider, spec, request), provider.descriptor


@dataclass
class Env:
    harness: SqliteCatalogHarness
    adapter: PyIcebergCatalogAdapter
    table: StateTable

    def head(self) -> str | None:
        info = self.adapter.load_table(STATE_STATES.table)
        assert info is not None
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def commit_rows(self, rows: list[dict[str, Any]], batch_id: str) -> str:
        batch = pa.Table.from_pylist(rows, schema=STATE_STATES.arrow_schema)
        committed = self.adapter.commit_batch(
            CommitRequest(
                table=STATE_STATES.table,
                batch_id=batch_id,
                batch_fingerprint=STATE_STATES.fingerprint_rule.fingerprint(batch),
                row_count=len(rows),
                expected_parent_snapshot_id=self.head(),
            ),
            batch,
        )
        return committed.snapshot.snapshot_id


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    harness = SqliteCatalogHarness(tmp_path, registry=PHASE2_REGISTRY)
    adapter = harness.open_adapter()
    ensure_state_tables(adapter)
    yield Env(harness, adapter, StateTable(adapter))
    harness.cleanup()


def test_definition_retains_logical_columns_and_declares_daily_partition() -> None:
    assert PHASE2_TABLES == (STATE_STATES,)
    assert tuple(field.name for field in STATE_STATES.arrow_schema[:9]) == tuple(
        field.name for field in STATE_TABLE_SCHEMA
    )
    assert tuple(field.name for field in STATE_STATES.arrow_schema[9:]) == (
        "provider_hash",
        "evaluation_index",
        "evaluation_count",
        "run_schema_version",
    )
    (partition,) = STATE_STATES.partition_spec.fields
    assert isinstance(partition.transform, DayTransform)
    assert STATE_STATES.schema.find_column_name(partition.source_id) == "evaluation_time"


def test_phase1_registry_does_not_gain_the_state_table() -> None:
    from infrastructure.catalog.phase1_tables import PHASE1_REGISTRY, PHASE1_TABLES

    assert STATE_STATES not in PHASE1_TABLES
    assert len(PHASE2_REGISTRY) == 1
    with pytest.raises(UnknownTableDefinition):
        PHASE1_REGISTRY.resolve(STATE_STATES.binding)


def test_ensure_and_run_round_trip_are_idempotent_and_snapshot_pinned(env: Env) -> None:
    again = ensure_state_tables(env.adapter)
    assert len(again) == 1 and not again[0].created
    assert again[0].partition == "day(evaluation_time)"
    spec, request, result, descriptor = _answer()
    rows = state_rows(result, spec, request, descriptor)
    assert [row["evaluation_index"] for row in rows] == [0, 1, 2]
    assert {row["evaluation_count"] for row in rows} == {3}
    assert state_rows_batch(result, spec, request, descriptor).schema.equals(
        STATE_STATES.arrow_schema, check_metadata=False
    )

    written = env.table.write(spec, request, result, descriptor)
    replay = env.table.write(spec, request, result, descriptor)
    loaded = env.table.read(result.result_hash, snapshot_id=written.snapshot_id)
    assert written.snapshot_id == replay.snapshot_id == env.head()
    assert not written.replayed and replay.replayed
    assert loaded is not None and loaded.result == result
    assert loaded.snapshot_id == written.snapshot_id
    assert env.adapter.get_snapshot(STATE_STATES.table, written.snapshot_id).batch_id == state_batch_id(
        result.result_hash
    )


def test_writer_refuses_a_missing_table(tmp_path: Path) -> None:
    harness = SqliteCatalogHarness(tmp_path, registry=PHASE2_REGISTRY)
    try:
        spec, request, result, descriptor = _answer()
        table = StateTable(harness.open_adapter())
        with pytest.raises(TableNotFound):
            table.write(spec, request, result, descriptor)
        with pytest.raises(TableNotFound):
            table.read(result.result_hash)
    finally:
        harness.cleanup()


def test_conflicting_existing_rows_fail_closed(env: Env) -> None:
    spec, request, result, descriptor = _answer()
    rows = state_rows(result, spec, request, descriptor)
    rows[0]["state"] = "tampered"
    snapshot = env.commit_rows(rows, "state.tamper")
    with pytest.raises(StateTableConflict, match="never overwritten"):
        env.table.write(spec, request, result, descriptor)
    assert env.head() == snapshot
    with pytest.raises(StateTableCorrupted):
        env.table.read(result.result_hash)


def test_rebuild_rejects_missing_or_duplicate_run_indices(env: Env) -> None:
    spec, request, result, descriptor = _answer()
    rows = state_rows(result, spec, request, descriptor)
    rows[1]["evaluation_index"] = 0
    env.commit_rows(rows, "state.duplicate-index")
    with pytest.raises(StateTableCorrupted, match="evaluation_index"):
        env.table.read(result.result_hash)


def test_empty_state_result_is_refused_by_the_published_contract() -> None:
    _spec, request, _result, descriptor = _answer()
    with pytest.raises(ValidationError):
        StateResult.build(request, descriptor, ())


def test_run_projection_refuses_mixed_value_schema_versions() -> None:
    spec, request, result, descriptor = _answer()
    changed = result.model_copy(
        update={"values": (result.values[0].model_copy(update={"schema_version": "99.0.0"}), *result.values[1:])}
    )
    with pytest.raises(StateTableError, match="mixes schema versions"):
        state_rows_batch(changed, spec, request, descriptor)
