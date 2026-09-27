"""Production PyArrow batch fingerprint rule (Phase 1 C3): ``hlens.pyarrow-batch-sha256@1.0.0``.

The C2 adapter recomputes this fingerprint from every actual ``pyarrow.Table`` it is asked to
commit (first commit **and** replay) and compares it with the caller's claim. The rule is part
of each table's hashed definition document, so changing it means a new rule version and new
table definition versions.

Why not raw Arrow IPC bytes: Arrow leaves the value bytes under null slots, the data outside a
sliced array's range and the presence of an all-valid bitmap unspecified. Two logically equal
tables can therefore serialize to different IPC bytes. This rule hashes a canonical **logical**
encoding instead, built only from values the Arrow columnar format defines.

Encoding (every ``frame`` is an unsigned 64-bit little-endian length followed by the bytes):

1. ``frame(RULE_ID)``; ``frame(schema document)`` — canonical UTF-8 JSON (sorted keys, compact,
   no NaN) of the field names, nullability and a closed type vocabulary. Schema and field
   metadata (including Iceberg ``doc`` metadata) are not part of it; ``frame(u64 num_rows)``.
2. For each top-level column in schema order, the column's chunks are concatenated logically
   (chunking never reaches the hash) and encoded recursively:

   - ``frame(validity)``: one byte per slot, ``1`` valid / ``0`` null;
   - ``bool``: ``frame`` of one byte per slot (null slots encode ``0``);
   - ``int64`` / ``timestamp[us, tz=UTC]``: ``frame`` of 8-byte little-endian two's complement
     values (timestamps as microseconds since the Unix epoch; null slots encode ``0``);
   - ``decimal128(p, s)``: ``frame`` of the 16-byte little-endian two's complement unscaled
     values (null slots encode ``0``); precision and scale are fixed by the schema document;
   - ``large_string``: ``frame`` of 8-byte little-endian UTF-8 byte lengths (null slots ``0``),
     then ``frame`` of the concatenated UTF-8 bytes. Strings are not Unicode-normalized:
     byte-different strings are different values;
   - ``large_list``: ``frame`` of 8-byte lengths (null lists ``0``), then the logically
     flattened child values (values backing a null list are excluded), encoded recursively;
   - ``struct``: each child in order, with the struct's nulls merged into the child.

3. The fingerprint is the lowercase hex SHA-256 of the frame sequence.

Accepted types are exactly those the Phase 1 production schemas need; every other Arrow type
(including ``string`` / ``binary`` / ``large_binary`` / ``list``, floating point, naive or
non-microsecond timestamps, dictionaries and maps) is rejected with ``BatchRejected``, as are
nulls in non-nullable fields. The rule reads native Arrow buffers and therefore refuses to run on
big-endian hosts.

Stability boundary: the encoding depends only on the Arrow columnar format and on the logical
semantics of ``pyarrow.compute.fill_null`` / ``is_valid`` / ``list_value_length`` and
``ListArray.flatten`` / ``StructArray.flatten``. It is verified by golden vectors against the
locked ``pyarrow==25.0.1`` (``uv.lock``); upgrading PyArrow requires the golden vectors to pass
unchanged, and any change of encoding or accepted types requires a new rule version.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]

from core.contracts.catalog import BatchRejected

__all__ = [
    "PYARROW_BATCH_FINGERPRINT",
    "PYARROW_BATCH_FINGERPRINT_RULE_ID",
    "PyArrowBatchFingerprint",
]

PYARROW_BATCH_FINGERPRINT_RULE_ID: Final = "hlens.pyarrow-batch-sha256@1.0.0"
_UTC: Final = "UTC"
_DECIMAL_WIDTH: Final = 16


def _type_document(data_type: pa.DataType) -> dict[str, Any]:
    """Closed vocabulary of accepted types; anything else is rejected."""
    if pa.types.is_boolean(data_type):
        return {"type": "bool"}
    if pa.types.is_int64(data_type):
        return {"type": "int64"}
    if pa.types.is_timestamp(data_type):
        if data_type.unit != "us" or data_type.tz != _UTC:
            raise BatchRejected(f"unsupported timestamp type {data_type}: need timestamp[us, UTC]")
        return {"type": "timestamp", "unit": "us", "tz": _UTC}
    if pa.types.is_decimal128(data_type):
        return {"type": "decimal128", "precision": data_type.precision, "scale": data_type.scale}
    if pa.types.is_large_string(data_type):
        return {"type": "large_string"}
    if pa.types.is_large_list(data_type):
        return {"type": "large_list", "item": _field_document(data_type.value_field)}
    if pa.types.is_struct(data_type):
        return {
            "type": "struct",
            "fields": [_field_document(data_type.field(i)) for i in range(data_type.num_fields)],
        }
    raise BatchRejected(
        f"unsupported Arrow type {data_type} for {PYARROW_BATCH_FINGERPRINT_RULE_ID}"
    )


def _field_document(field: pa.Field) -> dict[str, Any]:
    return {"name": field.name, "nullable": field.nullable, "type": _type_document(field.type)}


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


class _Frames:
    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def frame(self, payload: bytes | memoryview | pa.Buffer) -> None:
        view = memoryview(payload)
        self._hash.update(_u64(view.nbytes))
        self._hash.update(view)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _fixed_width_bytes(array: pa.Array, width: int) -> memoryview:
    """The ``len(array) * width`` value bytes of a fixed-width array without nulls."""
    buffer = array.buffers()[1]
    if buffer is None:
        return memoryview(b"")
    start = array.offset * width
    return memoryview(buffer)[start : start + len(array) * width]


def _validity(array: pa.Array) -> memoryview:
    return _fixed_width_bytes(pc.cast(pc.is_valid(array), pa.uint8()), 1)


def _check_required(array: pa.Array, field: pa.Field, parent_nulls: int) -> None:
    """Reject nulls in a non-nullable field (nulls inherited from a null parent are allowed)."""
    if not field.nullable and array.null_count != parent_nulls:
        raise BatchRejected(f"non-nullable field {field.name!r} contains nulls")


def _large_offsets(array: pa.Array) -> tuple[int, int]:
    """First and last int64 offset of a large_string / large_list array (slice-aware)."""
    offsets = pa.Array.from_buffers(
        pa.int64(), len(array) + 1, [None, array.buffers()[1]], offset=array.offset
    )
    return offsets[0].as_py(), offsets[len(array)].as_py()


def _encode(frames: _Frames, array: pa.Array, field: pa.Field, parent_nulls: int = 0) -> None:
    data_type = field.type
    _check_required(array, field, parent_nulls)
    frames.frame(_validity(array))
    if pa.types.is_boolean(data_type):
        filled = pc.cast(pc.fill_null(array, False), pa.uint8())
        frames.frame(_fixed_width_bytes(filled, 1))
    elif pa.types.is_int64(data_type):
        frames.frame(_fixed_width_bytes(pc.fill_null(array, 0), 8))
    elif pa.types.is_timestamp(data_type):
        frames.frame(_fixed_width_bytes(pc.fill_null(array.view(pa.int64()), 0), 8))
    elif pa.types.is_decimal128(data_type):
        filled = pc.fill_null(array, pa.scalar(0, type=data_type))
        frames.frame(_fixed_width_bytes(filled, _DECIMAL_WIDTH))
    elif pa.types.is_large_string(data_type):
        filled = pc.fill_null(array, "")
        frames.frame(_fixed_width_bytes(pc.binary_length(filled), 8))
        data = filled.buffers()[2]
        if len(filled) == 0 or data is None:
            frames.frame(b"")
        else:
            start, end = _large_offsets(filled)
            frames.frame(memoryview(data)[start:end])
    elif pa.types.is_large_list(data_type):
        lengths = pc.fill_null(pc.list_value_length(array), 0)
        frames.frame(_fixed_width_bytes(lengths, 8))
        values = array.flatten()
        if len(values) != (pc.sum(lengths).as_py() or 0):
            raise BatchRejected(f"list field {field.name!r} has inconsistent offsets")
        _encode(frames, values, data_type.value_field)
    elif pa.types.is_struct(data_type):
        children = array.flatten()
        for index, child in enumerate(children):
            _encode(frames, child, data_type.field(index), parent_nulls=array.null_count)
    else:  # pragma: no cover - _type_document rejects every other type first
        raise BatchRejected(f"unsupported Arrow type {data_type}")


class PyArrowBatchFingerprint:
    """``BatchFingerprintRule`` for bounded ``pyarrow.Table`` microbatches (see module docs)."""

    @property
    def rule_id(self) -> str:
        return PYARROW_BATCH_FINGERPRINT_RULE_ID

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.rule_id!r})"

    def fingerprint(self, batch: pa.Table) -> str:
        if type(batch) is not pa.Table:
            raise BatchRejected(
                f"batch must be a bounded pyarrow.Table, got {type(batch).__name__}"
            )
        if sys.byteorder != "little":  # pragma: no cover - the project runs on x86-64 / arm64
            raise BatchRejected(f"{self.rule_id} requires a little-endian host")
        schema_document = [_field_document(field) for field in batch.schema]
        encoded_schema = json.dumps(
            schema_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        frames = _Frames()
        frames.frame(self.rule_id.encode("ascii"))
        frames.frame(encoded_schema)
        frames.frame(_u64(batch.num_rows))
        for index, field in enumerate(batch.schema):
            column = batch.column(index)
            combined = (
                pa.concat_arrays(column.chunks) if column.num_chunks else pa.array([], field.type)
            )
            _encode(frames, combined, field)
        return frames.hexdigest()


#: The single production instance bound into every Phase 1 table definition.
PYARROW_BATCH_FINGERPRINT: Final = PyArrowBatchFingerprint()
