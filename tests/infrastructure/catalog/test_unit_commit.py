"""ADR-0108 §2: one logical unit = one Iceberg snapshot of many staged data files."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DecimalType, LongType, NestedField, StringType, TimestamptzType

from core.contracts.catalog import (
    BatchConflict,
    BatchRejected,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
)
from infrastructure.catalog.definitions import RegisteredTableDefinition, TableDefinitionRegistry
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.iceberg_adapter import (
    SUMMARY_COMMIT_LAYOUT,
    SUMMARY_WINDOW_ROWS,
    UNIT_COMMIT_LAYOUT,
    PyIcebergCatalogAdapter,
)
from infrastructure.catalog.unit_commit import StagedUnitWriter
from tests.infrastructure.catalog.catalog_support import ALPHA, SqliteCatalogHarness, make_batch

_SCHEMA = Schema(
    NestedField(1, "seq", LongType(), required=True),
    NestedField(2, "tag", StringType(), required=True),
    NestedField(3, "amount", DecimalType(18, 8), required=False),
    NestedField(4, "at", TimestamptzType(), required=True),
)


def _definition(table: str) -> RegisteredTableDefinition:
    return RegisteredTableDefinition(
        table=table,
        definition_id=table,
        version="1.0.0",
        schema=_SCHEMA,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        partition_spec=PartitionSpec(
            PartitionField(source_id=2, field_id=1000, transform=IdentityTransform(), name="tag")
        ),
    )


UNIT = _definition("c2test.unit")
WHOLE = _definition("c2test.whole")
REGISTRY = TableDefinitionRegistry((UNIT, WHOLE, ALPHA))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _rows(start: int, count: int, *, shift: int = 0) -> pa.Table:
    seqs = list(range(start, start + count))
    return pa.Table.from_pydict(
        {
            "seq": seqs,
            "tag": ["a" if seq % 3 else "b" for seq in seqs],
            "amount": [None if seq % 5 == 0 else Decimal(seq + shift) / 8 for seq in seqs],
            "at": [T0 + timedelta(seconds=seq) for seq in seqs],
        },
        schema=UNIT.arrow_schema,
    )


def _unit(total: int = 50, size: int = 7, *, shift: int = 0) -> list[pa.Table]:
    return [_rows(start, min(size, total - start), shift=shift) for start in range(0, total, size)]


def _request(table: str, parts: list[pa.Table], batch_id: str, parent: str | None) -> CommitRequest:
    whole = pa.concat_tables(parts)
    return CommitRequest(
        table=table,
        batch_id=batch_id,
        batch_fingerprint=PYARROW_BATCH_FINGERPRINT.fingerprint(whole),
        row_count=whole.num_rows,
        expected_parent_snapshot_id=parent,
    )


def _factory(parts: list[pa.Table]) -> Callable[[], Iterable[pa.Table]]:
    return lambda: iter(parts)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    directory = tmp_path / "scratch"
    directory.mkdir()
    return directory


@pytest.fixture
def adapter(sqlite_harness: SqliteCatalogHarness) -> PyIcebergCatalogAdapter:
    opened = sqlite_harness.open_adapter(REGISTRY)
    opened.create_table(UNIT.binding)
    opened.create_table(WHOLE.binding)
    opened.create_table(ALPHA.binding)
    return opened


def _head(adapter: PyIcebergCatalogAdapter, table: str) -> str | None:
    info = adapter.load_table(table)
    assert info is not None
    return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


def _sorted_rows(adapter: PyIcebergCatalogAdapter, table: str) -> list[dict[str, Any]]:
    scanned = adapter.scan_columns(table, columns=("seq", "tag", "amount", "at")).to_pylist()
    return sorted(scanned, key=lambda row: row["seq"])


def test_a_unit_is_one_snapshot_with_the_rows_of_its_microbatches(
    adapter: PyIcebergCatalogAdapter, scratch: Path
) -> None:
    parts = _unit()
    request = _request(UNIT.table, parts, "unit-1", None)
    result = adapter.commit_unit(request, _factory(parts), scratch_directory=scratch, window_rows=7)
    assert result.outcome is CommitOutcome.COMMITTED
    assert result.snapshot.added_rows == 50 and result.snapshot.parent_snapshot_id is None
    assert result.snapshot.batch_fingerprint == request.batch_fingerprint
    history = list(adapter.history(UNIT.table, result.snapshot.snapshot_id))
    assert [item.batch_id for item in history] == ["unit-1"]
    iceberg = adapter._require(UNIT.table)
    current = iceberg.current_snapshot()
    assert current is not None and current.summary is not None
    summary = current.summary.additional_properties
    assert summary[SUMMARY_COMMIT_LAYOUT] == UNIT_COMMIT_LAYOUT
    assert summary[SUMMARY_WINDOW_ROWS] == "7"
    files = list(iceberg.scan().plan_files())
    assert len(files) == 2  # one staged file per partition, not one per microbatch
    # The same rows as one commit_batch of the concatenated unit (another table).
    whole = pa.concat_tables(parts)
    adapter.commit_batch(_request(WHOLE.table, parts, "whole-1", None), whole)
    assert _sorted_rows(adapter, UNIT.table) == _sorted_rows(adapter, WHOLE.table)
    assert list(scratch.iterdir()) == []  # fingerprint spools removed


def test_a_unit_commit_is_idempotent_and_conflicting_content_is_refused(
    adapter: PyIcebergCatalogAdapter, scratch: Path
) -> None:
    parts = _unit()
    request = _request(UNIT.table, parts, "unit-1", None)
    first = adapter.commit_unit(request, _factory(parts), scratch_directory=scratch, window_rows=7)
    again = adapter.commit_unit(request, _factory(parts), scratch_directory=scratch, window_rows=7)
    assert again.outcome is CommitOutcome.ALREADY_COMMITTED
    assert again.snapshot.snapshot_id == first.snapshot.snapshot_id
    other = _unit(shift=1)
    with pytest.raises(BatchConflict):
        adapter.commit_unit(
            _request(UNIT.table, other, "unit-1", first.snapshot.snapshot_id),
            _factory(other),
            scratch_directory=scratch,
            window_rows=7,
        )
    assert _head(adapter, UNIT.table) == first.snapshot.snapshot_id


def test_a_false_claim_a_stale_parent_or_a_changing_source_commits_nothing(
    adapter: PyIcebergCatalogAdapter, scratch: Path
) -> None:
    parts = _unit()
    claim = _request(UNIT.table, _unit(shift=1), "unit-1", None)
    with pytest.raises(BatchRejected, match="fingerprint does not match"):
        adapter.commit_unit(claim, _factory(parts), scratch_directory=scratch, window_rows=7)
    short = _request(UNIT.table, parts[:-1], "unit-1", None)
    with pytest.raises(BatchRejected, match="rows but the request declares"):
        adapter.commit_unit(short, _factory(parts), scratch_directory=scratch, window_rows=7)
    stale = _request(UNIT.table, parts, "unit-1", "123")
    with pytest.raises(CommitConflict):
        adapter.commit_unit(stale, _factory(parts), scratch_directory=scratch, window_rows=7)
    calls = iter([parts, _unit(shift=1)])
    with pytest.raises(BatchRejected, match="changed between verification and staging"):
        adapter.commit_unit(
            _request(UNIT.table, parts, "unit-1", None),
            lambda: iter(next(calls)),
            scratch_directory=scratch,
            window_rows=7,
        )
    assert _head(adapter, UNIT.table) is None
    assert list(scratch.iterdir()) == []


def test_only_tables_bound_to_the_streaming_rule_take_unit_commits(
    adapter: PyIcebergCatalogAdapter, scratch: Path
) -> None:
    batch = make_batch(3, "x")
    request = CommitRequest(
        table=ALPHA.table,
        batch_id="alpha-1",
        batch_fingerprint=ALPHA.fingerprint_rule.fingerprint(batch),
        row_count=3,
        expected_parent_snapshot_id=None,
    )
    with pytest.raises(BatchRejected, match="not bound to"):
        adapter.commit_unit(
            request, lambda: iter([batch]), scratch_directory=scratch, window_rows=3
        )
    with pytest.raises(BatchRejected, match="window_rows"):
        adapter.commit_unit(
            request, lambda: iter([batch]), scratch_directory=scratch, window_rows=0
        )


def test_the_staged_writer_rolls_row_groups_and_files_in_bounded_steps(
    adapter: PyIcebergCatalogAdapter,
) -> None:
    iceberg = adapter._require(UNIT.table)
    writer = StagedUnitWriter(iceberg, tag="t", row_group_rows=3, file_rows=7)
    for part in _unit(total=40, size=4):
        writer.add(part)
    files = writer.finish()
    by_tag: dict[str, int] = {}
    for data_file in files:
        assert data_file.record_count <= 7
        tag = data_file.partition[0]
        by_tag[tag] = by_tag.get(tag, 0) + data_file.record_count
    whole = pa.concat_tables(_unit(total=40, size=4))
    assert by_tag == {
        tag: sum(1 for value in whole.column("tag").to_pylist() if value == tag)
        for tag in ("a", "b")
    }
    with pytest.raises(RuntimeError):
        writer.add(_rows(0, 1))
