"""``hlens.pyarrow-batch-sha256@1.0.0``: golden vectors, chunking invariance, sensitivity, rejects.

Golden vectors were produced with the locked ``pyarrow==25.0.1``; a different value means the
canonical encoding changed and the rule needs a new version (never an edit of these constants).
"""

from __future__ import annotations

import hashlib
import struct
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import BatchRejected
from core.domain.base import contract_schema_version_scope
from infrastructure.catalog import PYARROW_BATCH_FINGERPRINT, PYARROW_BATCH_FINGERPRINT_RULE_ID
from infrastructure.catalog.phase1_tables import CANONICAL_BARS_1M, DATASET_MANIFESTS
from tests.contract_version_support import at_pre_bump
from tests.infrastructure.catalog.phase1_support import (
    batch_for,
    manifest,
    manifest_row_of,
    minimal_batch,
)

fingerprint = PYARROW_BATCH_FINGERPRINT.fingerprint

ITEM = pa.struct([pa.field("x", pa.int64(), nullable=False), pa.field("y", pa.large_string())])
SCHEMA = pa.schema(
    [
        pa.field("flag", pa.bool_()),
        pa.field("n", pa.int64(), nullable=False),
        pa.field("ts", pa.timestamp("us", tz="UTC")),
        pa.field("amount", pa.decimal128(38, 18)),
        pa.field("text", pa.large_string()),
        pa.field("tags", pa.large_list(pa.field("element", pa.large_string(), nullable=False))),
        pa.field("pair", ITEM),
        pa.field("items", pa.large_list(pa.field("element", ITEM, nullable=False)), nullable=False),
    ]
)
T = datetime(2024, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
ROWS: list[dict[str, Any]] = [
    {
        "flag": True,
        "n": 0,
        "ts": T,
        "amount": Decimal("93712.010000000000000001"),
        "text": "BTCUSDT",
        "tags": ["a", ""],
        "pair": {"x": 1, "y": "p"},
        "items": [{"x": 2, "y": None}, {"x": 3, "y": "漢字"}],
    },
    {
        "flag": None,
        "n": -7,
        "ts": None,
        "amount": None,
        "text": None,
        "tags": None,
        "pair": None,
        "items": [],
    },
    {
        "flag": False,
        "n": 9_223_372_036_854_775_807,
        "ts": datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC),
        "amount": Decimal("-99999999999999999999.999999999999999999"),
        "text": "😀 é",
        "tags": [],
        "pair": {"x": -1, "y": None},
        "items": [{"x": 4, "y": ""}],
    },
]


def table(rows: list[dict[str, Any]], schema: pa.Schema = SCHEMA) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=schema)


BASE = table(ROWS)

GOLDEN = {
    "empty": "0f17ffc8b0db8f756c34c3e5f0f131b5fe5ef6c219eecbe5129a7aa75d2933c2",
    "base": "69c72f3a6c06fdac771f49153c9556ac4725eacadb3b1d43ae42e4e50062fb2f",
    "one_row": "44ad64ef592474c9c9e05d1cb4e094dca227910c81ecb181d3ecaf742648db66",
    "bars_1m_empty": "183d40ab65a495b697854082a9035a0b0732102a2cd9f0371d174dd1f67425ba",
    "bars_1m_row": "433131dc4b48beed7ae2cd66b45e235ef9bde51d6767dd04f9d6f64861b59c15",
    "manifest_row": "0c05682831e041614979e10c5c1f8fbdebd3554111a5315b49fe19a45e9a490a",
}


def golden_inputs() -> dict[str, pa.Table]:
    # The golden rows were pinned with the fixture objects built at contract 2.0.0: they are
    # rebuilt at 2.0.0, so these vectors keep testing the fingerprint rule only (ADR-0052 §4).
    with contract_schema_version_scope("2.0.0"):
        return {
            "empty": table([]),
            "base": BASE,
            "one_row": table(ROWS[:1]),
            "bars_1m_empty": CANONICAL_BARS_1M.arrow_schema.empty_table(),
            "bars_1m_row": minimal_batch(CANONICAL_BARS_1M, "golden"),
            # the fixture manifest nests import-time constants: its 2.0.0 twin
            "manifest_row": batch_for(
                DATASET_MANIFESTS, [manifest_row_of(at_pre_bump(manifest("golden")))]
            ),
        }


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_vectors(name: str) -> None:
    value = fingerprint(golden_inputs()[name])
    assert value == GOLDEN[name]
    assert len(value) == 64 and value == value.lower()


def test_encoding_matches_the_documented_frames() -> None:
    """Independent re-derivation of the module-docstring encoding for a small table."""
    schema = pa.schema(
        [
            pa.field("flag", pa.bool_()),
            pa.field("n", pa.int64(), nullable=False),
            pa.field("s", pa.large_string()),
            pa.field("ts", pa.timestamp("us", tz="UTC")),
        ]
    )
    rows: list[dict[str, Any]] = [
        {"flag": True, "n": -2, "s": "é", "ts": datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)},
        {"flag": None, "n": 5, "s": None, "ts": None},
    ]
    digest = hashlib.sha256()

    def frame(payload: bytes) -> None:
        digest.update(struct.pack("<Q", len(payload)) + payload)

    frame(b"hlens.pyarrow-batch-sha256@1.0.0")
    frame(
        b'[{"name":"flag","nullable":true,"type":{"type":"bool"}},'
        b'{"name":"n","nullable":false,"type":{"type":"int64"}},'
        b'{"name":"s","nullable":true,"type":{"type":"large_string"}},'
        b'{"name":"ts","nullable":true,"type":{"type":"timestamp","tz":"UTC","unit":"us"}}]'
    )
    frame(struct.pack("<Q", 2))
    frame(b"\x01\x00")  # flag validity
    frame(b"\x01\x00")  # flag values (null -> 0)
    frame(b"\x01\x01")  # n validity
    frame(struct.pack("<qq", -2, 5))
    frame(b"\x01\x00")  # s validity
    frame(struct.pack("<qq", 2, 0))  # UTF-8 byte lengths
    frame("é".encode())
    frame(b"\x01\x00")  # ts validity
    frame(struct.pack("<qq", 1_000_000, 0))
    assert fingerprint(table(rows, schema)) == digest.hexdigest()


def test_rule_identity() -> None:
    assert PYARROW_BATCH_FINGERPRINT.rule_id == PYARROW_BATCH_FINGERPRINT_RULE_ID
    assert PYARROW_BATCH_FINGERPRINT_RULE_ID == "hlens.pyarrow-batch-sha256@1.0.0"
    assert len(set(GOLDEN.values())) == len(GOLDEN)


# --------------------------------------------------------------------------- invariance


def rechunk(source: pa.Table, cuts: dict[str, list[int]]) -> pa.Table:
    """Same logical table; each column split at its own cut points (empty chunks included)."""
    columns = []
    for name in source.column_names:
        array = source.column(name).combine_chunks()
        points = [0, *cuts.get(name, []), len(array)]
        chunks = [array.slice(a, b - a) for a, b in zip(points, points[1:], strict=False)]
        columns.append(pa.chunked_array(chunks, type=array.type))
    return pa.Table.from_arrays(columns, schema=source.schema)


def test_chunking_and_slicing_do_not_change_the_fingerprint() -> None:
    expected = fingerprint(BASE)
    variants = [
        pa.concat_tables([BASE.slice(0, 1), BASE.slice(1, 1), BASE.slice(2)]),
        pa.concat_tables([BASE.slice(0, 0), BASE, BASE.slice(3)]),
        rechunk(BASE, {"flag": [1], "n": [0, 0, 2], "text": [2], "items": [1, 1], "pair": [2]}),
        BASE.combine_chunks(),
        pa.Table.from_batches(BASE.to_batches(max_chunksize=1)),
        table(ROWS + ROWS).slice(3),  # offsets inside every buffer
    ]
    for variant in variants:
        assert variant.equals(BASE)
        assert fingerprint(variant) == expected
    assert fingerprint(table(ROWS + ROWS).slice(1, 3)) == fingerprint(table([*ROWS[1:], ROWS[0]]))


def test_bytes_under_nulls_and_bitmap_presence_are_not_semantic() -> None:
    schema = pa.schema([pa.field("n", pa.int64()), pa.field("s", pa.large_string())])
    clean = table([{"n": 5, "s": "a"}, {"n": None, "s": None}, {"n": 7, "s": "c"}], schema)
    validity = pa.py_buffer(bytes([0b101]))
    dirty_n = pa.Array.from_buffers(
        pa.int64(), 3, [validity, pa.array([5, 999, 7], pa.int64()).buffers()[1]]
    )
    offsets = pa.array([0, 1, 4, 5], pa.int64()).buffers()[1]
    dirty_s = pa.Array.from_buffers(
        pa.large_string(), 3, [validity, offsets, pa.py_buffer(b"aXYZc")]
    )
    dirty = pa.Table.from_arrays([dirty_n, dirty_s], schema=schema)
    assert dirty.equals(clean)
    assert fingerprint(dirty) == fingerprint(clean)

    all_valid = pa.Array.from_buffers(
        pa.int64(), 2, [pa.py_buffer(bytes([0b11])), pa.array([1, 2], pa.int64()).buffers()[1]]
    )
    no_bitmap = pa.array([1, 2], pa.int64())
    one = pa.schema([pa.field("n", pa.int64())])
    assert fingerprint(pa.Table.from_arrays([all_valid], schema=one)) == fingerprint(
        pa.Table.from_arrays([no_bitmap], schema=one)
    )

    tags_type = SCHEMA.field("tags").type
    masked_list = pa.LargeListArray.from_arrays(
        pa.array([0, 1, 3, 3], pa.int64()),
        pa.array(["a", "hidden", "hidden"], pa.large_string()),
        type=tags_type,
        mask=pa.array([False, True, False]),
    )
    plain_list = pa.array([["a"], None, []], tags_type)
    masked_struct = pa.StructArray.from_arrays(
        [pa.array([1, 42, 3], pa.int64()), pa.array(["p", "hidden", None], pa.large_string())],
        fields=list(ITEM),
        mask=pa.array([False, True, False]),
    )
    plain_struct = pa.array([{"x": 1, "y": "p"}, None, {"x": 3, "y": None}], ITEM)
    nested = pa.schema([SCHEMA.field("tags"), SCHEMA.field("pair")])
    assert fingerprint(pa.Table.from_arrays([masked_list, masked_struct], schema=nested)) == (
        fingerprint(pa.Table.from_arrays([plain_list, plain_struct], schema=nested))
    )


def test_schema_and_field_metadata_are_not_semantic() -> None:
    annotated = BASE.replace_schema_metadata({"origin": "writer-a"})
    fields = [f.with_metadata({"doc": "anything"}) for f in annotated.schema]
    annotated = annotated.cast(pa.schema(fields, metadata={"origin": "writer-b"}))
    assert fingerprint(annotated) == fingerprint(BASE)
    documented = minimal_batch(CANONICAL_BARS_1M, "golden")  # carries Iceberg doc metadata
    assert fingerprint(documented.replace_schema_metadata(None)) == fingerprint(documented)


# --------------------------------------------------------------------------- sensitivity


def with_row(index: int, **changes: Any) -> pa.Table:
    rows = [dict(row) for row in ROWS]
    rows[index].update(changes)
    return table(rows)


def test_every_logical_change_changes_the_fingerprint() -> None:
    nfd = unicodedata.normalize("NFD", "😀 é")
    assert nfd != ROWS[2]["text"]
    renamed = BASE.rename_columns(["flag", "n", "ts", "amount", "label", "tags", "pair", "items"])
    relaxed = BASE.cast(pa.schema([f.with_nullable(True) for f in SCHEMA]))
    rescaled_schema = pa.schema(
        [pa.field("amount", pa.decimal128(38, 17))] + [f for f in SCHEMA if f.name != "amount"]
    )
    variants = {
        "row order": table([ROWS[1], ROWS[0], ROWS[2]]),
        "extra row": table([*ROWS, ROWS[0]]),
        "int": with_row(0, n=1),
        "int zero vs null (via bool)": with_row(2, flag=None),
        "bool": with_row(0, flag=False),
        "timestamp +1us": with_row(0, ts=T + timedelta(microseconds=1)),
        "timestamp null": with_row(2, ts=None),
        "decimal last digit": with_row(0, amount=Decimal("93712.010000000000000002")),
        "decimal null": with_row(0, amount=None),
        "string byte": with_row(0, text="BTCUSDt"),
        "string NFD": with_row(2, text=nfd),
        "empty vs null string": with_row(1, text=""),
        "empty vs null list": with_row(1, tags=[]),
        "list element": with_row(0, tags=["a", "b"]),
        "list order": with_row(0, tags=["", "a"]),
        "struct null vs null children": with_row(1, pair={"x": 0, "y": None}),
        "struct child": with_row(0, pair={"x": 1, "y": "q"}),
        "nested list struct": with_row(0, items=[{"x": 2, "y": ""}, {"x": 3, "y": "漢字"}]),
        "column order": BASE.select(["n", "flag", "ts", "amount", "text", "tags", "pair", "items"]),
        "column name": renamed,
        "nullability": relaxed,
        "decimal scale": pa.Table.from_pylist(
            [{**row, "amount": None} for row in ROWS], schema=rescaled_schema
        ),
    }
    base = fingerprint(BASE)
    values = {name: fingerprint(item) for name, item in variants.items()}
    assert base not in values.values(), [n for n, v in values.items() if v == base]
    assert len(set(values.values())) == len(values)
    # Zero and null are distinct for fixed-width values.
    one = pa.schema([pa.field("n", pa.int64())])
    assert fingerprint(table([{"n": 0}], one)) != fingerprint(table([{"n": None}], one))


# --------------------------------------------------------------------------- rejections


@pytest.mark.parametrize(
    "value",
    [BASE.to_batches()[0], BASE.to_reader(), BASE.to_pylist(), {"n": [1]}, None],
    ids=["record-batch", "reader", "list", "dict", "none"],
)
def test_only_bounded_tables_are_accepted(value: object) -> None:
    with pytest.raises(BatchRejected):
        fingerprint(value)


@pytest.mark.parametrize(
    "data_type",
    [
        pa.float64(),
        pa.int32(),
        pa.string(),
        pa.binary(),
        pa.large_binary(),
        pa.list_(pa.int64()),
        pa.timestamp("ns", tz="UTC"),
        pa.timestamp("us"),
        pa.timestamp("us", tz="+00:00"),
        pa.timestamp("us", tz="Etc/UTC"),
        pa.date32(),
        pa.map_(pa.large_string(), pa.large_string()),
        pa.dictionary(pa.int32(), pa.large_string()),
        pa.decimal256(40, 18),
        pa.null(),
    ],
    ids=str,
)
def test_types_outside_the_closed_vocabulary_are_rejected(data_type: pa.DataType) -> None:
    schema = pa.schema([pa.field("v", data_type)])
    with pytest.raises(BatchRejected, match="unsupported"):
        fingerprint(schema.empty_table())
    nested = pa.schema([pa.field("v", pa.large_list(pa.field("element", data_type)))])
    with pytest.raises(BatchRejected, match="unsupported"):
        fingerprint(nested.empty_table())


def test_nulls_in_non_nullable_fields_are_rejected() -> None:
    top = pa.Table.from_arrays(
        [pa.array([1, None], pa.int64())], schema=pa.schema([pa.field("n", pa.int64(), False)])
    )
    with pytest.raises(BatchRejected, match="non-nullable"):
        fingerprint(top)
    tags_type = SCHEMA.field("tags").type
    element = pa.LargeListArray.from_arrays(
        pa.array([0, 2], pa.int64()), pa.array(["a", None], pa.large_string()), type=tags_type
    )
    with pytest.raises(BatchRejected, match="non-nullable"):
        fingerprint(pa.Table.from_arrays([element], schema=pa.schema([SCHEMA.field("tags")])))
    child = pa.StructArray.from_arrays(
        [pa.array([None, 2], pa.int64()), pa.array(["p", None], pa.large_string())],
        fields=list(ITEM),
    )
    with pytest.raises(BatchRejected, match="non-nullable"):
        fingerprint(pa.Table.from_arrays([child], schema=pa.schema([SCHEMA.field("pair")])))
    # A required child may be null only where its struct is null.
    masked = pa.StructArray.from_arrays(
        [pa.array([None, 2], pa.int64()), pa.array(["p", None], pa.large_string())],
        fields=list(ITEM),
        mask=pa.array([True, False]),
    )
    fingerprint(pa.Table.from_arrays([masked], schema=pa.schema([SCHEMA.field("pair")])))
