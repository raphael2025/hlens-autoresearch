"""Bounded replay of the existing PyArrow batch fingerprint over a sorted row RunRef.

The frame format is exactly ``hlens.pyarrow-batch-sha256@1.0.0``. Length/count prepasses replay
an immutable RunRef; encoded payloads are then streamed into SHA-256 in bounded chunks. This is an
infrastructure helper and does not change the existing table fingerprint rule or its API.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.types as pat  # type: ignore[import-untyped]

from core.contracts.catalog import BatchRejected
from core.contracts.storage import StorageAdapter
from infrastructure.catalog.fingerprint import (
    PYARROW_BATCH_FINGERPRINT_RULE_ID,
    _field_document,
)
from infrastructure.streaming.runs import RunRef, iter_run

__all__ = ["StreamFingerprintStats", "fingerprint_run"]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StreamFingerprintStats:
    """Observable bounded replay work; ``max_row_bytes`` is checked on every pass."""

    run_passes: int
    rows_scanned: int
    max_row_bytes: int
    max_run_object_bytes: int


@dataclass(frozen=True, slots=True)
class _FieldStats:
    count: int
    null_count: int
    data_bytes: int = 0
    child_count: int = 0
    children: tuple[_FieldStats, ...] = ()


def fingerprint_run(
    storage: StorageAdapter,
    run: RunRef,
    schema: pa.Schema,
    *,
    max_record_bytes: int,
    max_run_object_bytes: int,
    row_chunk_capacity: int,
    max_hash_chunk_bytes: int,
) -> tuple[str, StreamFingerprintStats]:
    """Return the legacy PyArrow batch SHA-256 for a sorted RunRef without a full table.

    Run rows must be mappings with the exact ``schema`` field names and values representable by
    the schema's existing closed fingerprint type vocabulary. Every scan validates run length,
    field names and one-row decoded-value JSON size. ``max_record_bytes`` is an explicit,
    positive decoded-value budget; it does not bound RunSet's tagged JSONL representation.
    That serialized object is bounded before allocation by ``max_run_object_bytes``.
    The RunRef must be immutable/replayable; each field's lengths are counted before its frames
    are emitted, so the helper makes multiple bounded passes over the same content-addressed run.
    """
    if sys.byteorder != "little":
        raise BatchRejected(f"{PYARROW_BATCH_FINGERPRINT_RULE_ID} requires a little-endian host")
    if not isinstance(schema, pa.Schema):
        raise BatchRejected("stream fingerprint schema must be a PyArrow schema")
    _validate_unique_field_names(schema)
    if (
        isinstance(max_record_bytes, bool)
        or not isinstance(max_record_bytes, int)
        or max_record_bytes <= 0
    ):
        raise BatchRejected("max_record_bytes must be a positive integer")
    for name, value in (
        ("row_chunk_capacity", row_chunk_capacity),
        ("max_hash_chunk_bytes", max_hash_chunk_bytes),
        ("max_run_object_bytes", max_run_object_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BatchRejected(f"{name} must be a positive integer")
    if max_hash_chunk_bytes < 16:
        raise BatchRejected("max_hash_chunk_bytes must be at least 16 for decimal128 values")
    try:
        schema_document = [_field_document(field) for field in schema]
    except Exception as exc:
        raise BatchRejected("stream fingerprint schema has unsupported types") from exc
    encoded_schema = json.dumps(
        schema_document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if run.record_count < 0:
        raise BatchRejected("stream fingerprint RunRef has a negative row count")

    scan_count = 0
    rows_scanned = 0
    largest_row = 0

    @contextmanager
    def rows() -> Iterator[Iterator[Mapping[str, Any]]]:
        nonlocal scan_count, rows_scanned, largest_row
        scan_count += 1

        def checked() -> Iterator[Mapping[str, Any]]:
            nonlocal rows_scanned, largest_row
            with iter_run(storage, run, max_object_bytes=max_run_object_bytes) as source:
                count = 0
                for row in source:
                    if not isinstance(row, Mapping) or set(row) != set(schema.names):
                        raise BatchRejected("stream fingerprint row fields differ from schema")
                    row_size = _decoded_value_size(row, maximum=max_record_bytes)
                    largest_row = max(largest_row, row_size)
                    count += 1
                    rows_scanned += 1
                    yield row
                if count != run.record_count:
                    raise BatchRejected("stream fingerprint RunRef row count changed during replay")

        iterator = checked()
        try:
            yield iterator
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    def values(path: tuple[str | None, ...]) -> Iterator[Any]:
        def descend(value: Any, depth: int) -> Iterator[Any]:
            if depth == len(path):
                yield value
                return
            component = path[depth]
            if component is None:
                if value is not None:
                    for child_value in value:
                        yield from descend(child_value, depth + 1)
                return
            if value is None:
                if None in path[depth:]:
                    return
                yield None
                return
            yield from descend(value[component], depth + 1)

        with rows() as source:
            for row in source:
                root_field = path[0]
                if not isinstance(root_field, str):
                    raise BatchRejected("stream fingerprint field path has no root column")
                yield from descend(row[root_field], 1)

    if not schema:
        with rows() as source:
            for _ in source:
                pass

    # The helper stats and encoder replay each field independently; only a bounded group of
    # scalar values/payload bytes is retained at once.
    stats_by_field = tuple(
        _measure_field(
            field,
            (field.name,),
            values,
            run.record_count,
            0,
            row_chunk_capacity,
        )
        for field in schema
    )
    hasher = hashlib.sha256()

    def frame_header(length: int) -> None:
        hasher.update(struct.pack("<Q", length))

    def payload_chunks(chunks: Iterator[bytes], expected_length: int) -> None:
        frame_header(expected_length)
        seen = 0
        for chunk in chunks:
            seen += len(chunk)
            if seen > expected_length:
                raise BatchRejected("stream fingerprint payload exceeded measured frame length")
            view = memoryview(chunk)
            for offset in range(0, len(view), max_hash_chunk_bytes):
                hasher.update(view[offset : offset + max_hash_chunk_bytes])
        if seen != expected_length:
            raise BatchRejected("stream fingerprint payload differs from measured frame length")

    rule_id_bytes = PYARROW_BATCH_FINGERPRINT_RULE_ID.encode("ascii")
    payload_chunks(iter((rule_id_bytes,)), len(rule_id_bytes))
    payload_chunks(iter((encoded_schema,)), len(encoded_schema))
    payload_chunks(iter((struct.pack("<Q", run.record_count),)), 8)
    for field, field_stats in zip(schema, stats_by_field, strict=True):
        _encode_field(
            field,
            (field.name,),
            field_stats,
            values,
            payload_chunks,
            max_hash_chunk_bytes,
        )
    return hasher.hexdigest(), StreamFingerprintStats(
        scan_count,
        rows_scanned,
        largest_row,
        max_run_object_bytes,
    )


def _measure_field(
    field: pa.Field,
    path: tuple[str | None, ...],
    values: Any,
    expected_count: int,
    inherited_nulls: int,
    row_chunk_capacity: int,
) -> _FieldStats:
    count = 0
    null_count = 0
    data_bytes = 0
    child_count = 0
    value_type = field.type
    validation_values: list[Any] = []
    for value in values(path):
        count += 1
        validation_values.append(value)
        if len(validation_values) >= row_chunk_capacity:
            _validate_values(field, validation_values)
            validation_values.clear()
        if value is None:
            null_count += 1
        elif pat.is_large_string(value_type):
            data_bytes += _utf8_size(value)
        elif pat.is_large_list(value_type):
            try:
                child_count += len(value)
            except TypeError as exc:
                raise BatchRejected("stream fingerprint list value is malformed") from exc
    if count != expected_count:
        raise BatchRejected(f"stream fingerprint field {field.name!r} changed slot count")
    if validation_values:
        _validate_values(field, validation_values)
        validation_values.clear()
    if not field.nullable and null_count != inherited_nulls:
        raise BatchRejected(f"non-nullable field {field.name!r} contains nulls")
    children: tuple[_FieldStats, ...] = ()
    if pat.is_large_list(value_type):
        child = value_type.value_field
        children = (
            _measure_field(
                child,
                (*path, None),
                values,
                child_count,
                0,
                row_chunk_capacity,
            ),
        )
    elif pat.is_struct(value_type):
        children = tuple(
            _measure_field(
                value_type.field(index),
                (*path, value_type.field(index).name),
                values,
                expected_count,
                null_count,
                row_chunk_capacity,
            )
            for index in range(value_type.num_fields)
        )
    return _FieldStats(count, null_count, data_bytes, child_count, children)


def _validate_unique_field_names(schema: pa.Schema) -> None:
    def unique(fields: Sequence[pa.Field], path: str) -> None:
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            raise BatchRejected(f"stream fingerprint schema has duplicate field names at {path}")
        for field in fields:
            visit_type(field.type, f"{path}.{field.name}")

    def visit_type(data_type: pa.DataType, path: str) -> None:
        if pat.is_struct(data_type):
            unique(data_type, path)
        elif pat.is_large_list(data_type) or pat.is_list(data_type):
            visit_type(data_type.value_field.type, f"{path}[]")

    unique(schema, "root")


def _encode_field(
    field: pa.Field,
    path: tuple[str | None, ...],
    stats: _FieldStats,
    values: Any,
    payload_chunks: Any,
    max_hash_chunk_bytes: int,
) -> None:
    value_type = field.type

    def validity() -> Iterator[bytes]:
        buffer = bytearray()
        for value in values(path):
            buffer.append(0 if value is None else 1)
            if len(buffer) >= max_hash_chunk_bytes:
                yield bytes(buffer)
                buffer.clear()
        if buffer:
            yield bytes(buffer)

    payload_chunks(validity(), stats.count)
    if pat.is_boolean(value_type):
        payload_chunks(
            _fixed_values(
                values(path),
                lambda value: b"\x01" if value is True else b"\x00",
                max_hash_chunk_bytes,
            ),
            stats.count,
        )
    elif pat.is_int64(value_type):
        payload_chunks(
            _fixed_values(
                values(path),
                lambda value: struct.pack("<q", 0 if value is None else value),
                max_hash_chunk_bytes,
            ),
            stats.count * 8,
        )
    elif pat.is_timestamp(value_type):
        payload_chunks(
            _fixed_values(
                values(path),
                lambda value: struct.pack("<q", 0 if value is None else _timestamp_us(value)),
                max_hash_chunk_bytes,
            ),
            stats.count * 8,
        )
    elif pat.is_decimal128(value_type):
        payload_chunks(
            _fixed_values(
                values(path),
                lambda value: (
                    0 if value is None else _decimal_unscaled(value, value_type.scale)
                ).to_bytes(16, "little", signed=True),
                max_hash_chunk_bytes,
            ),
            stats.count * 16,
        )
    elif pat.is_large_string(value_type):

        def lengths() -> Iterator[bytes]:
            yield from _fixed_values(
                values(path),
                lambda value: struct.pack("<Q", 0 if value is None else _utf8_size(value)),
                max_hash_chunk_bytes,
            )

        payload_chunks(lengths(), stats.count * 8)
        payload_chunks(
            _text_bytes(values(path), max_hash_chunk_bytes),
            stats.data_bytes,
        )
    elif pat.is_large_list(value_type):

        def lengths() -> Iterator[bytes]:
            yield from _fixed_values(
                values(path),
                lambda value: struct.pack("<Q", 0 if value is None else len(value)),
                max_hash_chunk_bytes,
            )

        payload_chunks(lengths(), stats.count * 8)
        child_field = value_type.value_field
        _encode_field(
            child_field,
            (*path, None),
            stats.children[0],
            values,
            payload_chunks,
            max_hash_chunk_bytes,
        )
    elif pat.is_struct(value_type):
        for index in range(value_type.num_fields):
            child = value_type.field(index)
            _encode_field(
                child,
                (*path, child.name),
                stats.children[index],
                values,
                payload_chunks,
                max_hash_chunk_bytes,
            )
    else:  # schema document validation should have rejected this first
        raise BatchRejected(f"unsupported stream fingerprint type {value_type}")


def _fixed_values(values: Iterator[Any], encode: Any, max_hash_chunk_bytes: int) -> Iterator[bytes]:
    buffer = bytearray()
    for value in values:
        encoded = encode(value)
        if len(encoded) > max_hash_chunk_bytes:
            raise BatchRejected("one fixed-width value exceeds max_hash_chunk_bytes")
        if buffer and len(buffer) + len(encoded) > max_hash_chunk_bytes:
            yield bytes(buffer)
            buffer.clear()
        buffer.extend(encoded)
    if buffer:
        yield bytes(buffer)


def _text_bytes(values: Iterator[Any], max_hash_chunk_bytes: int) -> Iterator[bytes]:
    buffer = bytearray()
    for value in values:
        if value is not None:
            for character in value:
                try:
                    encoded = character.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise BatchRejected("stream fingerprint text must be valid UTF-8") from exc
                if buffer and len(buffer) + len(encoded) > max_hash_chunk_bytes:
                    yield bytes(buffer)
                    buffer.clear()
                buffer.extend(encoded)
    if buffer:
        yield bytes(buffer)


def _timestamp_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BatchRejected("stream fingerprint timestamp must be timezone-aware")
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _decimal_unscaled(value: Decimal, scale: int) -> int:
    if not value.is_finite():
        raise BatchRejected("Decimal value must be finite")
    sign, digits, exponent = cast(tuple[int, tuple[int, ...], int], value.as_tuple())
    if not isinstance(exponent, int):
        raise BatchRejected("Decimal value has no finite exponent")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    shift = exponent + scale
    if shift >= 0:
        coefficient *= 10**shift
    else:
        divisor = 10 ** (-shift)
        if coefficient % divisor:
            raise BatchRejected("Decimal value cannot be represented at the schema scale")
        coefficient //= divisor
    return int(-coefficient if sign == 1 else coefficient)


def _utf8_size(value: str) -> int:
    size = 0
    for offset in range(0, len(value), 256):
        try:
            size += len(value[offset : offset + 256].encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise BatchRejected("stream fingerprint text must be valid UTF-8") from exc
    return size


def _validate_values(field: pa.Field, values: list[Any]) -> None:
    try:
        pa.array(values, type=field.type, from_pandas=False)
    except (TypeError, ValueError, OverflowError, pa.ArrowException) as exc:
        raise BatchRejected(f"stream fingerprint field {field.name!r} has invalid values") from exc


def _decoded_value_size(value: object, *, maximum: int) -> int:
    size = 0

    def add(amount: int) -> None:
        nonlocal size
        size += amount
        if size > maximum:
            raise BatchRejected(
                f"stream fingerprint decoded row exceeds max_record_bytes={maximum}"
            )

    def text(item: str) -> None:
        add(2)
        for offset in range(0, len(item), 128):
            try:
                encoded = json.dumps(item[offset : offset + 128], ensure_ascii=False)[1:-1].encode(
                    "utf-8"
                )
            except UnicodeEncodeError as exc:
                raise BatchRejected("stream fingerprint text contains invalid UTF-8") from exc
            add(len(encoded))

    def visit(item: object) -> None:
        if isinstance(item, str):
            text(item)
        elif isinstance(item, datetime):
            text(item.isoformat())
        elif item is None or isinstance(item, bool):
            add(len(json.dumps(item).encode("ascii")))
        elif isinstance(item, int):
            add(len(str(item)))
        elif isinstance(item, Decimal):
            text(str(item))
        elif isinstance(item, list | tuple):
            add(2)
            for index, child in enumerate(item):
                if index:
                    add(1)
                visit(child)
        elif isinstance(item, Mapping):
            add(2)
            for index, key in enumerate(sorted(item)):
                if not isinstance(key, str):
                    raise BatchRejected("stream fingerprint JSON keys must be text")
                if index:
                    add(1)
                text(key)
                add(1)
                visit(item[key])
        else:
            raise BatchRejected(f"stream fingerprint row contains {type(item).__name__}")

    visit(value)
    add(1)
    return size
