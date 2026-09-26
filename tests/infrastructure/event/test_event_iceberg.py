"""Phase 3: the physical Event table ``event.events`` (ADR-0056) on a temporary SQLite catalog.

SQLite results are unit evidence only (never PostgreSQL evidence); no real warehouse is touched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.transforms import MonthTransform

from core.contracts.catalog import (
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotNotFound,
    TableNotFound,
    UnknownTableDefinition,
)
from core.contracts.event import EventResult
from core.domain.base import CONTRACT_SCHEMA_VERSION
from infrastructure.catalog.iceberg_adapter import PyIcebergCatalogAdapter
from infrastructure.catalog.phase1_tables import (
    PHASE1_REGISTRY,
    PHASE1_TABLES,
    describe_partition_spec,
)
from infrastructure.event.iceberg import (
    EventTable,
    EventTableConflict,
    EventTableCorrupted,
    EventTableError,
    event_batch_id,
    event_rows,
    event_rows_batch,
)
from infrastructure.event.runner import run_events
from infrastructure.event.table import EVENT_TABLE_COLUMNS, event_table
from infrastructure.event.table_definition import (
    EVENT_EVENTS,
    EVENT_RUN_COLUMNS,
    PHASE3_REGISTRY,
    PHASE3_TABLES,
    ensure_event_tables,
)
from plugins.events import EventSequenceProvider, FeatureThresholdCrossProvider, StateSwitchProvider
from tests.contract_version_support import at_pre_bump, built_at_pre_bump
from tests.fake_events import LAG, MINUTE, REGIME, REGIME_INPUTS, X_INPUTS, X, request
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness

CROSS = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)
CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)
#: Golden hash of the event.events definition document: any change is a new definition version.
#: Re-pinned 2026-09-26 when the optional ``subject`` column (ADR-0057) joined the logical table;
#: the definition had never been created in any catalog, so its version stays 1.0.0.
#: Re-pinned again 2026-09-26 (7c4372c0.. -> c7c494cd..) when the run block gained
#: ``contract_schema_version`` (field 16; list elements 17, 18): the recorded envelope a run is
#: rebuilt at (ADR-0052 versioned replay, V1). Still never created anywhere, still 1.0.0.
EVENT_EVENTS_HASH = "c7c494cd9126a8e38a86133e1d2bddadd10a25c5859688cf980daf7825cea1ae"


def _cross() -> EventResult:
    return run_events(
        FeatureThresholdCrossProvider((CROSS,)), CROSS, request(CROSS, inputs=X_INPUTS)
    )


def _switches() -> EventResult:
    return run_events(StateSwitchProvider((SWITCH,)), SWITCH, request(SWITCH, inputs=REGIME_INPUTS))


def _sequence() -> EventResult:
    ups = run_events(
        FeatureThresholdCrossProvider((CROSS_UP,)), CROSS_UP, request(CROSS_UP, inputs=X_INPUTS)
    )
    upstream = ups.events + _switches().events
    spec = EventSequenceProvider.spec(CROSS_UP, SWITCH, 3 * MINUTE, observable_lag=LAG)
    return run_events(
        EventSequenceProvider((spec,)),
        spec,
        request(spec, upstream_events=upstream),
        upstream_specs=(CROSS_UP, SWITCH),
    )


@dataclass
class Env:
    harness: SqliteCatalogHarness
    adapter: PyIcebergCatalogAdapter
    table: EventTable

    def head(self) -> str | None:
        info = self.adapter.load_table(EVENT_EVENTS.table)
        assert info is not None
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def commit_rows(self, rows: list[dict[str, Any]], batch_id: str) -> str:
        """Bypass the writer: commit arbitrary rows (tampering / corruption injection)."""
        batch = pa.Table.from_pylist(rows, schema=EVENT_EVENTS.arrow_schema)
        result = self.adapter.commit_batch(
            CommitRequest(
                table=EVENT_EVENTS.table,
                batch_id=batch_id,
                batch_fingerprint=EVENT_EVENTS.fingerprint_rule.fingerprint(batch),
                row_count=len(rows),
                expected_parent_snapshot_id=self.head(),
            ),
            batch,
        )
        return result.snapshot.snapshot_id


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    harness = SqliteCatalogHarness(tmp_path, registry=PHASE3_REGISTRY)
    adapter = harness.open_adapter()
    ensure_event_tables(adapter)
    yield Env(harness, adapter, EventTable(adapter))
    harness.cleanup()


# ------------------------------------------------------------------------------ definition


def test_definition_is_the_logical_table_plus_the_run_block() -> None:
    assert EVENT_EVENTS.table == "event.events" == EVENT_EVENTS.definition_id
    assert EVENT_EVENTS.version == "1.0.0"
    names = tuple(item.name for item in EVENT_EVENTS.arrow_schema)
    assert names == EVENT_TABLE_COLUMNS + EVENT_RUN_COLUMNS
    assert names[:10] == EVENT_TABLE_COLUMNS and names[9] == "subject"
    assert [item.name for item in EVENT_EVENTS.schema.fields if not item.required] == ["subject"]
    assert describe_partition_spec(EVENT_EVENTS) == "month(event_time)"
    assert EVENT_EVENTS.definition_hash == EVENT_EVENTS_HASH
    assert EVENT_EVENTS.fingerprint_rule.rule_id == "hlens.pyarrow-batch-sha256@1.0.0"


def test_phase1_registry_is_untouched() -> None:
    assert EVENT_EVENTS not in PHASE1_TABLES and len(PHASE1_TABLES) == 15
    with pytest.raises(UnknownTableDefinition):
        PHASE1_REGISTRY.resolve(EVENT_EVENTS.binding)
    assert PHASE3_TABLES == (EVENT_EVENTS,)


def test_create_is_idempotent_and_the_partition_spec_is_as_declared(env: Env) -> None:
    again = ensure_event_tables(env.adapter)
    assert len(again) == 1 and not again[0].created
    assert again[0].definition == EVENT_EVENTS.binding
    assert again[0].partition == "month(event_time)" and again[0].current_snapshot_id is None
    iceberg = env.harness.sql_catalog().load_table(("event", "events"))
    (partition,) = iceberg.spec().fields
    assert isinstance(partition.transform, MonthTransform)
    assert iceberg.schema().find_column_name(partition.source_id) == "event_time"
    assert partition.name == "event_time_month" and partition.field_id == 1000


def test_a_missing_table_is_refused(tmp_path: Path) -> None:
    harness = SqliteCatalogHarness(tmp_path, registry=PHASE3_REGISTRY)
    try:
        table = EventTable(harness.open_adapter())
        with pytest.raises(TableNotFound):
            table.write(_cross())
        with pytest.raises(TableNotFound):
            table.read(_cross().result_hash)
    finally:
        harness.cleanup()


# ------------------------------------------------------------------------------ round trip


@pytest.mark.parametrize("make", [_cross, _switches, _sequence], ids=["cross", "switch", "seq"])
def test_write_read_round_trip(env: Env, make: Callable[[], EventResult]) -> None:
    result = make()
    assert result.events
    written = env.table.write(result)
    assert written.outcome is CommitOutcome.COMMITTED and not written.replayed
    assert written.row_count == len(result.events) and written.snapshot_id == env.head()
    stored = env.table.read(result.result_hash)
    assert stored is not None and stored.snapshot_id == written.snapshot_id
    assert stored.rows == event_table(result)
    assert stored.result == result
    assert env.table.load(result.result_hash) == result
    snapshot = env.adapter.get_snapshot(EVENT_EVENTS.table, written.snapshot_id)
    assert snapshot.batch_id == event_batch_id(result.result_hash)
    assert snapshot.added_rows == len(result.events)


def test_interaction_rows_keep_their_upstream_lineage(env: Env) -> None:
    result = _sequence()
    env.table.write(result)
    stored = env.table.read(result.result_hash)
    assert stored is not None
    assert all(row.upstream_event_ids for row in stored.rows)


def test_rows_carry_the_run_block() -> None:
    result = _cross()
    rows = event_rows(result)
    assert [row["event_index"] for row in rows] == list(range(len(result.events)))
    assert {row["event_count"] for row in rows} == {len(result.events)}
    assert {(row["request_hash"], row["provider_hash"], row["as_of"]) for row in rows} == {
        (result.request_hash, result.provider_hash, result.as_of)
    }
    assert event_rows_batch(result).schema.equals(EVENT_EVENTS.arrow_schema, check_metadata=False)


def test_an_identical_rewrite_is_a_no_op(env: Env) -> None:
    result = _cross()
    first = env.table.write(result)
    again = env.table.write(result)
    assert again.replayed and again.outcome is None
    assert again.snapshot_id == first.snapshot_id == env.head()


def test_an_empty_run_writes_nothing(env: Env) -> None:
    provider = FeatureThresholdCrossProvider((CROSS,))
    empty = EventResult.build(request(CROSS, inputs=X_INPUTS), provider.descriptor, ())
    written = env.table.write(empty)
    assert written.empty and written.snapshot_id is None and written.outcome is None
    assert env.head() is None and env.table.read(empty.result_hash) is None


# ------------------------------------------------------------------------------ conflicts


def test_other_rows_under_the_run_are_refused_and_never_overwritten(env: Env) -> None:
    result = _cross()
    rows = event_rows(result)
    rows[0]["attributes_json"] = '{"direction":"down"}'
    tampered = env.commit_rows(rows, "tamper.1")
    with pytest.raises(EventTableConflict, match="never overwritten"):
        env.table.write(result)
    assert env.head() == tampered
    with pytest.raises(EventTableCorrupted):
        env.table.read(result.result_hash)


def test_the_run_batch_id_with_other_content_is_refused(env: Env) -> None:
    result, other = _cross(), _switches()
    rows = event_rows(other)  # other content committed under this run's batch id
    before = env.commit_rows(rows, event_batch_id(result.result_hash))
    with pytest.raises(EventTableConflict, match="other content"):
        env.table.write(result)
    assert env.head() == before and env.table.read(result.result_hash) is None


def _corrupt_missing_row(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows[1:]


def _corrupt_duplicate_index(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[1]["event_index"] = 0
    return rows


def _corrupt_event_count(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["event_count"] += 1
    return rows


def _corrupt_run_block(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[-1]["provider_hash"] = "0" * 64
    return rows


def _corrupt_event_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["event_time"] = rows[0]["event_time"] - MINUTE
    return rows


def _corrupt_as_of(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        row["as_of"] = row["as_of"] + MINUTE  # every event id holds; result_hash does not
    return rows


@pytest.mark.parametrize(
    "corrupt",
    [
        _corrupt_missing_row,
        _corrupt_duplicate_index,
        _corrupt_event_count,
        _corrupt_run_block,
        _corrupt_event_id,
        _corrupt_as_of,
    ],
)
def test_corrupted_runs_fail_closed_on_read(
    env: Env, corrupt: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
) -> None:
    result = _cross()
    assert len(result.events) >= 2
    env.commit_rows(corrupt(event_rows(result)), "tamper.corrupt")
    with pytest.raises(EventTableCorrupted):
        env.table.read(result.result_hash)
    with pytest.raises(EventTableConflict):
        env.table.write(result)


# ------------------------------------------------------------------------------ snapshots


def test_reads_are_snapshot_pinned(env: Env) -> None:
    a, b = _cross(), _switches()
    s1 = env.table.write(a).snapshot_id
    s2 = env.table.write(b).snapshot_id
    assert s1 is not None and s2 is not None and s1 != s2
    assert env.table.read(b.result_hash, snapshot_id=s1) is None
    at_s1 = env.table.read(a.result_hash, snapshot_id=s1)
    at_head = env.table.read(a.result_hash)
    assert at_s1 is not None and at_head is not None
    assert at_s1.snapshot_id == s1 and at_head.snapshot_id == s2
    assert at_s1.result == at_head.result == a and at_s1.rows == at_head.rows
    with pytest.raises(SnapshotNotFound):
        env.table.read(a.result_hash, snapshot_id="123")


@dataclass
class RacingAdapter:
    """Lands ``rival`` just before the first commit, so the writer's parent is stale."""

    inner: PyIcebergCatalogAdapter
    rival: EventResult
    commits: list[str] = field(default_factory=list)

    def load_table(self, table: str) -> Any:
        return self.inner.load_table(table)

    def scan_columns(self, table: str, **kwargs: Any) -> pa.Table:
        return self.inner.scan_columns(table, **kwargs)

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult:
        if not self.commits:
            self.commits.append("rival")
            EventTable(self.inner).write(self.rival)
        self.commits.append(request.batch_id)
        return self.inner.commit_batch(request, batch)


def test_a_lost_race_is_retried_on_the_new_head(env: Env) -> None:
    result, rival = _cross(), _switches()
    racing = RacingAdapter(env.adapter, rival)
    written = EventTable(racing).write(result)
    assert written.outcome is CommitOutcome.COMMITTED
    batch_id = event_batch_id(result.result_hash)
    assert racing.commits == ["rival", batch_id, batch_id]
    assert env.table.load(result.result_hash) == result
    assert env.table.load(rival.result_hash) == rival


def test_a_subject_bound_run_round_trips_with_its_subject(env: Env) -> None:
    """ADR-0057: a run bound to a subject keeps it in every row and rebuilds with the same hash."""
    req = request(CROSS, inputs=X_INPUTS, subject="BTCUSDT")
    bound = run_events(FeatureThresholdCrossProvider((CROSS,)), CROSS, req)
    assert bound.events and bound.result_hash != _cross().result_hash
    env.table.write(bound)
    env.table.write(_cross())  # an unbound run of the same spec lives beside it
    stored = env.table.read(bound.result_hash)
    assert stored is not None and stored.result == bound
    assert {row.subject for row in stored.rows} == {"BTCUSDT"}
    plain = env.table.read(_cross().result_hash)
    assert plain is not None and {row.subject for row in plain.rows} == {None}


# ------------------------------------------------------------------------------ contract versions


def _cross_at_2_0_0() -> EventResult:
    """The run exactly as the 2.0.0 code wrote it (ADR-0052 versioned replay)."""
    with built_at_pre_bump():
        spec = at_pre_bump(CROSS)
        return run_events(
            FeatureThresholdCrossProvider((spec,)),
            spec,
            at_pre_bump(request(spec, inputs=X_INPUTS)),
        )


def test_rows_record_the_run_envelope() -> None:
    assert {row["contract_schema_version"] for row in event_rows(_cross())} == {
        CONTRACT_SCHEMA_VERSION
    }
    assert {row["contract_schema_version"] for row in event_rows(_cross_at_2_0_0())} == {"2.0.0"}


def test_a_2_0_0_run_rebuilds_at_its_recorded_version_beside_a_current_run(env: Env) -> None:
    old, new = _cross_at_2_0_0(), _cross()
    assert old.schema_version == "2.0.0" and new.schema_version == CONTRACT_SCHEMA_VERSION
    assert old.result_hash != new.result_hash  # the envelope is part of every identity
    env.table.write(old)
    env.table.write(new)
    for result in (old, new):
        stored = env.table.read(result.result_hash)
        assert stored is not None and stored.result == result
        assert stored.result.result_hash == result.result_hash
        assert {item.schema_version for item in stored.result.events} == {result.schema_version}
        assert env.table.write(result).replayed


def test_a_run_mixing_envelopes_is_refused_before_writing(env: Env) -> None:
    # a 2.0.0 result envelope around current events (result_hash does not cover the envelope)
    mixed = EventResult.model_validate({**_cross().model_dump(), "schema_version": "2.0.0"})
    assert mixed.schema_version == "2.0.0"
    assert mixed.events[0].schema_version == CONTRACT_SCHEMA_VERSION
    with pytest.raises(EventTableError, match="mixes contract envelopes"):
        env.table.write(mixed)
    assert env.head() is None


@pytest.mark.parametrize("recorded", ["2.0.0", "2.1.0", "1.0.0", "2.9.0"])
def test_a_wrong_or_unpublished_recorded_version_fails_closed(env: Env, recorded: str) -> None:
    result = _cross()  # current content under another recorded version
    rows = [{**row, "contract_schema_version": recorded} for row in event_rows(result)]
    env.commit_rows(rows, "tamper.version")
    with pytest.raises(EventTableCorrupted):
        env.table.read(result.result_hash)
