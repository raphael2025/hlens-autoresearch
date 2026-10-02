"""Streaming whole-unit fingerprint over bounded microbatches (ADR-0108 §2).

``UnitFingerprint`` computes ``hlens.pyarrow-batch-sha256@1.0.0`` of the logical concatenation of
a sequence of ``pyarrow.Table`` microbatches **bit for bit** as the rule computes it on one
concatenated table, without holding the unit: the rule is column-major (every frame of column 1,
then every frame of column 2, …), so each microbatch is encoded with the rule's own ``_encode`` and
its frame payloads are appended to one scratch spool file per frame position. The frame structure
depends only on the schema, so every microbatch yields the same frame positions; at the end the
digest is the rule's header frames followed by each spooled frame (length prefix = its total
size) in order. Memory: one microbatch's encoded frames at a time plus fixed read buffers; disk:
the encoded size of the unit, under the caller's scratch directory, removed on ``close``.

This adds no new rule: the encoding, accepted types and rejections are those of
``infrastructure.catalog.fingerprint`` (golden-vector stable), which this module only reuses.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Final

import pyarrow as pa  # type: ignore[import-untyped]

from core.contracts.catalog import BatchRejected
from infrastructure.catalog.fingerprint import (
    PYARROW_BATCH_FINGERPRINT_RULE_ID,
    _encode,
    _field_document,
    _u64,
)

__all__ = ["UnitFingerprint"]

_READ_CHUNK: Final = 1024 * 1024


class _CountingFrames:
    """Counts the frames one encoding emits (to learn the schema's frame structure)."""

    def __init__(self) -> None:
        self.count = 0

    def frame(self, payload: bytes | memoryview | pa.Buffer) -> None:
        self.count += 1


class _SpoolingFrames:
    """Appends the i-th frame payload of one microbatch to spool file i."""

    def __init__(self, spools: list[BinaryIO], sizes: list[int]) -> None:
        self._spools = spools
        self._sizes = sizes
        self.count = 0

    def frame(self, payload: bytes | memoryview | pa.Buffer) -> None:
        index = self.count
        if index >= len(self._spools):
            raise BatchRejected("a microbatch emitted more fingerprint frames than its schema")
        view = memoryview(payload)
        self._spools[index].write(view)
        self._sizes[index] += view.nbytes
        self.count += 1


class UnitFingerprint:
    """The rule's fingerprint of a unit given as consecutive microbatches (see module docs)."""

    def __init__(self, schema: pa.Schema, scratch_directory: Path) -> None:
        if sys.byteorder != "little":  # pragma: no cover - the project runs on little-endian
            raise BatchRejected(
                f"{PYARROW_BATCH_FINGERPRINT_RULE_ID} requires a little-endian host"
            )
        if not isinstance(schema, pa.Schema):
            raise BatchRejected("the unit schema must be a pyarrow.Schema")
        self._schema = schema
        self._schema_document = json.dumps(
            [_field_document(field) for field in schema],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        counting = _CountingFrames()
        for field in schema:
            _encode(counting, pa.array([], field.type), field)  # type: ignore[arg-type]
        self._frame_count = counting.count
        self._rows = 0
        self._closed = False
        self._temporary = tempfile.TemporaryDirectory(
            prefix="hlens-unit-fingerprint-", dir=str(scratch_directory)
        )
        self._spools: list[BinaryIO] = []
        self._sizes = [0] * self._frame_count
        try:
            for index in range(self._frame_count):
                self._spools.append(
                    open(os.path.join(self._temporary.name, f"frame-{index:05d}"), "w+b")  # noqa: SIM115
                )
        except BaseException:
            self.close()
            raise

    @property
    def rows(self) -> int:
        return self._rows

    def add(self, batch: pa.Table) -> None:
        """Encode one microbatch (the rule's checks and rejections apply to it)."""
        if self._closed:
            raise RuntimeError("the unit fingerprint is closed")
        if type(batch) is not pa.Table:
            raise BatchRejected(
                f"a microbatch must be a bounded pyarrow.Table, got {type(batch).__name__}"
            )
        if not batch.schema.equals(self._schema, check_metadata=False):
            raise BatchRejected("a microbatch does not have the unit schema")
        frames = _SpoolingFrames(self._spools, self._sizes)
        for index, field in enumerate(batch.schema):
            column = batch.column(index)
            combined = (
                pa.concat_arrays(column.chunks) if column.num_chunks else pa.array([], field.type)
            )
            _encode(frames, combined, field)  # type: ignore[arg-type]
        if frames.count != self._frame_count:
            raise BatchRejected("a microbatch emitted fewer fingerprint frames than its schema")
        self._rows += batch.num_rows

    def hexdigest(self) -> str:
        """The rule's fingerprint of every microbatch added so far, concatenated in order."""
        if self._closed:
            raise RuntimeError("the unit fingerprint is closed")
        digest = hashlib.sha256()

        def frame(payload: bytes) -> None:
            digest.update(_u64(len(payload)))
            digest.update(payload)

        frame(PYARROW_BATCH_FINGERPRINT_RULE_ID.encode("ascii"))
        frame(self._schema_document)
        frame(_u64(self._rows))
        for spool, size in zip(self._spools, self._sizes, strict=True):
            digest.update(_u64(size))
            spool.flush()
            spool.seek(0)
            remaining = size
            while remaining:
                chunk = spool.read(min(_READ_CHUNK, remaining))
                if not chunk:
                    raise BatchRejected("a fingerprint spool is shorter than written")
                digest.update(chunk)
                remaining -= len(chunk)
            spool.seek(0, os.SEEK_END)
        return digest.hexdigest()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for spool in self._spools:
            try:
                spool.close()
            except Exception:
                pass
        self._spools = []
        self._temporary.cleanup()

    def __enter__(self) -> UnitFingerprint:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
