"""``PyIcebergCatalogAdapter.max_int64``: streaming, bounded, fail-closed reduction (D2-R1).

The D2 arrival-sequence anchor must not materialise a whole column history as one Arrow table.
These tests pin the two properties that matter and would both fail against the previous
``scan_columns(...).to_arrow()`` implementation:

- the reduction goes through PyIceberg's streaming batch reader and never calls ``DataScan``'s
  whole-table ``to_arrow``;
- every streamed value is validated before it can win the reduction.

SQLite is the injected catalog here (fast unit harness); it is never PostgreSQL evidence.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.expressions import BooleanExpression, EqualTo
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType

from core.contracts.catalog import BatchRejected, CommitRequest
from infrastructure.catalog import (
    CatalogIntegrityError,
    PyIcebergCatalogAdapter,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
)
from tests.infrastructure.catalog.catalog_support import RULE, ScanSpy, SqliteCatalogHarness

#: A test-only definition with a required and an optional ``int64`` column plus a string column.
REDUCE = RegisteredTableDefinition(
    table="c2test.reduce",
    definition_id="c2test.reduce",
    version="1.0.0",
    schema=Schema(
        NestedField(1, "seq", LongType(), required=True),
        NestedField(2, "maybe", LongType(), required=False),
        NestedField(3, "tag", StringType(), required=True),
    ),
    fingerprint_rule=RULE,
)
REDUCE_REGISTRY = TableDefinitionRegistry((REDUCE,))


def _tag_is(value: str) -> BooleanExpression:
    """``tag == value``; ``EqualTo``'s generic base confuses the type checker."""
    return EqualTo("tag", value)  # type: ignore[call-arg, arg-type]


def _batch(seqs: list[int], *, maybe: list[int | None] | None = None, tag: str = "a") -> pa.Table:
    return pa.Table.from_pydict(
        {
            "seq": seqs,
            "maybe": list(seqs) if maybe is None else maybe,
            "tag": [tag] * len(seqs),
        },
        schema=REDUCE.arrow_schema,
    )


@pytest.fixture
def adapter(sqlite_harness: SqliteCatalogHarness) -> PyIcebergCatalogAdapter:
    opened = sqlite_harness.open_adapter(registry=REDUCE_REGISTRY)
    opened.create_table(REDUCE.binding)
    return opened


def _commit(adapter: PyIcebergCatalogAdapter, batch: pa.Table, batch_id: str) -> None:
    """Append ``batch`` as its own commit, so each call adds another data file."""
    info = adapter.load_table(REDUCE.table)
    assert info is not None
    parent = None if info.current_snapshot is None else info.current_snapshot.snapshot_id
    adapter.commit_batch(
        CommitRequest(
            table=REDUCE.table,
            batch_id=batch_id,
            batch_fingerprint=RULE.fingerprint(batch),
            row_count=batch.num_rows,
            expected_parent_snapshot_id=parent,
        ),
        batch,
    )


def test_empty_table_has_no_maximum(adapter: PyIcebergCatalogAdapter) -> None:
    assert adapter.max_int64(REDUCE.table, "seq") is None


def test_maximum_spans_every_committed_data_file(adapter: PyIcebergCatalogAdapter) -> None:
    _commit(adapter, _batch([3, 1, 2]), "b1")
    _commit(adapter, _batch([9, 4]), "b2")
    _commit(adapter, _batch([7]), "b3")

    assert adapter.max_int64(REDUCE.table, "seq") == 9


def test_the_reduction_streams_and_never_materialises_one_table(
    adapter: PyIcebergCatalogAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old ``scan_columns(...).to_arrow()`` anchor is impossible under this assertion."""
    for index in range(6):
        _commit(adapter, _batch([index * 10, index * 10 + 1]), f"b{index}")

    spy = ScanSpy(monkeypatch)
    assert adapter.max_int64(REDUCE.table, "seq") == 51

    assert spy.readers == 1, "the anchor must go through the streaming batch reader"
    assert spy.to_arrow_rows == [], "no whole-column table may be materialised"
    assert sum(spy.batch_rows) == 12  # every row was still visited
    assert max(spy.batch_rows) < 12  # ...but never all of them at once


def test_a_row_filter_bounds_the_reduction(adapter: PyIcebergCatalogAdapter) -> None:
    _commit(adapter, _batch([1, 2], tag="a"), "b1")
    _commit(adapter, _batch([30, 40], tag="b"), "b2")

    assert adapter.max_int64(REDUCE.table, "seq", row_filter=_tag_is("a")) == 2


def test_every_value_is_checked_before_it_can_win(adapter: PyIcebergCatalogAdapter) -> None:
    _commit(adapter, _batch([2, 4, 6]), "b1")
    _commit(adapter, _batch([8, 9]), "b2")
    seen: list[int] = []

    def check(value: int) -> None:
        seen.append(value)
        if value % 2:
            raise CatalogIntegrityError("odd value")

    with pytest.raises(CatalogIntegrityError, match="odd"):
        adapter.max_int64(REDUCE.table, "seq", check=check)
    assert 9 in seen, "the illegal value was reached, not skipped by an aggregate"


def test_a_check_that_accepts_everything_returns_the_maximum(
    adapter: PyIcebergCatalogAdapter,
) -> None:
    _commit(adapter, _batch([5, 11, 8]), "b1")
    seen: list[int] = []
    assert adapter.max_int64(REDUCE.table, "seq", check=seen.append) == 11
    assert sorted(seen) == [5, 8, 11]


def test_a_null_value_fails_closed(adapter: PyIcebergCatalogAdapter) -> None:
    _commit(adapter, _batch([1, 2], maybe=[1, None]), "b1")

    with pytest.raises(CatalogIntegrityError, match="null"):
        adapter.max_int64(REDUCE.table, "maybe")


def test_a_non_int64_column_fails_closed(adapter: PyIcebergCatalogAdapter) -> None:
    _commit(adapter, _batch([1, 2]), "b1")

    with pytest.raises(CatalogIntegrityError, match="int64"):
        adapter.max_int64(REDUCE.table, "tag")


@pytest.mark.parametrize("column", ["", None, 7])
def test_a_malformed_column_name_is_refused(adapter: PyIcebergCatalogAdapter, column: Any) -> None:
    with pytest.raises(BatchRejected):
        adapter.max_int64(REDUCE.table, column)


def test_a_non_callable_check_is_refused(adapter: PyIcebergCatalogAdapter) -> None:
    with pytest.raises(BatchRejected):
        adapter.max_int64(REDUCE.table, "seq", check="nope")  # type: ignore[arg-type]
