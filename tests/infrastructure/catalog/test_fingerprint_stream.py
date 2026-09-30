"""The row-run fingerprint helper emits the exact frozen PyArrow batch digest."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import BatchRejected
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.fingerprint_stream import _text_bytes, _utf8_size, fingerprint_run
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import (
    RunIntegrityError,
    RunLimits,
    RunRef,
    RunSetBuilder,
    write_sorted_run,
)


def _schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("flag", pa.bool_(), nullable=True),
            pa.field("number", pa.int64(), nullable=True),
            pa.field("instant", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("amount", pa.decimal128(20, 4), nullable=True),
            pa.field("label", pa.large_string(), nullable=True),
            pa.field(
                "tags",
                pa.large_list(pa.field("item", pa.large_string(), nullable=True)),
                nullable=True,
            ),
            pa.field(
                "nested",
                pa.struct(
                    [
                        pa.field("enabled", pa.bool_(), nullable=True),
                        pa.field(
                            "values",
                            pa.large_list(pa.field("item", pa.int64(), nullable=False)),
                            nullable=True,
                        ),
                    ]
                ),
                nullable=True,
            ),
        ]
    )


def _table() -> pa.Table:
    schema = _schema()
    rows = [
        {
            "flag": True,
            "number": 3,
            "instant": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            "amount": Decimal("-12.3456"),
            "label": "多字节😀",
            "tags": ["one", None, "three"],
            "nested": {"enabled": False, "values": [1, -2]},
        },
        {
            "flag": None,
            "number": -7,
            "instant": None,
            "amount": None,
            "label": None,
            "tags": None,
            "nested": None,
        },
        {
            "flag": False,
            "number": 0,
            "instant": datetime(1960, 1, 2, 3, 4, 5, 6, tzinfo=UTC),
            "amount": Decimal("999999.0001"),
            "label": 'escaped\\n"text',
            "tags": [],
            "nested": {"enabled": True, "values": []},
        },
    ]
    return pa.Table.from_pylist(rows, schema=schema)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[LocalFileStorageAdapter]:
    opened = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "stage").as_uri()
    )
    yield opened
    opened.close()


def _run(storage: LocalFileStorageAdapter, table: pa.Table, *, capacity: int) -> RunRef:
    builder = RunSetBuilder(
        storage,
        key=lambda row: (
            row["number"]
            if isinstance(row.get("number"), int) and not isinstance(row.get("number"), bool)
            else -999
        ),
        capacity=capacity,
        merge_fanout=2,
        limits=RunLimits(leaf_max_records=capacity, leaf_max_bytes=32768, fanout=2),
    )
    with builder:
        for row in table.to_pylist():
            builder.add(row)
        result = builder.finish()
    assert result is not None
    return result


class _ReadSpyHandle:
    def __init__(self, handle: Any, sizes: list[int]) -> None:
        self._handle = handle
        self._sizes = sizes

    def __enter__(self) -> _ReadSpyHandle:
        return self

    def __exit__(self, *args: object) -> None:
        self._handle.close()

    def read(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        if size < 0:
            raise AssertionError("reader attempted an unbounded object read")
        return cast(bytes, self._handle.read(size))


class _ReadSpyStorage:
    def __init__(self, storage: LocalFileStorageAdapter) -> None:
        self._storage = storage
        self.read_sizes: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def open_read(self, ref: Any) -> _ReadSpyHandle:
        return _ReadSpyHandle(self._storage.open_read(ref), self.read_sizes)


def test_stream_fingerprint_matches_frozen_rule_across_run_partitions(
    storage: LocalFileStorageAdapter,
) -> None:
    unsorted = _table().slice(0, 3)
    ordered_rows = sorted(
        unsorted.to_pylist(), key=lambda row: row["number"] if row["number"] is not None else -999
    )
    table = pa.Table.from_pylist(ordered_rows, schema=unsorted.schema)
    expected = PYARROW_BATCH_FINGERPRINT.fingerprint(table)
    for capacity in (1, 2, 5):
        run = _run(storage, table, capacity=capacity)
        actual, stats = fingerprint_run(
            storage,
            run,
            table.schema,
            max_record_bytes=8192,
            max_run_object_bytes=32768,
            row_chunk_capacity=2,
            max_hash_chunk_bytes=31,
        )
        assert actual == expected
        assert stats.run_passes > 1
        assert stats.max_row_bytes > 0
        exact, _ = fingerprint_run(
            storage,
            run,
            table.schema,
            max_record_bytes=stats.max_row_bytes,
            max_run_object_bytes=32768,
            row_chunk_capacity=2,
            max_hash_chunk_bytes=31,
        )
        assert exact == expected
        with pytest.raises(BatchRejected, match="max_record_bytes"):
            fingerprint_run(
                storage,
                run,
                table.schema,
                max_record_bytes=stats.max_row_bytes - 1,
                max_run_object_bytes=32768,
                row_chunk_capacity=2,
                max_hash_chunk_bytes=31,
            )


def test_stream_fingerprint_handles_empty_run(storage: LocalFileStorageAdapter) -> None:
    empty = write_sorted_run(
        storage,
        [],
        RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
    )
    table = pa.Table.from_pylist([], schema=_schema())
    assert fingerprint_run(
        storage,
        empty,
        table.schema,
        max_record_bytes=8192,
        max_run_object_bytes=4096,
        row_chunk_capacity=2,
        max_hash_chunk_bytes=128,
    )[0] == PYARROW_BATCH_FINGERPRINT.fingerprint(table)


def test_stream_fingerprint_preserves_max_precision_decimal(
    storage: LocalFileStorageAdapter,
) -> None:
    schema = pa.schema([pa.field("amount", pa.decimal128(38, 18), nullable=False)])
    table = pa.Table.from_pylist(
        [{"amount": Decimal("99999999999999999999.999999999999999999")}],
        schema=schema,
    )
    run = _run(storage, table, capacity=1)
    actual, _ = fingerprint_run(
        storage,
        run,
        schema,
        max_record_bytes=4096,
        max_run_object_bytes=4096,
        row_chunk_capacity=1,
        max_hash_chunk_bytes=16,
    )
    assert actual == PYARROW_BATCH_FINGERPRINT.fingerprint(table)


def test_stream_fingerprint_rejects_duplicate_schema_field_names(
    storage: LocalFileStorageAdapter,
) -> None:
    run = _run(storage, _table(), capacity=1)
    duplicate_top = pa.schema([pa.field("value", pa.int64()), pa.field("value", pa.int64())])
    duplicate_nested = pa.schema(
        [
            pa.field(
                "items",
                pa.large_list(
                    pa.struct([pa.field("child", pa.int64()), pa.field("child", pa.int64())])
                ),
            )
        ]
    )
    for schema in (duplicate_top, duplicate_nested):
        with pytest.raises(BatchRejected, match="duplicate field names"):
            fingerprint_run(
                storage,
                run,
                schema,
                max_record_bytes=4096,
                max_run_object_bytes=4096,
                row_chunk_capacity=1,
                max_hash_chunk_bytes=128,
            )


def test_stream_fingerprint_rejects_invalid_utf8_and_required_nulls(
    storage: LocalFileStorageAdapter,
) -> None:
    schema = pa.schema([pa.field("label", pa.large_string(), nullable=True)])
    with pytest.raises(BatchRejected):
        _utf8_size("\ud800")
    chunks = list(_text_bytes(iter(("多字节😀" * 8,)), 16))
    assert chunks and all(len(chunk) <= 16 for chunk in chunks)
    with pytest.raises(BatchRejected, match="at least 16"):
        fingerprint_run(
            storage,
            write_sorted_run(
                storage, [], RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2)
            ),
            schema,
            max_record_bytes=4096,
            max_run_object_bytes=4096,
            row_chunk_capacity=1,
            max_hash_chunk_bytes=15,
        )

    required = pa.schema([pa.field("required", pa.int64(), nullable=False)])
    nullable = pa.schema([pa.field("required", pa.int64(), nullable=True)])
    null_table = pa.Table.from_pylist([{"required": None}], schema=nullable)
    null_row = _run(storage, null_table, capacity=1)
    with pytest.raises(BatchRejected, match="contains nulls"):
        fingerprint_run(
            storage,
            null_row,
            required,
            max_record_bytes=4096,
            max_run_object_bytes=4096,
            row_chunk_capacity=1,
            max_hash_chunk_bytes=128,
        )


def test_stream_fingerprint_caps_run_object_read_before_full_allocation(
    storage: LocalFileStorageAdapter,
) -> None:
    schema = pa.schema([pa.field("label", pa.large_string(), nullable=False)])
    table = pa.Table.from_pylist([{"label": "x" * 2000}], schema=schema)
    run = _run(storage, table, capacity=1)
    spy = _ReadSpyStorage(storage)
    with pytest.raises(RunIntegrityError, match="exceeds its configured byte bound"):
        fingerprint_run(
            spy,  # type: ignore[arg-type]
            run,
            schema,
            max_record_bytes=4096,
            max_run_object_bytes=128,
            row_chunk_capacity=1,
            max_hash_chunk_bytes=128,
        )
    assert spy.read_sizes == [129]
