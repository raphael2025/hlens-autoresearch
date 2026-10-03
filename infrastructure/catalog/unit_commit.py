"""Staged data files of one logical unit, written in bounded memory (ADR-0108 §2).

``StagedUnitWriter`` turns a unit's microbatches (each a bounded ``pyarrow.Table`` in the
registered schema) into Iceberg ``DataFile`` objects ready for **one** multi-file fast append. It
reproduces PyIceberg 0.12's ``_dataframe_to_data_files`` / ``write_file`` path — the same
``pyarrow_to_schema`` task schema, ``_to_requested_schema`` file schema with field ids, partition
split (``_determine_partitions``), ``ParquetFormatWriter`` and its Parquet-footer statistics, and
the same ``DataFile`` fields — but keeps one streaming Parquet writer open per partition instead of
materializing the unit: microbatch slices are buffered per partition up to ``row_group_rows`` and
written as one row group; a file is closed after ``file_rows`` rows. Working set: one microbatch,
the per-partition row-group buffers and the open writers' footers; the returned ``DataFile`` list
has one entry per closed file (``ceil(rows / file_rows)`` per partition), not per microbatch.

Files are named ``hlens-unit-<tag>-<write uuid>-<n>.parquet`` under the table's location provider.
``tag`` binds the file to its unit for audit; the per-writer UUID keeps a concurrent or retried
writer of the same unit from overwriting files another writer may be committing. Files written
before a failed or lost commit are referenced by no snapshot: invisible, harmless orphans (the
ADR-0077 orphan semantics), removed only by explicit future maintenance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.io.fileformat import FileFormatFactory
from pyiceberg.io.pyarrow import (
    _determine_partitions,
    _to_requested_schema,
    pyarrow_to_schema,
)
from pyiceberg.manifest import DataFile, DataFileContent, FileFormat
from pyiceberg.partitioning import PartitionKey
from pyiceberg.table import Table as IcebergTable
from pyiceberg.table import TableProperties
from pyiceberg.table.locations import load_location_provider
from pyiceberg.typedef import Record
from pyiceberg.utils.config import Config

__all__ = ["StagedUnitWriter", "UNIT_FILE_ROWS", "UNIT_ROW_GROUP_ROWS"]

#: Rows buffered per partition before one Parquet row group is written (engineering parameter).
UNIT_ROW_GROUP_ROWS: Final = 4096
#: Rows per staged data file before it is closed and a new one opened (engineering parameter).
#: One row group per file, the size of the pre-ADR-0108 D2 element batch files: Arrow's Parquet
#: fragment scan holds more as a file grows (more row groups), so whole-unit files made every
#: bounded scan of a unit O(unit) — the E1-CAP-1 run on ``main@4e78e255`` grew 230-273 MiB at
#: 500k rows. A unit stays one snapshot (ADR-0108 §1); only its data files are capped.
UNIT_FILE_ROWS: Final = 4096


@dataclass
class _PartitionStream:
    partition_key: PartitionKey | None
    path_key: str
    buffered: list[pa.Table] = field(default_factory=list)
    buffered_rows: int = 0
    writer: Any = None
    output: Any = None
    file_path: str | None = None
    file_rows: int = 0


class StagedUnitWriter:
    """Bounded-memory staged data files for one unit (see module docs)."""

    def __init__(
        self,
        table: IcebergTable,
        *,
        tag: str,
        row_group_rows: int = UNIT_ROW_GROUP_ROWS,
        file_rows: int = UNIT_FILE_ROWS,
    ) -> None:
        if row_group_rows < 1 or file_rows < row_group_rows:
            raise ValueError("need 1 <= row_group_rows <= file_rows")
        from pyiceberg.table import DOWNCAST_NS_TIMESTAMP_TO_US_ON_WRITE

        metadata = table.metadata
        self._metadata = metadata
        self._io = table.io
        self._tag = tag
        self._write_uuid = uuid.uuid4()
        self._row_group_rows = row_group_rows
        self._file_rows = file_rows
        self._file_format = FileFormat(
            metadata.properties.get(
                TableProperties.WRITE_FILE_FORMAT, TableProperties.WRITE_FILE_FORMAT_DEFAULT
            )
        )
        self._format_model = FileFormatFactory.get(self._file_format)
        self._location_provider = load_location_provider(
            table_location=metadata.location, table_properties=metadata.properties
        )
        self._downcast = Config().get_bool(DOWNCAST_NS_TIMESTAMP_TO_US_ON_WRITE) or False
        self._file_schema = metadata.schema()
        self._partitioned = not metadata.spec().is_unpartitioned()
        self._streams: dict[str, _PartitionStream] = {}
        self._data_files: list[DataFile] = []
        self._file_counter = 0
        self._task_schema: Any = None
        self._finished = False

    @property
    def written_files(self) -> tuple[str, ...]:
        """Paths of every file this writer created (closed or open)."""
        paths = [str(data_file.file_path) for data_file in self._data_files]
        paths += [s.file_path for s in self._streams.values() if s.file_path is not None]
        return tuple(paths)

    def add(self, batch: pa.Table) -> None:
        if self._finished:
            raise RuntimeError("the staged unit writer is finished")
        if batch.num_rows == 0:
            return
        if self._task_schema is None:
            self._task_schema = pyarrow_to_schema(
                batch.schema,
                name_mapping=self._metadata.schema().name_mapping,
                downcast_ns_timestamp_to_us=self._downcast,
                format_version=self._metadata.format_version,
            )
        if self._partitioned:
            for partition in _determine_partitions(
                spec=self._metadata.spec(), schema=self._metadata.schema(), arrow_table=batch
            ):
                key = partition.partition_key
                self._append(key.to_path(), key, partition.arrow_table_partition)
        else:
            self._append("", None, batch)

    def finish(self) -> list[DataFile]:
        """Flush and close every partition's file; the ``DataFile`` of each staged file."""
        if self._finished:
            raise RuntimeError("the staged unit writer is finished")
        for stream in self._streams.values():
            self._flush(stream)
            self._close_file(stream)
        self._finished = True
        return list(self._data_files)

    def abort(self) -> None:
        """Close open writers after a failure (their files stay as unreferenced orphans)."""
        self._finished = True
        for stream in self._streams.values():
            writer, stream.writer = stream.writer, None
            output, stream.output = stream.output, None
            for resource in (writer, output):
                close = getattr(resource, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------ internals

    def _append(self, path_key: str, key: PartitionKey | None, table: pa.Table) -> None:
        stream = self._streams.get(path_key)
        if stream is None:
            stream = _PartitionStream(partition_key=key, path_key=path_key)
            self._streams[path_key] = stream
        stream.buffered.append(table)
        stream.buffered_rows += table.num_rows
        while stream.buffered_rows >= self._row_group_rows:
            self._flush(stream, self._row_group_rows)

    def _flush(self, stream: _PartitionStream, rows: int | None = None) -> None:
        if stream.buffered_rows == 0:
            return
        combined = pa.concat_tables(stream.buffered)
        take = combined.num_rows if rows is None else min(rows, combined.num_rows)
        room = self._file_rows - stream.file_rows
        take = min(take, room)
        head, rest = combined.slice(0, take), combined.slice(take)
        stream.buffered = [rest] if rest.num_rows else []
        stream.buffered_rows = rest.num_rows
        self._write(stream, head)
        if stream.file_rows >= self._file_rows:
            self._close_file(stream)
        if rows is None and stream.buffered_rows:
            self._flush(stream)

    def _write(self, stream: _PartitionStream, table: pa.Table) -> None:
        batches = [
            _to_requested_schema(
                requested_schema=self._file_schema,
                file_schema=self._task_schema,
                batch=batch,
                downcast_ns_timestamp_to_us=self._downcast,
                include_field_ids=True,
                format_model=self._format_model,
            )
            for batch in table.to_batches()
        ]
        arrow_table = pa.Table.from_batches(batches)
        if stream.writer is None:
            name = (
                f"hlens-unit-{self._tag}-{self._write_uuid}-{self._file_counter:05d}."
                f"{self._format_model.file_extension()}"
            )
            self._file_counter += 1
            stream.file_path = self._location_provider.new_data_location(
                data_file_name=name, partition_key=stream.partition_key
            )
            stream.output = self._io.new_output(stream.file_path)
            stream.writer = self._format_model.create_writer(
                stream.output, self._file_schema, self._metadata.properties
            )
            stream.file_rows = 0
        # One write == one row group: the buffer already holds at most ``row_group_rows`` rows.
        stream.writer._row_group_size = max(arrow_table.num_rows, 1)
        stream.writer.write(arrow_table)
        stream.file_rows += table.num_rows

    def _close_file(self, stream: _PartitionStream) -> None:
        writer = stream.writer
        if writer is None:
            return
        statistics = writer.close()
        output, path = stream.output, stream.file_path
        self._data_files.append(
            DataFile.from_args(
                content=DataFileContent.DATA,
                file_path=path,
                file_format=self._file_format,
                partition=stream.partition_key.partition if stream.partition_key else Record(),
                file_size_in_bytes=len(output),
                sort_order_id=None,
                spec_id=self._metadata.default_spec_id,
                equality_ids=None,
                key_metadata=None,
                **statistics.to_serialized_dict(),
            )
        )
        stream.writer = None
        stream.output = None
        stream.file_path = None
        stream.file_rows = 0
