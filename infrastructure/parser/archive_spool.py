"""Disk-spooled strict parse of a Binance archive (E1-CAP-1; D1 ``binance.spot.archive.parser``).

``spool_archive`` runs the D1 parser's own ``_parse_zip`` — the same rules, checks and rejection
order — over a private on-disk copy of the object, and writes the accepted rows to disk in fixed
blocks instead of building one ``pyarrow.Table``. It adds no parser rule and changes none: the
parser binding and hash are the D1 ones, and a ``SpooledArchive`` holds exactly the rows a
``ParsedArchive`` of the same object holds (the parser tests check every parse both ways).

It exists so that proving Raw rows against their archive object (D3E-R3) needs memory for one
window of rows, not for the whole object: readers take the lines they prove, block by block.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import tempfile
import weakref
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Final, Self

import pyarrow as pa  # type: ignore[import-untyped]

from core.contracts.storage import IntegrityViolation, StorageAdapter
from infrastructure.parser.binance_archive import (
    _READ_CHUNK_BYTES,
    _ROW_SCHEMAS,
    PARSER_BINDING,
    ArchiveParseRequest,
    ArchiveRejection,
    RejectionCode,
    _check_request_identity,
    _ColumnBuffer,
    _parse_zip,
    _Reject,
    _rejection,
)

__all__ = ["DEFAULT_SPOOL_CHUNK_ROWS", "SpooledArchive", "spool_archive"]

#: Rows per spooled block (memory of one block read, not a parser rule).
DEFAULT_SPOOL_CHUNK_ROWS: Final = 8_192
_OBJECT: Final = "object.zip"
_ROWS: Final = "rows.arrows"
_INDEX: Final = "rows.index"
#: Per block, fixed width: start and end byte offset (little-endian ``uint64``) and SHA-256.
_ENTRY: Final = struct.Struct("<QQ32s")
_ENTRY_NUMBER: Final = struct.Struct("<Q")


class SpooledArchive:
    """整文件严格解析成功、行按固定块落盘的结果（E1-CAP-1）。

    与 ``parse_archive`` 同一解析（同一 ``_parse_zip``、同一拒绝规则与顺序），只是被接受的行
    每 ``chunk_rows`` 行写成一个自包含的 Arrow IPC stream 块；每块的起止字节偏移与绑定块号的
    SHA-256 以定宽条目写入独立的索引文件，内存里只有标识字段与三个整数。``take`` 每次只读入
    所需的块（一块至多 ``chunk_rows`` 行，读后核对摘要、行数与 Schema），所以读取一个窗口的行
    不随归档行数增长。

    文件在私有临时目录中（不是 warehouse，不是可发布对象）；``close()`` / context manager /
    垃圾回收删除它们。目录应位于磁盘上：tmpfs 上的 spool 占用的是内存。
    """

    def __init__(
        self,
        request: ArchiveParseRequest,
        member_name: str,
        *,
        row_count: int,
        chunk_rows: int,
        directory: Path,
    ) -> None:
        self.parser = PARSER_BINDING
        self.archive_revision_id = request.archive_revision_id
        self.data_type = request.data_type
        self.symbol = request.symbol
        self.coverage_start = request.coverage_start
        self.coverage_end = request.coverage_end
        self.object_ref = request.object_ref
        self.member_name = member_name
        self.time_unit = request.time_unit
        self.schema = _ROW_SCHEMAS[request.data_type]
        self._row_count = row_count
        self._chunk_rows = chunk_rows
        self._directory = directory
        self._cleanup = weakref.finalize(self, shutil.rmtree, directory, True)

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def chunk_rows(self) -> int:
        return self._chunk_rows

    @property
    def closed(self) -> bool:
        return not self._cleanup.alive

    def close(self) -> None:
        """Delete the spool files (idempotent); later reads fail."""
        self._cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def take(self, indices: Sequence[int]) -> pa.Table:
        """Rows at the 0-based ``indices``, in that order; reads only the chunks they fall in.

        Consecutive indices in one chunk share one read; ascending indices read each chunk once.
        """
        if self.closed:
            raise ValueError("the archive spool is closed")
        pieces: list[pa.Table] = []
        run: list[int] = []
        current = -1
        for index in indices:
            if isinstance(index, bool) or not isinstance(index, int):
                raise TypeError("row indices must be ints")
            if not 0 <= index < self._row_count:
                raise IndexError(f"row {index} is outside the {self._row_count}-row archive")
            chunk = index // self._chunk_rows
            if chunk != current and run:
                pieces.append(self._take_run(current, run))
                run = []
            current = chunk
            run.append(index - chunk * self._chunk_rows)
        if run:
            pieces.append(self._take_run(current, run))
        if not pieces:
            return self.schema.empty_table()
        return pa.concat_tables(pieces)

    def _take_run(self, chunk: int, offsets: Sequence[int]) -> pa.Table:
        return self._chunk(chunk).take(pa.array(offsets, type=pa.int64()))

    def _chunk(self, chunk: int) -> pa.Table:
        """One block: its recorded bytes (digest checked), then its planned rows and schema."""
        with open(self._directory / _INDEX, "rb") as index:
            index.seek(chunk * _ENTRY.size)
            start, end, digest = _ENTRY.unpack(index.read(_ENTRY.size))
        with open(self._directory / _ROWS, "rb") as rows:
            rows.seek(start)
            data = rows.read(end - start)
        if _block_digest(chunk, data) != digest:
            raise ValueError(f"archive spool block {chunk} does not hold its spooled bytes")
        table = pa.ipc.open_stream(pa.py_buffer(data)).read_all()
        expected = min(self._chunk_rows, self._row_count - chunk * self._chunk_rows)
        if table.num_rows != expected or not table.schema.equals(self.schema):
            raise ValueError(f"archive spool chunk {chunk} does not hold its planned rows")
        return table


class _SpoolSink:
    """``_RowSink`` writing each ``chunk_rows`` rows as one IPC stream block plus its offsets."""

    def __init__(self, schema: pa.Schema, directory: Path, chunk_rows: int) -> None:
        self._buffer = _ColumnBuffer(schema, chunk_rows, self._write)
        self._schema = schema
        self._rows = open(directory / _ROWS, "wb")  # noqa: SIM115 - closed in finish/close
        self._index = open(directory / _INDEX, "wb")  # noqa: SIM115
        self._offset = 0
        self._blocks = 0
        self._count = 0

    def append(self, values: tuple[Any, ...]) -> None:
        self._buffer.append(values)

    def _write(self, batch: pa.RecordBatch) -> None:
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, self._schema) as writer:
            writer.write_batch(batch)
        data = sink.getvalue()
        self._rows.write(data)
        start, self._offset = self._offset, self._offset + data.size
        digest = _block_digest(self._blocks, data)
        self._index.write(_ENTRY.pack(start, self._offset, digest))
        self._blocks += 1
        self._count += batch.num_rows

    def finish(self) -> int:
        self._buffer.flush()
        self.close()
        return self._count

    def close(self) -> None:
        self._rows.close()
        self._index.close()


def spool_archive(
    request: ArchiveParseRequest,
    storage: StorageAdapter,
    *,
    directory: Path | None = None,
    chunk_rows: int = DEFAULT_SPOOL_CHUNK_ROWS,
) -> SpooledArchive | ArchiveRejection:
    """``parse_archive`` with bounded memory: the same verdict, the rows spooled to disk.

    The object is copied once into a private file under ``directory`` (default: the platform
    temporary directory) while its SHA-256 and length are computed, and **that** copy is parsed —
    as ``parse_archive`` parses exactly the bytes it hashed. Same checks in the same order, same
    ``ArchiveRejection`` for every refused input; storage failures propagate. On success the
    object copy is deleted and the rows stay spooled (``SpooledArchive``); on any failure nothing
    is left behind. Memory: one read chunk of the object, the parser's one pending chunk of
    ``chunk_rows`` rows and one IPC block, whatever the archive's size.
    """
    if isinstance(chunk_rows, bool) or not isinstance(chunk_rows, int) or chunk_rows < 1:
        raise ValueError("chunk_rows must be a positive int")
    spool = Path(tempfile.mkdtemp(prefix="hlens-archive-spool-", dir=directory))
    kept = False
    try:
        try:
            _check_request_identity(request)
            ref = request.object_ref
            try:
                handle = storage.open_read(ref)
            except IntegrityViolation as exc:
                raise _Reject(
                    RejectionCode.OBJECT_INTEGRITY_MISMATCH,
                    f"storage refused the object: {type(exc).__name__}",
                ) from exc
            with handle, open(spool / _OBJECT, "w+b") as copy:
                size, digest = _copy_bounded(handle, copy, ref.size + 1)
                if size != ref.size:
                    raise _Reject(
                        RejectionCode.OBJECT_INTEGRITY_MISMATCH,
                        f"byte length {size} != ObjectRef.size {ref.size}",
                    )
                if digest != ref.sha256:
                    raise _Reject(
                        RejectionCode.OBJECT_INTEGRITY_MISMATCH, "SHA-256 != ObjectRef.sha256"
                    )
                sinks: list[_SpoolSink] = []

                def sink(schema: pa.Schema) -> _SpoolSink:
                    sinks.append(_SpoolSink(schema, spool, chunk_rows))
                    return sinks[-1]

                try:
                    row_count, member_name = _parse_zip(copy, request, sink)
                finally:
                    for opened in sinks:
                        opened.close()
        except _Reject as reject:
            return _rejection(request, reject)
        (spool / _OBJECT).unlink()
        spooled = SpooledArchive(
            request, member_name, row_count=row_count, chunk_rows=chunk_rows, directory=spool
        )
        kept = True
        return spooled
    finally:
        if not kept:
            shutil.rmtree(spool, ignore_errors=True)


def _block_digest(block: int, data: Any) -> bytes:
    """SHA-256 of a block's bytes, bound to its number (a moved block is not a valid one)."""
    return hashlib.sha256(_ENTRY_NUMBER.pack(block) + bytes(data)).digest()


def _copy_bounded(source: IO[bytes], target: IO[bytes], limit: int) -> tuple[int, str]:
    """Copy at most ``limit`` bytes (callers pass ``size + 1``); ``(bytes copied, SHA-256)``."""
    digest = hashlib.sha256()
    copied = 0
    while copied < limit and (chunk := source.read(min(_READ_CHUNK_BYTES, limit - copied))):
        digest.update(chunk)
        target.write(chunk)
        copied += len(chunk)
    target.flush()
    target.seek(0)
    return copied, digest.hexdigest()
