"""PyIceberg-backed ``CatalogAdapter[pyarrow.Table]`` (ADR-0021 D-01; Phase 1 C2).

Semantics follow ``core/contracts/catalog.py``; this module only maps them onto real Iceberg
metadata:

- the definition binding is persisted as table properties at creation (one PyIceberg commit)
  and, on every access, re-resolved in the registry **and** compared with the stored schema,
  partition spec, format version and adapter-owned properties;
- batch id / fingerprint / row count / fingerprint rule are persisted in the Iceberg snapshot
  summary, so idempotency is recovered from snapshot history after a restart (no sidecar);
- every commit goes through PyIceberg with commit retries pinned to zero, so PyIceberg's
  ``AssertRefSnapshotId`` requirement and the SQL catalog's compare-and-swap on the metadata
  pointer decide optimistic conflicts; ``CommitFailedException`` becomes ``CommitConflict``;
- catalog database failures raise ``CatalogUnavailable`` (fail closed). There is no fallback
  catalog and no local state besides the Iceberg files PyIceberg itself writes;
- ``evolve_partition_spec`` (C3, infrastructure-only, not part of the core Protocol) moves a
  table from a registered source definition to a registered partition-spec-only target in one
  PyIceberg transaction (new default spec + ``hlens.definition.*`` binding properties, one
  metadata commit). Data files are never rewritten; old files keep their old spec;
- ``scan_column_batches`` (ADR-0075) does not use PyIceberg's high-level scan planner: it pins
  one snapshot and streams its manifests, entries and data files one at a time through the
  "bounded snapshot scan" helper section below, the only place that depends on private
  PyIceberg read symbols (reviewed against PyIceberg 0.12.0).

Runtime construction goes through ``open_postgres_catalog_adapter`` (PostgreSQL only). Tests may
inject any PyIceberg ``Catalog`` (for example a temporary SQLite ``SqlCatalog``) through the
``PyIcebergCatalogAdapter`` constructor; SQLite results are not PostgreSQL evidence.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self
from urllib.parse import urlparse

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.dataset as ds  # type: ignore[import-untyped]
import pyiceberg
from pydantic import ValidationError
from pyiceberg.avro.file import AvroFile
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import (
    CommitFailedException,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.expressions import AlwaysFalse, AlwaysTrue, BooleanExpression
from pyiceberg.expressions.visitors import (
    ResidualEvaluator,
    bind,
    translate_column_names,
)
from pyiceberg.io import FileIO
from pyiceberg.io.pyarrow import (
    ArrowScan,
    _get_column_projection_values,
    _to_requested_schema,
    expression_to_pyarrow,
    pyarrow_to_schema,
    schema_to_pyarrow,
)
from pyiceberg.manifest import (
    DEFAULT_READ_VERSION,
    MANIFEST_ENTRY_SCHEMAS,
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestContent,
    ManifestEntry,
    ManifestEntryStatus,
    ManifestFile,
    _inherit_from_manifest,
    read_manifest_list,
)
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema, prune_columns
from pyiceberg.table import ManifestGroupPlanner
from pyiceberg.table import Table as IcebergTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.name_mapping import NameMapping
from pyiceberg.table.snapshots import Snapshot, ancestors_of
from pyiceberg.table.update.spec import UpdateSpec
from pyiceberg.typedef import KeyDefaultDict, TableVersion

from core.contracts.catalog import (
    BatchConflict,
    BatchRejected,
    CatalogError,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    SnapshotNotFound,
    TableDefinition,
    TableDefinitionConflict,
    TableInfo,
    TableNotFound,
    UnknownTableDefinition,
    validate_table_name,
)
from infrastructure.catalog.definitions import (
    ICEBERG_FORMAT_VERSION,
    PROPERTY_DEFINITION_HASH,
    PROPERTY_DEFINITION_ID,
    PROPERTY_DEFINITION_VERSION,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    require_partition_only_change,
)
from infrastructure.settings import Settings

if TYPE_CHECKING:
    from core.contracts.storage import StorageAdapter
    from infrastructure.catalog.bounded_metadata import (
        BoundedIcebergMetadata,
        BoundedMetadataLimits,
    )

__all__ = [
    "SUMMARY_BATCH_FINGERPRINT",
    "SUMMARY_BATCH_ID",
    "SUMMARY_BATCH_ROW_COUNT",
    "SUMMARY_FINGERPRINT_RULE",
    "CatalogIntegrityError",
    "CatalogUnavailable",
    "DefinitionEvolutionError",
    "EvolutionOutcome",
    "PartitionEvolutionResult",
    "PyIcebergCatalogAdapter",
    "connect_postgres_catalog",
    "open_postgres_catalog_adapter",
]

SUMMARY_BATCH_ID: Final = "hlens.batch.id"
SUMMARY_BATCH_FINGERPRINT: Final = "hlens.batch.fingerprint"
SUMMARY_BATCH_ROW_COUNT: Final = "hlens.batch.row-count"
SUMMARY_FINGERPRINT_RULE: Final = "hlens.batch.fingerprint-rule"
#: ADR-0108: the commit layout of a snapshot that appends a whole logical unit at once, and the
#: microbatch (window) size the unit was processed and verified in. Snapshots without the layout
#: key are the original one-snapshot-per-batch layout.
SUMMARY_COMMIT_LAYOUT: Final = "hlens.commit.layout"
SUMMARY_WINDOW_ROWS: Final = "hlens.commit.window-rows"
UNIT_COMMIT_LAYOUT: Final = "hlens.unit-commit@1.0.0"
_ADDED_RECORDS: Final = "added-records"
_TOTAL_RECORDS: Final = "total-records"

_SNAPSHOT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Module-level singleton so the scan default is not a call in an argument default.
_ALWAYS_TRUE: Final[BooleanExpression] = AlwaysTrue()
#: Top-level packages whose exceptions mean "the catalog database failed". They are matched by
#: module name so project code does not import these transitive-only packages (03-data.md §6.1).
_BACKEND_PACKAGES: Final = frozenset({"sqlalchemy", "psycopg2"})
#: ADR-0075: the PyIceberg release the bounded snapshot scan helper was reviewed against
#: (``uv.lock``). Any other version fails closed until the helper is reviewed again.
_REVIEWED_PYICEBERG_VERSION: Final = "0.12.0"
#: ADR-0075 §3: the only Iceberg format version the bounded snapshot scan reads.
_SCAN_FORMAT_VERSION: Final = 2
#: Maximum rows per record batch of the bounded scan (Arrow's default would be 131 072).
_SCAN_BATCH_ROWS: Final = 65_536
#: Record batches Arrow may read ahead inside the one open data file.
_SCAN_BATCH_READAHEAD: Final = 1
#: Fragments Arrow may read ahead; each scanner of the bounded scan has exactly one.
_SCAN_FRAGMENT_READAHEAD: Final = 1
#: Buffered-stream size for Parquet column reads, used instead of pre-buffering the file.
_SCAN_READ_BUFFER_BYTES: Final = 1024 * 1024


class CatalogUnavailable(CatalogError):
    """The catalog database could not be reached or failed; nothing is assumed committed."""


class CatalogIntegrityError(CatalogError):
    """Stored Iceberg metadata disagrees with the registered definition or batch metadata."""


class DefinitionEvolutionError(CatalogError):
    """A partition-spec evolution request is not a valid, registered source → target step.

    Raised before anything is committed; the table keeps its current definition.
    """


class EvolutionOutcome(StrEnum):
    """Result of ``evolve_partition_spec``: evolved now, or already at the target (replay)."""

    EVOLVED = "evolved"
    ALREADY_EVOLVED = "already_evolved"


@dataclass(frozen=True)
class PartitionEvolutionResult:
    """Binding before / after an explicit partition-spec evolution and the new default spec."""

    table: str
    source: TableDefinition
    target: TableDefinition
    outcome: EvolutionOutcome
    spec_id: int


def _is_backend_error(exc: BaseException) -> bool:
    return any(cls.__module__.split(".")[0] in _BACKEND_PACKAGES for cls in type(exc).__mro__)


@contextmanager
def _backend(operation: str) -> Iterator[None]:
    """Map catalog database failures to ``CatalogUnavailable`` without leaking driver text."""
    try:
        yield
    except CatalogError:
        raise
    except Exception as exc:
        if _is_backend_error(exc):
            raise CatalogUnavailable(
                f"catalog database unavailable during {operation} ({type(exc).__name__})"
            ) from None
        raise


def _identifier(table: str) -> tuple[str, str]:
    namespace, name = table.split(".")
    return namespace, name


def _reduce_max_int64(
    batches: Iterable[pa.RecordBatch],
    *,
    name: str,
    column: str,
    check: Callable[[int], None] | None,
) -> int | None:
    """Fold a stream of one-column record batches into their maximum (bounded memory).

    Only the running maximum crosses a batch boundary, so the whole scanned history is never
    materialised as one table. Each batch is validated structurally (single ``int64`` projection
    of ``column``, no nulls) and each value is handed to ``check`` before it can win the
    reduction: an illegal value fails closed instead of silently anchoring on garbage.
    """
    largest: int | None = None
    for batch in batches:
        if batch.num_columns != 1 or batch.schema.field(0).name != column:
            raise CatalogIntegrityError(f"{name} did not project exactly the column {column!r}")
        array = batch.column(0)
        if not pa.types.is_int64(array.type):
            raise CatalogIntegrityError(f"{name}.{column} is not an int64 column")
        if array.null_count:
            raise CatalogIntegrityError(f"{name}.{column} carries a null value")
        for value in array.to_pylist():
            if not isinstance(value, int) or isinstance(value, bool):
                raise CatalogIntegrityError(f"{name}.{column} carries a non-integer value")
            if check is not None:
                check(value)
            if largest is None or value > largest:
                largest = value
    return largest


# ---------------------------------------------------------------------- bounded snapshot scan
#
# ADR-0075. Every use of private or semi-public PyIceberg read symbols is confined to this
# section, reviewed against PyIceberg 0.12.0; ``_open_snapshot_batches`` refuses any other
# version. What the high-level ``DataScan.to_arrow_batch_reader`` path holds and this does not:
#
# - ``Snapshot.manifests`` lists every manifest (and fills the process-wide manifest cache);
#   here the manifest list is iterated item by item with ``read_manifest_list``;
# - ``ManifestFile.fetch_manifest_entry`` returns a list per manifest, ``plan_files`` keeps every
#   data entry, every ``FileScanTask`` and a ``DeleteFileIndex``; here entries of one manifest are
#   streamed one at a time and matched data files are read one at a time, in the order the planner
#   would have produced them (manifest-list order, then entry order);
# - ``ArrowScan.to_record_batches`` maps tasks over a thread pool, lists each task's batches and
#   opens Parquet with ``pre_buffer``; here one data file is decoded at a time, with an explicit
#   batch size and readahead, no pre-buffering and a bounded buffered stream.
#
# Kept semantics: the snapshot is resolved once, as ``TableScan.snapshot`` does; the projection is
# ``DataScan.projection`` (field IDs; the snapshot's schema when a snapshot id is given);
# manifest / partition / metrics evaluators come from the same ``ManifestGroupPlanner`` builders;
# the row filter is bound by ``ArrowScan``; per-file decoding repeats ``_task_to_record_batches``
# for a task without deletes (field-ID or name-mapping file schema, identity partition values for
# missing columns, filter push-down, ns -> us timestamp rule, ``_to_requested_schema`` with the
# same flags); and batches pass through the same ``RecordBatchReader.from_batches(...).cast``.
#
# Rejected (``CatalogIntegrityError``) before any row batch: format versions other than 2,
# delete manifests, live delete files (position / equality), unknown content (PyIceberg's enum
# decoding raises), and live Avro data files (PyIceberg 0.12.0 Arrow reader does not support
# Avro). Parquet and ORC retain their PyIceberg-supported decoding paths. All manifests of the
# snapshot and all their live entries are checked first, independently of the row filter.
#
# Memory: the manifest list's bytes and one manifest's bytes are held while being iterated
# (PyIceberg's ``AvroFile`` reads a whole Avro file before decoding it), plus one data file's
# scanner state (Parquet row groups / ORC stripes can exceed one output batch; the batch size
# bounds output rows, not bytes in a row group / stripe). No Python collection grows with the
# number of manifests or data files.


class _SnapshotBatchStream(Iterator[pa.RecordBatch]):
    """Record batches of one pinned snapshot; releases the reader and open file on every exit.

    Exhaustion, a read failure and an explicit ``close`` (stopping early) all close the Arrow
    reader and the generator behind it, which closes the one data file that may be open.
    ``close`` is idempotent; a closed stream yields nothing more.
    """

    def __init__(self, reader: pa.RecordBatchReader, source: Generator[pa.RecordBatch]) -> None:
        self._reader = reader
        self._source = source
        self._closed = False

    def __iter__(self) -> Self:
        return self

    @property
    def schema(self) -> pa.Schema:
        """Projected schema, including when the selected snapshot has no matching rows."""
        return self._reader.schema

    def __next__(self) -> pa.RecordBatch:
        if self._closed:
            raise StopIteration
        try:
            return next(self._reader)
        except StopIteration:
            self.close()
            raise
        except BaseException:
            try:
                self.close()
            except BaseException:
                # Preserve the read failure while still attempting to release the resources.
                pass
            raise

    def close(self) -> None:
        """Release the reader and the open data file, including when iteration stops early."""
        if not self._closed:
            self._closed = True
            try:
                self._reader.close()
            finally:
                self._source.close()


@dataclass(frozen=True)
class _ScanPlan:
    """Evaluators built exactly as ``ManifestGroupPlanner.plan_files`` builds them.

    Keyed by partition spec id, so bounded by the table's partition specs, not its files.
    """

    manifest_evaluators: KeyDefaultDict[int, Callable[[ManifestFile], bool]]
    partition_evaluators: KeyDefaultDict[int, Callable[[DataFile], bool]]
    metrics_evaluator: Callable[[DataFile], bool]
    residual_evaluators: KeyDefaultDict[int, Callable[[DataFile], ResidualEvaluator]]


@dataclass(frozen=True)
class _FileRead:
    """The per-scan arguments ``ArrowScan`` passes to ``_task_to_record_batches``, resolved once."""

    io: FileIO
    bound_row_filter: BooleanExpression
    projected_schema: Schema
    table_schema: Schema
    projected_field_ids: set[int]
    case_sensitive: bool
    name_mapping: NameMapping | None
    specs: dict[int, PartitionSpec]
    format_version: TableVersion
    downcast_ns_timestamp_to_us: bool


def _open_snapshot_batches(
    name: str,
    iceberg: IcebergTable,
    *,
    columns: tuple[str, ...],
    row_filter: BooleanExpression,
    snapshot_id: int | None,
) -> _SnapshotBatchStream:
    """Stream ``columns`` of one snapshot of ``iceberg`` as bounded Arrow record batches.

    ``snapshot_id`` must be a snapshot of the loaded metadata; ``None`` pins the current
    snapshot of that metadata and projects the current schema, as ``Table.scan`` does. The
    snapshot is fixed here. The PyIceberg and format versions, every manifest of the snapshot and
    every live entry are checked, and all planning evaluators run, before this returns; row
    batches are then produced lazily, one data file at a time.
    """
    if pyiceberg.__version__ != _REVIEWED_PYICEBERG_VERSION:
        raise CatalogIntegrityError(
            f"bounded scan of {name} was reviewed for PyIceberg {_REVIEWED_PYICEBERG_VERSION}, "
            f"not {pyiceberg.__version__}"
        )
    if iceberg.metadata.format_version != _SCAN_FORMAT_VERSION:
        raise CatalogIntegrityError(
            f"bounded scan of {name} supports Iceberg format version {_SCAN_FORMAT_VERSION} only"
        )
    scan = iceberg.scan(row_filter=row_filter, selected_fields=columns, snapshot_id=snapshot_id)
    metadata = scan.table_metadata
    projected_schema = scan.projection()
    snapshot = scan.snapshot()
    planner = ManifestGroupPlanner(
        table_metadata=metadata,
        io=scan.io,
        row_filter=scan.row_filter,
        case_sensitive=scan.case_sensitive,
        options=scan.options,
    )
    plan = _ScanPlan(
        manifest_evaluators=KeyDefaultDict(planner._build_manifest_evaluator),
        partition_evaluators=KeyDefaultDict(planner._build_partition_evaluator),
        metrics_evaluator=planner._build_metrics_evaluator(),
        residual_evaluators=KeyDefaultDict(planner._build_residual_evaluator),
    )
    if snapshot is not None:
        _preflight_snapshot(name, scan.io, snapshot, plan)
    target_schema = schema_to_pyarrow(projected_schema)
    arrow_scan = ArrowScan(
        metadata, scan.io, projected_schema, scan.row_filter, scan.case_sensitive
    )
    downcast = arrow_scan._downcast_ns_timestamp_to_us
    read = _FileRead(
        io=scan.io,
        bound_row_filter=arrow_scan._bound_row_filter,
        projected_schema=projected_schema,
        table_schema=metadata.schema(),
        projected_field_ids=arrow_scan._projected_field_ids,
        case_sensitive=scan.case_sensitive,
        name_mapping=metadata.name_mapping(),
        specs=metadata.specs(),
        format_version=metadata.format_version,
        # As in _task_to_record_batches: unset means "downcast" for format versions <= 2.
        downcast_ns_timestamp_to_us=(
            downcast if downcast is not None else metadata.format_version <= 2
        ),
    )
    source = _snapshot_batches(name, snapshot, plan, read)
    reader = pa.RecordBatchReader.from_batches(target_schema, source).cast(target_schema)
    return _SnapshotBatchStream(reader, source)


def _manifest_files(name: str, io: FileIO, snapshot: Snapshot) -> Iterator[ManifestFile]:
    """Read one manifest-list entry at a time and map malformed content to integrity failure."""
    try:
        yield from read_manifest_list(io.new_input(snapshot.manifest_list))
    except ValueError as exc:
        raise CatalogIntegrityError(
            f"snapshot of {name} has an invalid manifest-list content value"
        ) from exc


def _live_entries(name: str, io: FileIO, manifest: ManifestFile) -> Iterator[ManifestEntry]:
    """``ManifestFile.fetch_manifest_entry(io, discard_deleted=True)``, one entry at a time."""
    try:
        with AvroFile[ManifestEntry](
            io.new_input(manifest.manifest_path),
            MANIFEST_ENTRY_SCHEMAS[DEFAULT_READ_VERSION],
            read_types={-1: ManifestEntry, 2: DataFile},
            read_enums={0: ManifestEntryStatus, 101: FileFormat, 134: DataFileContent},
        ) as reader:
            for entry in reader:
                if entry.status != ManifestEntryStatus.DELETED:
                    yield _inherit_from_manifest(entry, manifest)
    except ValueError as exc:
        raise CatalogIntegrityError(
            f"snapshot of {name} has an invalid manifest entry content value"
        ) from exc


def _check_manifest(name: str, manifest: ManifestFile) -> None:
    if manifest.content != ManifestContent.DATA:
        raise CatalogIntegrityError(
            f"snapshot of {name} has a {manifest.content!r} manifest; "
            "the bounded scan does not support delete manifests"
        )


def _check_entry(name: str, data_file: DataFile) -> None:
    if data_file.content != DataFileContent.DATA:
        raise CatalogIntegrityError(
            f"snapshot of {name} has a live {data_file.content!r} file; "
            "the bounded scan does not support delete files"
        )
    if data_file.file_format == FileFormat.AVRO:
        raise CatalogIntegrityError(
            f"snapshot of {name} has a live Avro data file; "
            "the locked PyIceberg Arrow reader does not support Avro"
        )
    if data_file.file_format not in (FileFormat.PARQUET, FileFormat.ORC):
        raise CatalogIntegrityError(
            f"snapshot of {name} has a live data file with unsupported format "
            f"{data_file.file_format!r}"
        )


def _entry_matches(plan: _ScanPlan, data_file: DataFile, spec_id: int) -> bool:
    """``_open_manifest``'s entry predicate: partition evaluator, then metrics evaluator."""
    return plan.partition_evaluators[spec_id](data_file) and plan.metrics_evaluator(data_file)


def _preflight_snapshot(name: str, io: FileIO, snapshot: Snapshot, plan: _ScanPlan) -> None:
    """Check every manifest and live entry, and run all planning evaluators, before any row.

    Content checks ignore the row filter: a delete manifest or live delete file anywhere in the
    snapshot rejects the scan. The evaluators run on the same manifests and entries, in the same
    order and with the same short-circuiting as ``DataScan.plan_files`` (including the residual it
    computes per matched data file), so a planning failure also surfaces before any row.
    """
    for manifest in _manifest_files(name, io, snapshot):
        _check_manifest(name, manifest)
        selected = plan.manifest_evaluators[manifest.partition_spec_id](manifest)
        for entry in _live_entries(name, io, manifest):
            data_file = entry.data_file
            _check_entry(name, data_file)
            if selected and _entry_matches(plan, data_file, manifest.partition_spec_id):
                plan.residual_evaluators[data_file.spec_id](data_file).residual_for(
                    data_file.partition
                )


def _snapshot_batches(
    name: str, snapshot: Snapshot | None, plan: _ScanPlan, read: _FileRead
) -> Generator[pa.RecordBatch]:
    """Second pass: the matched data files of ``snapshot``, read one at a time, in plan order.

    Manifest and data files are immutable, so this sees what the preflight checked; the content
    checks are repeated so that a changed file still fails closed instead of being skipped.
    """
    if snapshot is None:
        return
    for manifest in _manifest_files(name, read.io, snapshot):
        _check_manifest(name, manifest)
        if not plan.manifest_evaluators[manifest.partition_spec_id](manifest):
            continue
        for entry in _live_entries(name, read.io, manifest):
            data_file = entry.data_file
            _check_entry(name, data_file)
            if _entry_matches(plan, data_file, manifest.partition_spec_id):
                yield from _data_file_batches(read, data_file)


def _data_file_batches(read: _FileRead, data_file: DataFile) -> Iterator[pa.RecordBatch]:
    """``_task_to_record_batches`` for one Parquet data file without deletes, with bounded I/O.

    Same file schema resolution, projection, partition-value filling, filter translation and
    requested-schema conversion as the locked version; only the Parquet read options differ (no
    pre-buffering, a bounded buffered stream, explicit batch size and readahead). The file is
    closed when its batches are exhausted, a read fails, or the generator is closed.
    """
    if data_file.file_format == FileFormat.PARQUET:
        arrow_format: ds.FileFormat = ds.ParquetFileFormat(
            pre_buffer=False, use_buffered_stream=True, buffer_size=_SCAN_READ_BUFFER_BYTES
        )
    elif data_file.file_format == FileFormat.ORC:
        # Arrow's ORC reader has no pre_buffer / buffer_size options; retain its native scanner
        # and the explicit batch/readahead limits configured below.
        arrow_format = ds.OrcFileFormat()
    else:
        raise CatalogIntegrityError(
            f"snapshot has unsupported data format {data_file.file_format!r}"
        )
    with read.io.new_input(data_file.file_path).open() as fin:
        fragment = arrow_format.make_fragment(fin)
        physical_schema = fragment.physical_schema
        file_schema = pyarrow_to_schema(
            physical_schema,
            read.name_mapping,
            downcast_ns_timestamp_to_us=read.downcast_ns_timestamp_to_us,
            format_version=read.format_version,
        )
        # Column projection rules: https://iceberg.apache.org/spec/#column-projection
        projected_missing_fields: dict[int, Any] = _get_column_projection_values(
            data_file,
            read.projected_schema,
            read.table_schema,
            read.specs.get(data_file.spec_id),
            file_schema.field_ids,
        )
        pyarrow_filter = None
        if read.bound_row_filter is not AlwaysTrue():
            translated_row_filter = translate_column_names(
                read.bound_row_filter,
                file_schema,
                case_sensitive=read.case_sensitive,
                projected_field_values=projected_missing_fields,
            )
            bound_file_filter = bind(
                file_schema, translated_row_filter, case_sensitive=read.case_sensitive
            )
            pyarrow_filter = expression_to_pyarrow(bound_file_filter, file_schema)
        file_project_schema = prune_columns(
            file_schema, read.projected_field_ids, select_full_types=False
        )
        scanner = ds.Scanner.from_fragment(
            fragment=fragment,
            schema=physical_schema,
            filter=pyarrow_filter,
            columns=[column.name for column in file_project_schema.columns],
            batch_size=_SCAN_BATCH_ROWS,
            batch_readahead=_SCAN_BATCH_READAHEAD,
            fragment_readahead=_SCAN_FRAGMENT_READAHEAD,
        )
        batches = scanner.to_batches()
        try:
            for batch in batches:
                if batch.num_rows == 0:
                    continue
                yield _to_requested_schema(
                    read.projected_schema,
                    file_project_schema,
                    batch,
                    downcast_ns_timestamp_to_us=read.downcast_ns_timestamp_to_us,
                    projected_missing_fields=projected_missing_fields,
                    allow_timestamp_tz_mismatch=True,
                )
        finally:
            # A caller can close the outer snapshot stream between batches. Release a lazy
            # scanner iterator first, while the fragment's input stream is still open.
            close = getattr(batches, "close", None)
            if callable(close):
                close()


def _revalidated[M: (TableDefinition, CommitRequest)](model: type[M], value: object) -> M:
    if type(value) is not model:
        raise TypeError(f"expected {model.__name__}, got {type(value).__name__}")
    assert isinstance(value, model)
    return model.model_validate_json(value.model_dump_json())


def _listed_position(snapshots: Sequence[Snapshot], snapshot_id: object) -> int | None:
    """Position of the first listed snapshot with this id, resolved as ``get_snapshot`` does.

    A plain scan (``TableMetadata.snapshot_by_id`` semantics) with no index, so a walk over
    the loaded metadata needs no memory that grows with the history.
    """
    if not (isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id)):
        return None
    wanted = int(snapshot_id)
    return next(
        (index for index, listed in enumerate(snapshots) if listed.snapshot_id == wanted), None
    )


class PyIcebergCatalogAdapter:
    """``CatalogAdapter[pyarrow.Table]`` over a PyIceberg ``Catalog``."""

    def __init__(self, catalog: Catalog, registry: TableDefinitionRegistry) -> None:
        if not isinstance(registry, TableDefinitionRegistry):
            raise TypeError("registry must be a TableDefinitionRegistry")
        self._catalog = catalog
        self._registry = registry

    def __repr__(self) -> str:
        return f"{type(self).__name__}(catalog={self._catalog.name!r})"

    def close(self) -> None:
        """Release the catalog's database connections (idempotent)."""
        self._catalog.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ CatalogAdapter

    def load_table(self, table: str) -> TableInfo | None:
        name = validate_table_name(table)
        with _backend("load_table"):
            iceberg = self._load(name)
            if iceberg is None:
                return None
            binding = self._verified(name, iceberg)[1]
            return self._table_info(name, iceberg, binding)

    def create_table(self, definition: TableDefinition) -> TableInfo:
        name = validate_table_name(definition.table)
        try:
            requested = _revalidated(TableDefinition, definition)
        except (TypeError, ValidationError) as exc:
            raise UnknownTableDefinition(f"invalid table definition for {name}") from exc
        registered = self._registry.resolve(requested)
        with _backend("create_table"):
            existing = self._load(name)
            if existing is None and registered.is_evolution_target:
                raise DefinitionEvolutionError(
                    f"{requested.definition_id}@{requested.version} is a partition-spec evolution "
                    f"target; create {name} with its initial definition and evolve it"
                )
            if existing is None:
                self._ensure_namespace(name)
                try:
                    self._catalog.create_table(
                        _identifier(name),
                        schema=registered.schema,
                        partition_spec=registered.partition_spec,
                        properties={
                            **registered.table_properties(),
                            "format-version": str(ICEBERG_FORMAT_VERSION),
                        },
                    )
                except TableAlreadyExistsError:
                    pass  # created concurrently: compare the winner's binding below
                existing = self._load(name)
                if existing is None:
                    raise CatalogIntegrityError(f"table {name} vanished right after creation")
            stored = self._stored_binding(name, existing)
            if stored != requested:
                raise TableDefinitionConflict(
                    f"table {name} exists with {stored.definition_id}@{stored.version}"
                )
            binding = self._verified(name, existing)[1]
            return self._table_info(name, existing, binding)

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        name = validate_table_name(table)
        with _backend("get_snapshot"):
            iceberg = self._require(name)
            self._verified(name, iceberg)
            snapshot = (
                iceberg.metadata.snapshot_by_id(int(snapshot_id))
                if isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id)
                else None
            )
            if snapshot is None:
                raise SnapshotNotFound(f"table {name} has no snapshot {snapshot_id!r}")
            return self._snapshot_info(name, snapshot)

    def history(self, table: str, snapshot_id: str) -> Iterator[SnapshotInfo]:
        """``snapshot_id`` and its ancestors, newest first, from **one** load of the metadata.

        Walking with ``get_snapshot`` reloads (and re-verifies) the table metadata at every
        step: ``H`` loads for a history of ``H`` snapshots. This loads and verifies it once and
        follows the parent ids inside that one metadata version. Infrastructure only (not part
        of the core Protocol).

        Same result and failures as that walk, step by step: an id is resolved like
        ``get_snapshot`` (malformed or unknown → ``SnapshotNotFound``; with repeated ids the
        first listed wins, as in ``TableMetadata.snapshot_by_id``), then converted by
        ``_snapshot_info`` (a snapshot naming itself as parent stays ``CatalogIntegrityError``),
        then yielded. A dangling parent is ``SnapshotNotFound`` for that parent id. Unlike that
        walk, a multi-snapshot parent cycle cannot loop forever: a second pointer runs ahead at
        double speed (Floyd), and once it meets the walk the cycle is measured, so every
        snapshot on the ancestry is yielded once and the step that would repeat one is
        ``CatalogIntegrityError`` instead.

        Memory: no index or visited set; each id is found by a linear scan of the loaded
        metadata (``O(H)`` time per lookup, three lookups per step). The loaded metadata itself
        (PyIceberg keeps **every** snapshot of the table in ``metadata.snapshots``) stays
        referenced until the iterator is exhausted or closed.
        """
        name = validate_table_name(table)
        with _backend("history"):
            iceberg = self._require(name)
            self._verified(name, iceberg)
        snapshots = iceberg.metadata.snapshots

        def parent_of(position: int | None) -> int | None:
            # The walk's next position, read from the raw snapshot so it never raises.
            if position is None:
                return None
            parent = snapshots[position].parent_snapshot_id
            return None if parent is None else _listed_position(snapshots, str(parent))

        start = _listed_position(snapshots, snapshot_id)
        ahead = start  # at position 2 * walked while no cycle is known
        limit: int | None = None  # distinct snapshots on a cyclic ancestry, once measured
        wanted: str | None = snapshot_id
        walked = 0
        while wanted is not None:
            position = _listed_position(snapshots, wanted)
            if position is None:
                raise SnapshotNotFound(f"table {name} has no snapshot {wanted!r}")
            if limit is None and walked and position == ahead:
                # Met at a multiple of the cycle length: find where it starts, then its length.
                behind: int | None = start
                lead: int | None = position
                entry = 0
                while behind != lead:
                    behind, lead, entry = parent_of(behind), parent_of(lead), entry + 1
                lead, length = parent_of(behind), 1
                while lead != behind:
                    lead, length = parent_of(lead), length + 1
                limit = entry + length
            if walked == limit:
                raise CatalogIntegrityError(f"table {name} has a cycle in snapshot history")
            info = self._snapshot_info(name, snapshots[position])
            yield info
            walked += 1
            wanted = info.parent_snapshot_id
            if limit is None:
                ahead = parent_of(parent_of(ahead))

    def pin_bounded_metadata(
        self,
        table: str,
        *,
        storage: StorageAdapter,
        limits: BoundedMetadataLimits,
    ) -> BoundedIcebergMetadata:
        """Pin and verify one bounded SqlCatalog metadata version (infrastructure only).

        Unlike ``load_table`` this does not retain the full ``TableMetadata.snapshots`` list.
        The registered table binding/schema/spec/properties are still verified by this adapter
        against the same metadata pointer returned by the private bounded reader.
        """
        name = validate_table_name(table)
        return self._pin_bounded_metadata(name, storage=storage, limits=limits)

    def pin_bounded_metadata_at(
        self,
        table: str,
        snapshot_id: str | None,
        *,
        storage: StorageAdapter,
        limits: BoundedMetadataLimits,
    ) -> BoundedIcebergMetadata:
        """Pin bounded metadata and restrict reads to one exact historical snapshot.

        ``None`` is an explicit empty view, distinct from ``pin_bounded_metadata``'s
        current-head selection.  The requested ID is resolved against the pinned
        metadata index before this method returns.
        """
        from infrastructure.catalog.bounded_metadata import pin_bounded_sql_table

        name = validate_table_name(table)
        if not isinstance(self._catalog, SqlCatalog):
            raise CatalogIntegrityError(
                "bounded metadata pinning requires the configured PyIceberg SqlCatalog"
            )
        with _backend("pin_bounded_metadata_at"):
            bounded = pin_bounded_sql_table(
                self._catalog,
                name,
                storage=storage,
                limits=limits,
                selected_snapshot_id=snapshot_id,
                select_current_head=False,
            )
            self._verified(name, bounded.verification_table())
            return bounded

    def _pin_bounded_metadata(
        self,
        name: str,
        *,
        storage: StorageAdapter,
        limits: BoundedMetadataLimits,
    ) -> BoundedIcebergMetadata:
        from infrastructure.catalog.bounded_metadata import pin_bounded_sql_table

        if not isinstance(self._catalog, SqlCatalog):
            raise CatalogIntegrityError(
                "bounded metadata pinning requires the configured PyIceberg SqlCatalog"
            )
        with _backend("pin_bounded_metadata"):
            bounded = pin_bounded_sql_table(
                self._catalog,
                name,
                storage=storage,
                limits=limits,
            )
            self._verified(name, bounded.verification_table())
            return bounded

    def scan_pinned_batches(
        self,
        bounded: BoundedIcebergMetadata,
        *,
        snapshot_id: str,
        columns: Sequence[str],
        row_filter: BooleanExpression = _ALWAYS_TRUE,
    ) -> _SnapshotBatchStream:
        """Scan a snapshot from one previously pinned bounded metadata view.

        This private infrastructure path avoids calling ``load_table`` again. It checks the
        selected snapshot in the external index, constructs and re-verifies a compact ordinary
        PyIceberg metadata object, then gives the existing fixed-snapshot reader only that one
        selected Snapshot plus schema/spec/properties. The public scan APIs remain unchanged.
        """
        from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata

        if not isinstance(bounded, BoundedIcebergMetadata):
            raise TypeError("bounded must come from this adapter's pin_bounded_metadata")
        if bounded.selected_snapshot_id != snapshot_id:
            raise CatalogIntegrityError("scan snapshot differs from the bounded metadata selection")
        return self.scan_bounded_batches(
            bounded, snapshot_id=snapshot_id, columns=columns, row_filter=row_filter
        )

    def scan_bounded_batches(
        self,
        bounded: BoundedIcebergMetadata,
        *,
        snapshot_id: str,
        columns: Sequence[str],
        row_filter: BooleanExpression = _ALWAYS_TRUE,
    ) -> _SnapshotBatchStream:
        """Scan any exact snapshot indexed by a previously pinned metadata pointer.

        The caller may select a historical ID only from this immutable pointer. The
        normalizer uses this for explicit history reads while its implicit reads remain bound
        to ``bounded.selected_snapshot_id`` through :meth:`scan_pinned_batches`.
        """
        from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata

        if not isinstance(bounded, BoundedIcebergMetadata):
            raise TypeError("bounded must come from this adapter's bounded metadata pin")
        if bounded.catalog is not self._catalog:
            raise CatalogIntegrityError("bounded metadata belongs to a different catalog instance")
        name = validate_table_name(bounded.name)
        if isinstance(columns, str) or not columns:
            raise BatchRejected("scan_bounded_batches needs a non-empty sequence of column names")
        compact = bounded.compact_table(snapshot_id)
        with _backend("scan_bounded_batches"):
            self._verified(name, compact)
            # ``TableScan.snapshot`` resolves by ID from the compact metadata. The selected id is
            # passed explicitly so a caller cannot silently fall back to the table's current head.
            snapshot = compact.metadata.snapshot_by_id(int(snapshot_id))
            if snapshot is None:
                raise SnapshotNotFound(f"table {name} has no snapshot {snapshot_id!r}")
            return _open_snapshot_batches(
                name,
                compact,
                columns=tuple(columns),
                row_filter=row_filter,
                snapshot_id=snapshot.snapshot_id,
            )

    def table_info_from_bounded(self, bounded: BoundedIcebergMetadata) -> TableInfo:
        """Describe the selected snapshot from a bounded immutable metadata pointer."""
        from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata

        if not isinstance(bounded, BoundedIcebergMetadata):
            raise TypeError("bounded must come from this adapter's bounded metadata pin")
        if bounded.catalog is not self._catalog:
            raise CatalogIntegrityError("bounded metadata belongs to a different catalog instance")
        name = validate_table_name(bounded.name)
        iceberg = bounded.verification_table()
        _, binding = self._verified(name, iceberg)
        selected = bounded.selected_snapshot_id
        snapshot = (
            None
            if selected is None
            else self._snapshot_info(name, bounded.require_snapshot(selected))
        )
        return TableInfo(definition=binding, current_snapshot=snapshot)

    def scan_bounded_columns(
        self,
        bounded: BoundedIcebergMetadata,
        *,
        snapshot_id: str | None,
        columns: Sequence[str],
        row_filter: BooleanExpression = _ALWAYS_TRUE,
        limit: int | None = None,
    ) -> pa.Table:
        """Materialize columns from one exact bounded metadata pointer.

        ``snapshot_id=None`` is the explicit empty selection used by an unbound pinned view.
        Its Arrow schema is projected from that pointer's validated table schema without loading
        the catalog's current metadata pointer.
        """
        from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata

        if not isinstance(bounded, BoundedIcebergMetadata):
            raise TypeError("bounded must come from this adapter's bounded metadata pin")
        if bounded.catalog is not self._catalog:
            raise CatalogIntegrityError("bounded metadata belongs to a different catalog instance")
        name = validate_table_name(bounded.name)
        if isinstance(columns, str) or not columns:
            raise BatchRejected("scan_columns needs a non-empty sequence of column names")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise BatchRejected("scan limit must be a positive int or None")
        if snapshot_id is None:
            if bounded.selected_snapshot_id is not None:
                raise CatalogIntegrityError(
                    "empty scan differs from the bounded metadata selection"
                )
            with _backend("scan_bounded_columns"):
                table = bounded.verification_table()
                self._verified(name, table)
                empty_scan = table.scan(row_filter=AlwaysFalse(), selected_fields=tuple(columns))
                return pa.Table.from_batches([], schema=schema_to_pyarrow(empty_scan.projection()))

        reader = self.scan_bounded_batches(
            bounded,
            snapshot_id=snapshot_id,
            columns=columns,
            row_filter=row_filter,
        )
        schema = reader.schema
        batches: list[pa.RecordBatch] = []
        remaining = limit
        try:
            for record_batch in reader:
                if remaining is None:
                    batches.append(record_batch)
                    continue
                take = min(record_batch.num_rows, remaining)
                if take:
                    batches.append(record_batch.slice(0, take))
                    remaining -= take
                if remaining == 0:
                    break
        except BaseException:
            try:
                reader.close()
            except BaseException:
                pass
            raise
        else:
            reader.close()
        return pa.Table.from_batches(batches, schema=schema)

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult:
        name = validate_table_name(request.table)
        request = _revalidated(CommitRequest, request)
        with _backend("commit_batch"):
            iceberg = self._require(name)
            registered = self._verified(name, iceberg)[0]
            # Step 1: verify the actual batch before any replay fast path.
            self._check_batch(registered, request, batch)
            # Step 2: idempotency from the persisted snapshot history.
            replay = self._replay(name, iceberg, request)
            if replay is not None:
                return replay
            # Step 3: optimistic concurrency against the current snapshot.
            current = iceberg.metadata.current_snapshot_id
            current_id = None if current is None else str(current)
            if current_id != request.expected_parent_snapshot_id:
                raise CommitConflict(
                    f"table {name} is at snapshot {current_id}, "
                    f"not {request.expected_parent_snapshot_id}"
                )
            properties = self._summary_properties(registered, request)
            try:
                iceberg.append(batch, snapshot_properties=properties)
            except CommitFailedException:
                return self._after_lost_race(name, request)
            committed = iceberg.current_snapshot()
            info = None if committed is None else self._snapshot_info(name, committed)
            if (
                info is None
                or info.batch_id != request.batch_id
                or info.parent_snapshot_id != request.expected_parent_snapshot_id
            ):
                raise CatalogIntegrityError(f"committed metadata of {name} does not match request")
            return CommitResult(request=request, snapshot=info, outcome=CommitOutcome.COMMITTED)

    def commit_unit(
        self,
        request: CommitRequest,
        batches: Callable[[], Iterable[pa.Table]],
        *,
        scratch_directory: Path,
        window_rows: int,
    ) -> CommitResult:
        """Commit one logical unit as **one** snapshot of many staged data files (ADR-0108).

        Infrastructure-only; not part of the frozen ``CatalogAdapter`` protocol. ``request`` is the
        unit's ``CommitRequest``: its ``batch_fingerprint`` is the registered rule's fingerprint of
        the logical concatenation of every microbatch ``batches()`` yields, in order, and its
        ``row_count`` their total. ``batches`` is a factory: it is called twice, and both passes
        must yield the same content (a non-deterministic source is refused before committing).

        The steps mirror ``commit_batch``: (1) the actual unit is verified (schema, row count, a
        bounded streaming fingerprint on ``scratch_directory``) before any replay fast path; (2) the
        batch id is replayed from the persisted history (same content: ``ALREADY_COMMITTED``;
        other content: ``BatchConflict``); (3) the expected parent is checked; (4) the unit is
        staged as data files in bounded memory (``unit_commit.StagedUnitWriter``) while its
        fingerprint is recomputed; (5) every staged file is appended in one fast append whose
        summary also records ``SUMMARY_COMMIT_LAYOUT`` and ``SUMMARY_WINDOW_ROWS``. A lost race is
        resolved as ``commit_batch`` resolves it. Staged files of a failed or lost commit are
        unreferenced orphans.
        """
        from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT_RULE_ID
        from infrastructure.catalog.fingerprint_unit import UnitFingerprint
        from infrastructure.catalog.unit_commit import StagedUnitWriter

        name = validate_table_name(request.table)
        request = _revalidated(CommitRequest, request)
        if isinstance(window_rows, bool) or not isinstance(window_rows, int) or window_rows < 1:
            raise BatchRejected("window_rows must be a positive int")
        if not callable(batches):
            raise BatchRejected("batches must be a factory returning the unit's microbatches")
        with _backend("commit_unit"):
            iceberg = self._require(name)
            registered = self._verified(name, iceberg)[0]
            if registered.fingerprint_rule.rule_id != PYARROW_BATCH_FINGERPRINT_RULE_ID:
                raise BatchRejected(
                    f"{registered.definition_id} is not bound to "
                    f"{PYARROW_BATCH_FINGERPRINT_RULE_ID}; a unit commit streams that rule's "
                    "fingerprint"
                )
            # Step 1: verify the actual unit before any replay fast path.
            with UnitFingerprint(registered.arrow_schema, scratch_directory) as unit:
                for batch in batches():
                    self._check_unit_batch(registered, batch)
                    unit.add(batch)
                self._check_unit_claim(request, unit.rows, unit.hexdigest())
            # Step 2: idempotency from the persisted snapshot history.
            replay = self._replay(name, iceberg, request)
            if replay is not None:
                return replay
            # Step 3: optimistic concurrency against the current snapshot.
            current = iceberg.metadata.current_snapshot_id
            current_id = None if current is None else str(current)
            if current_id != request.expected_parent_snapshot_id:
                raise CommitConflict(
                    f"table {name} is at snapshot {current_id}, "
                    f"not {request.expected_parent_snapshot_id}"
                )
            # Step 4: stage the unit's data files, re-verifying the content as it is written.
            tag = hashlib.sha256(
                f"{name}\0{request.batch_id}\0{request.batch_fingerprint}".encode()
            ).hexdigest()[:24]
            writer = StagedUnitWriter(iceberg, tag=tag)
            try:
                with UnitFingerprint(registered.arrow_schema, scratch_directory) as again:
                    for batch in batches():
                        self._check_unit_batch(registered, batch)
                        again.add(batch)
                        writer.add(batch)
                    if again.rows != request.row_count or again.hexdigest() != (
                        request.batch_fingerprint
                    ):
                        raise BatchRejected(
                            "the unit's microbatches changed between verification and staging"
                        )
                data_files = writer.finish()
            except BaseException:
                writer.abort()
                raise
            # Step 5: one snapshot appending every staged file.
            properties = {
                **self._summary_properties(registered, request),
                SUMMARY_COMMIT_LAYOUT: UNIT_COMMIT_LAYOUT,
                SUMMARY_WINDOW_ROWS: str(window_rows),
            }
            try:
                with iceberg.transaction() as transaction:
                    with transaction._append_snapshot_producer(properties) as producer:
                        for data_file in data_files:
                            producer.append_data_file(data_file)
            except CommitFailedException:
                return self._after_lost_race(name, request)
            committed = iceberg.current_snapshot()
            info = None if committed is None else self._snapshot_info(name, committed)
            if (
                info is None
                or info.batch_id != request.batch_id
                or info.parent_snapshot_id != request.expected_parent_snapshot_id
                or info.added_rows != request.row_count
            ):
                raise CatalogIntegrityError(f"committed metadata of {name} does not match request")
            return CommitResult(request=request, snapshot=info, outcome=CommitOutcome.COMMITTED)

    @staticmethod
    def _check_unit_batch(registered: RegisteredTableDefinition, batch: object) -> None:
        if not isinstance(batch, pa.Table):
            raise BatchRejected(f"a microbatch must be a pyarrow.Table, got {type(batch).__name__}")
        if not batch.schema.equals(registered.arrow_schema, check_metadata=False):
            raise BatchRejected(f"a microbatch schema does not match {registered.definition_id}")

    @staticmethod
    def _check_unit_claim(request: CommitRequest, rows: int, fingerprint: str) -> None:
        if rows != request.row_count:
            raise BatchRejected(
                f"the unit has {rows} rows but the request declares {request.row_count}"
            )
        if fingerprint != request.batch_fingerprint:
            raise BatchRejected("unit fingerprint does not match the actual unit content")

    # ------------------------------------------------------------------ D2 bounded read

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = _ALWAYS_TRUE,
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> pa.Table:
        """Read selected columns of ``table`` at its current snapshot (infrastructure only).

        ``snapshot_id`` (Phase 1 F1) reads a named snapshot of the table instead — the time
        travel a point-in-time build needs to re-read exactly what its manifest bound. An id the
        table does not have is ``SnapshotNotFound``; nothing falls back to the current head.

        This is **not** part of the core ``CatalogAdapter`` Protocol: adding a required method
        there would break every existing implementation, and the general point-in-time read
        interface is defined by its first consumer in batch F (``core/contracts/catalog.py``).
        D2 needs exactly this much: the committed ``arrival_seq`` of the allocation anchor, the
        archive revision row a restart has to recover from, and the revisions of one observation
        key. ``row_filter`` is a PyIceberg expression object, never a string built from data, and
        the caller keeps the result bounded by projecting columns and filtering on the archive
        table (which holds one row per archive file).

        The table's definition binding is verified first, so a drifted table fails closed instead
        of returning rows of unknown provenance.
        """
        name = validate_table_name(table)
        if isinstance(columns, str) or not columns:
            raise BatchRejected("scan_columns needs a non-empty sequence of column names")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise BatchRejected("scan limit must be a positive int or None")
        with _backend("scan_columns"):
            iceberg = self._require(name)
            self._verified(name, iceberg)
            if snapshot_id is None:
                scan = iceberg.scan(
                    row_filter=row_filter, selected_fields=tuple(columns), limit=limit
                )
                return scan.to_arrow()
            pinned = (
                iceberg.metadata.snapshot_by_id(int(snapshot_id))
                if isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id)
                else None
            )
            if pinned is None:
                raise SnapshotNotFound(f"table {name} has no snapshot {snapshot_id!r}")
            scan = iceberg.scan(
                row_filter=row_filter,
                selected_fields=tuple(columns),
                limit=limit,
                snapshot_id=pinned.snapshot_id,
            )
            return scan.to_arrow()

    def scan_column_batches(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = _ALWAYS_TRUE,
        snapshot_id: str | None = None,
    ) -> _SnapshotBatchStream:
        """Stream selected columns of one fixed snapshot as bounded Arrow record batches.

        This infrastructure-only read verifies the persisted table definition before scanning.
        When ``snapshot_id`` is supplied, the scan is pinned to that exact snapshot and fails
        with ``SnapshotNotFound`` if the table has no matching snapshot; otherwise it is pinned
        to the current snapshot of the metadata loaded by this call.

        ADR-0075: PyIceberg's high-level planner is not used. Before this returns, every manifest
        of the snapshot and every live entry are checked (independently of ``row_filter``):
        a table that is not format version 2, a delete manifest, a live delete file or an Avro
        data file (unsupported by the locked PyIceberg Arrow reader) is ``CatalogIntegrityError``
        and no row is produced. Parquet and ORC batches are read one data file at a time, with the
        same projection, filter, partition-value and timestamp semantics as ``scan_columns``.
        Call ``close`` on the returned iterator when stopping before exhaustion so its reader and
        open data file are released promptly.
        """
        name = validate_table_name(table)
        if isinstance(columns, str) or not columns:
            raise BatchRejected("scan_column_batches needs a non-empty sequence of column names")
        with _backend("scan_column_batches"):
            iceberg = self._require(name)
            self._verified(name, iceberg)
            pinned_id: int | None = None
            if snapshot_id is not None:
                pinned = (
                    iceberg.metadata.snapshot_by_id(int(snapshot_id))
                    if isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id)
                    else None
                )
                if pinned is None:
                    raise SnapshotNotFound(f"table {name} has no snapshot {snapshot_id!r}")
                pinned_id = pinned.snapshot_id
            return _open_snapshot_batches(
                name,
                iceberg,
                columns=tuple(columns),
                row_filter=row_filter,
                snapshot_id=pinned_id,
            )

    def max_int64(
        self,
        table: str,
        column: str,
        *,
        row_filter: BooleanExpression = _ALWAYS_TRUE,
        check: Callable[[int], None] | None = None,
    ) -> int | None:
        """The largest value of the ``int64`` ``column``, reduced over a streaming batch reader.

        Unlike ``scan_columns`` this never builds one ``pyarrow.Table`` over the scanned history;
        the reduction itself retains only the running maximum (one Python int) between batches.
        It uses the ADR-0075 fixed-snapshot scan path, so it does not invoke PyIceberg's
        high-level manifest / task planner. The I/O cost still grows with the number of matching
        data files, because every one is opened and its projected column read.

        Every value is validated while it streams (fail closed, no unbounded bookkeeping): the
        column must be a non-nullable ``int64`` projection with no nulls, and ``check`` — the
        caller's domain rule — must accept each value. ``None`` means the scan matched no row.
        """
        name = validate_table_name(table)
        if not isinstance(column, str) or not column:
            raise BatchRejected("max_int64 needs a column name")
        if check is not None and not callable(check):
            raise BatchRejected("max_int64 check must be callable")
        with _backend("max_int64"):
            reader = self.scan_column_batches(name, columns=(column,), row_filter=row_filter)
            try:
                return _reduce_max_int64(reader, name=name, column=column, check=check)
            finally:
                reader.close()

    # ------------------------------------------------------------------ C3 evolution

    def evolve_partition_spec(
        self, source: TableDefinition, target: TableDefinition
    ) -> PartitionEvolutionResult:
        """Evolve ``source.table`` from ``source`` to the partition-spec-only target ``target``.

        Both bindings must be registered and ``target`` must evolve exactly from ``source``.
        The persisted table must be verified at ``source`` (or already verified at ``target``:
        idempotent replay). The new spec and the ``hlens.definition.*`` binding properties are
        staged in one PyIceberg transaction, checked against ``target`` before the commit and
        committed as one metadata update (commit retries are pinned to zero). Any failure before
        or during the commit leaves the table at ``source``; data files are never rewritten.
        """
        try:
            requested_source = _revalidated(TableDefinition, source)
            requested_target = _revalidated(TableDefinition, target)
        except (TypeError, ValidationError) as exc:
            raise DefinitionEvolutionError("invalid evolution bindings") from exc
        name = validate_table_name(requested_source.table)
        if requested_target.table != name:
            raise DefinitionEvolutionError("source and target must bind the same table")
        source_def = self._registry.resolve(requested_source)
        target_def = self._registry.resolve(requested_target)
        try:
            require_partition_only_change(source_def, target_def)
        except ValueError as exc:
            raise DefinitionEvolutionError(str(exc)) from None
        with _backend("evolve_partition_spec"):
            iceberg = self._require(name)
            stored = self._stored_binding(name, iceberg)
            if stored == requested_target:
                self._verified(name, iceberg)
                return self._evolution_result(
                    name, source_def, target_def, EvolutionOutcome.ALREADY_EVOLVED
                )
            if stored != requested_source:
                raise TableDefinitionConflict(
                    f"table {name} is at {stored.definition_id}@{stored.version}, "
                    f"not the evolution source {requested_source.version}"
                )
            self._verified(name, iceberg)
            transaction = iceberg.transaction()
            try:
                with transaction.update_spec() as update:
                    self._stage_spec_changes(iceberg, update, source_def, target_def)
            except ValueError as exc:
                raise DefinitionEvolutionError(f"PyIceberg rejected the evolution: {exc}") from None
            transaction.set_properties(
                {
                    PROPERTY_DEFINITION_VERSION: target_def.version,
                    PROPERTY_DEFINITION_HASH: target_def.definition_hash,
                }
            )
            self._check_staged(name, transaction.table_metadata, target_def)
            try:
                transaction.commit_transaction()
            except CommitFailedException:
                raise CommitConflict(
                    f"table {name} changed concurrently; partition-spec evolution not applied"
                ) from None
            evolved = self._require(name)
            if self._verified(name, evolved)[1] != requested_target:
                raise CatalogIntegrityError(f"table {name} is not at the evolution target")
            return self._evolution_result(name, source_def, target_def, EvolutionOutcome.EVOLVED)

    @staticmethod
    def _stage_spec_changes(
        iceberg: IcebergTable,
        update: UpdateSpec,
        source: RegisteredTableDefinition,
        target: RegisteredTableDefinition,
    ) -> None:
        """Remove source-only fields and add target-only fields (matched by partition field ID)."""
        old = {field.field_id: field for field in source.partition_spec.fields}
        new = {field.field_id: field for field in target.partition_spec.fields}
        for field_id in sorted(old.keys() & new.keys()):
            if old[field_id] != new[field_id]:
                raise DefinitionEvolutionError(
                    f"partition field {field_id} changes in place; renames are not evolutions"
                )
        schema = iceberg.schema()
        for field_id in sorted(old.keys() - new.keys()):
            update.remove_field(old[field_id].name)
        for field_id in sorted(new.keys() - old.keys()):
            added = new[field_id]
            column = schema.find_column_name(added.source_id)
            if column is None:
                raise DefinitionEvolutionError(f"partition field {added.name!r} has no source")
            update.add_field(column, added.transform, added.name)

    @staticmethod
    def _check_staged(name: str, staged: TableMetadata, target: RegisteredTableDefinition) -> None:
        """Compare the staged (uncommitted) metadata with ``target``; nothing is committed yet."""
        spec = staged.spec()
        if (
            spec.fields != target.partition_spec.fields
            or spec.spec_id != target.partition_spec.spec_id
        ):
            raise DefinitionEvolutionError(
                f"Iceberg would assign {spec} to {name}, not the declared target spec "
                f"{target.partition_spec}"
            )
        if staged.schema().as_struct() != target.schema.as_struct():
            raise DefinitionEvolutionError(f"staged schema of {name} differs from the target")
        expected = target.table_properties()
        if any(staged.properties.get(key) != value for key, value in expected.items()):
            raise DefinitionEvolutionError(f"staged properties of {name} differ from the target")

    def _evolution_result(
        self,
        name: str,
        source: RegisteredTableDefinition,
        target: RegisteredTableDefinition,
        outcome: EvolutionOutcome,
    ) -> PartitionEvolutionResult:
        return PartitionEvolutionResult(
            table=name,
            source=source.binding,
            target=target.binding,
            outcome=outcome,
            spec_id=target.partition_spec.spec_id,
        )

    # ------------------------------------------------------------------ helpers

    def _load(self, name: str) -> IcebergTable | None:
        try:
            return self._catalog.load_table(_identifier(name))
        except NoSuchTableError:
            return None

    def _require(self, name: str) -> IcebergTable:
        iceberg = self._load(name)
        if iceberg is None:
            raise TableNotFound(f"table {name} does not exist")
        return iceberg

    def _ensure_namespace(self, name: str) -> None:
        namespace = _identifier(name)[0]
        try:
            self._catalog.create_namespace_if_not_exists(namespace)
        except Exception:
            # A concurrent creator may win the unique-key race; only its existence matters.
            if not self._catalog.namespace_exists(namespace):
                raise

    def _stored_binding(self, name: str, iceberg: IcebergTable) -> TableDefinition:
        properties = iceberg.properties
        try:
            return TableDefinition(
                table=name,
                definition_id=properties[PROPERTY_DEFINITION_ID],
                version=properties[PROPERTY_DEFINITION_VERSION],
                definition_hash=properties[PROPERTY_DEFINITION_HASH],
            )
        except (KeyError, ValidationError):
            raise CatalogIntegrityError(
                f"table {name} has no valid persisted definition binding"
            ) from None

    def _verified(
        self, name: str, iceberg: IcebergTable
    ) -> tuple[RegisteredTableDefinition, TableDefinition]:
        """Persisted binding → registry → stored layout; any disagreement fails closed."""
        binding = self._stored_binding(name, iceberg)
        registered = self._registry.resolve(binding)
        stored = iceberg.properties
        expected = registered.table_properties()
        mismatched = [key for key, value in expected.items() if stored.get(key) != value]
        if mismatched:
            raise CatalogIntegrityError(f"table {name} properties differ: {sorted(mismatched)}")
        if iceberg.metadata.format_version != ICEBERG_FORMAT_VERSION:
            raise CatalogIntegrityError(f"table {name} has an unexpected format version")
        if iceberg.schema().as_struct() != registered.schema.as_struct():
            raise CatalogIntegrityError(f"table {name} schema differs from its definition")
        spec = iceberg.spec()
        expected_spec = registered.partition_spec
        if spec.fields != expected_spec.fields or spec.spec_id != expected_spec.spec_id:
            raise CatalogIntegrityError(f"table {name} partition spec differs from its definition")
        history = iceberg.metadata.specs()
        for ancestor in self._registry.ancestors(registered):
            kept = history.get(ancestor.partition_spec.spec_id)
            if kept is None or kept.fields != ancestor.partition_spec.fields:
                raise CatalogIntegrityError(
                    f"table {name} lacks the partition spec of {ancestor.definition_id}@"
                    f"{ancestor.version} it evolved from"
                )
        return registered, binding

    def _table_info(self, name: str, iceberg: IcebergTable, binding: TableDefinition) -> TableInfo:
        current = iceberg.current_snapshot()
        return TableInfo(
            definition=binding,
            current_snapshot=None if current is None else self._snapshot_info(name, current),
        )

    def _snapshot_info(self, name: str, snapshot: Snapshot) -> SnapshotInfo:
        summary = snapshot.summary
        extra = {} if summary is None else summary.additional_properties
        batch_id = extra.get(SUMMARY_BATCH_ID)
        fingerprint = extra.get(SUMMARY_BATCH_FINGERPRINT)
        row_count = extra.get(SUMMARY_BATCH_ROW_COUNT)
        parent = snapshot.parent_snapshot_id
        try:
            added = int(extra.get(_ADDED_RECORDS, "0"))
            total = int(extra[_TOTAL_RECORDS])
            if batch_id is not None and (row_count is None or int(row_count) != added):
                raise ValueError("batch row count does not match added records")
            return SnapshotInfo(
                table=name,
                snapshot_id=str(snapshot.snapshot_id),
                parent_snapshot_id=None if parent is None else str(parent),
                committed_at=_EPOCH + timedelta(milliseconds=snapshot.timestamp_ms),
                batch_id=batch_id,
                batch_fingerprint=fingerprint,
                added_rows=added,
                total_rows=total,
            )
        except (KeyError, ValueError):
            raise CatalogIntegrityError(
                f"snapshot {snapshot.snapshot_id} of {name} has inconsistent metadata"
            ) from None

    @staticmethod
    def _check_batch(
        registered: RegisteredTableDefinition, request: CommitRequest, batch: object
    ) -> None:
        if not isinstance(batch, pa.Table):
            raise BatchRejected(f"batch must be a pyarrow.Table, got {type(batch).__name__}")
        if batch.num_rows != request.row_count:
            raise BatchRejected(
                f"batch has {batch.num_rows} rows but the request declares {request.row_count}"
            )
        if not batch.schema.equals(registered.arrow_schema, check_metadata=False):
            raise BatchRejected(f"batch schema does not match {registered.definition_id}")
        actual = registered.fingerprint_rule.fingerprint(batch)
        if not isinstance(actual, str) or _SHA256_RE.fullmatch(actual) is None:
            raise CatalogIntegrityError("fingerprint rule did not return lowercase SHA-256 hex")
        if actual != request.batch_fingerprint:
            raise BatchRejected("batch fingerprint does not match the actual batch content")

    def _replay(
        self, name: str, iceberg: IcebergTable, request: CommitRequest
    ) -> CommitResult | None:
        """Return the first commit of ``request.batch_id`` on the main branch, if any.

        Iceberg ancestry must be a finite chain. Bound the iterator by the number of snapshots
        in this metadata version (plus a possible external starting snapshot) so corrupt parent
        cycles fail closed instead of hanging the idempotent replay path. The guard uses
        constant extra memory and accepts every valid ancestry.
        """
        snapshots = iceberg.metadata.snapshots
        max_ancestors = len(snapshots) + 1
        first_match: Snapshot | None = None
        match_count = 0
        for index, snapshot in enumerate(
            ancestors_of(iceberg.current_snapshot(), iceberg.metadata)
        ):
            if index >= max_ancestors:
                raise CatalogIntegrityError(f"table {name} has a cycle in snapshot history")
            if (
                snapshot.summary is not None
                and snapshot.summary.additional_properties.get(SUMMARY_BATCH_ID) == request.batch_id
            ):
                if first_match is None:
                    first_match = snapshot
                match_count += 1
        if match_count == 0:
            return None
        if match_count > 1:
            raise CatalogIntegrityError(f"batch {request.batch_id} was committed twice to {name}")
        assert first_match is not None
        info = self._snapshot_info(name, first_match)
        if (
            info.batch_fingerprint != request.batch_fingerprint
            or info.added_rows != request.row_count
        ):
            raise BatchConflict(
                f"batch {request.batch_id} of {name} was committed with different content"
            )
        return CommitResult(request=request, snapshot=info, outcome=CommitOutcome.ALREADY_COMMITTED)

    def _after_lost_race(self, name: str, request: CommitRequest) -> CommitResult:
        """PyIceberg rejected the commit: re-read, then replay or report the conflict."""
        iceberg = self._require(name)
        self._verified(name, iceberg)
        replay = self._replay(name, iceberg, request)
        if replay is not None:
            return replay
        raise CommitConflict(
            f"table {name} changed concurrently; expected parent "
            f"{request.expected_parent_snapshot_id} is no longer current"
        )

    @staticmethod
    def _summary_properties(
        registered: RegisteredTableDefinition, request: CommitRequest
    ) -> dict[str, str]:
        return {
            SUMMARY_BATCH_ID: request.batch_id,
            SUMMARY_BATCH_FINGERPRINT: request.batch_fingerprint,
            SUMMARY_BATCH_ROW_COUNT: str(request.row_count),
            SUMMARY_FINGERPRINT_RULE: registered.fingerprint_rule.rule_id,
        }


def connect_postgres_catalog(settings: Settings) -> SqlCatalog:
    """Open the PostgreSQL-backed PyIceberg ``SqlCatalog`` described by runtime ``settings``.

    Only PostgreSQL is accepted; there is no fallback. Connection failures raise
    ``CatalogUnavailable`` without echoing the DSN.
    """
    uri = settings.catalog_uri.get_secret_value()
    scheme = urlparse(uri).scheme.lower()
    if scheme != "postgresql" and not scheme.startswith("postgresql+"):
        raise CatalogUnavailable("catalog_uri must be a PostgreSQL DSN")
    with _backend("connect"):
        catalog = SqlCatalog(
            settings.catalog_name,
            uri=uri,
            warehouse=settings.warehouse_uri,
            pool_pre_ping="true",
        )
    if catalog.engine.dialect.name != "postgresql":
        catalog.close()
        raise CatalogUnavailable("catalog engine is not PostgreSQL")
    return catalog


def open_postgres_catalog_adapter(
    settings: Settings, registry: TableDefinitionRegistry
) -> PyIcebergCatalogAdapter:
    """Runtime entry point: PostgreSQL SQL catalog + ``file://`` warehouse from settings."""
    return PyIcebergCatalogAdapter(connect_postgres_catalog(settings), registry)
