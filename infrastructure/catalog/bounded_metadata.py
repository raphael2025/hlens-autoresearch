"""Bounded, infrastructure-private reads of one pinned Iceberg metadata version.

This module is deliberately separate from PyIceberg's public Catalog API. PyIceberg 0.12's
``SqlCatalog.load_table`` reads and validates the complete metadata document into Python lists.
The reader below pins the SQL catalog row once, incrementally validates its JSON arrays, spills
snapshots through ADR-0077 sorted runs, and retains only a content-key-tree root for snapshot
lookup. A compact, normally validated ``TableMetadataV2`` can then be made for one selected
snapshot; that view is intended for read-only scan planning and must never be serialized.

Every resource limit is supplied by the caller. This helper does not create temporary files or
use the process temporary directory. Objects published during a failed read remain harmless
content-addressed orphans, as required by ADR-0077.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import IcebergTables, SqlCatalog
from pyiceberg.io import FileIO, load_file_io
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.table.metadata import TableMetadata, TableMetadataUtil
from pyiceberg.table.snapshots import (
    MetadataLogEntry,
    Snapshot,
    SnapshotLogEntry,
)
from pyiceberg.table.sorting import SortOrder
from pyiceberg.table.statistics import PartitionStatisticsFile, StatisticsFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.contracts.catalog import SnapshotNotFound, TableNotFound, validate_table_name
from core.contracts.storage import StorageAdapter
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.streaming.content_key_tree import (
    ContentKeyTree,
    ContentKeyTreeError,
    ContentKeyTreeRoot,
    KeyTreeParams,
)
from infrastructure.streaming.runs import (
    RunLimits,
    RunRef,
    RunSetBuilder,
    RunWriteError,
    iter_run,
)

__all__ = [
    "BoundedIcebergMetadata",
    "BoundedMetadataError",
    "BoundedMetadataLimits",
    "pin_bounded_sql_table",
]

_ARRAY_MODELS: Final[dict[str, type[Any]]] = {
    "schemas": Schema,
    "partition-specs": PartitionSpec,
    "sort-orders": SortOrder,
    "snapshot-log": SnapshotLogEntry,
    "metadata-log": MetadataLogEntry,
    "statistics": StatisticsFile,
    "partition-statistics": PartitionStatisticsFile,
}
_SNAPSHOTS: Final = "snapshots"
_SCALAR_FIELDS: Final = frozenset(
    {
        "location",
        "table-uuid",
        "last-updated-ms",
        "last-column-id",
        "current-schema-id",
        "default-spec-id",
        "last-partition-id",
        "current-snapshot-id",
        "default-sort-order-id",
        "format-version",
        "last-sequence-number",
        "next-row-id",
    }
)
_MAP_FIELDS: Final = frozenset({"properties", "refs"})
_KNOWN_FIELDS: Final = _SCALAR_FIELDS | _MAP_FIELDS | frozenset(_ARRAY_MODELS) | {_SNAPSHOTS}
_SNAPSHOT_ID_RE: Final = re.compile(r"^[1-9][0-9]{0,18}$")


class BoundedMetadataError(ValueError):
    """Pinned Iceberg metadata is malformed, unsupported, or exceeds an explicit limit."""


@dataclass(frozen=True, slots=True)
class BoundedMetadataLimits:
    """Caller-selected limits for the metadata parser, snapshot runs, and point index."""

    max_metadata_bytes: int
    max_item_bytes: int
    max_retained_json_bytes: int
    read_chunk_bytes: int
    max_small_array_items: int
    max_map_items: int
    max_snapshots: int
    run_capacity: int
    run_limits: RunLimits
    run_merge_fanout: int
    key_tree_params: KeyTreeParams

    def __post_init__(self) -> None:
        for field in (
            "max_metadata_bytes",
            "max_item_bytes",
            "max_retained_json_bytes",
            "read_chunk_bytes",
            "max_small_array_items",
            "max_map_items",
            "max_snapshots",
            "run_capacity",
            "run_merge_fanout",
        ):
            value = getattr(self, field)
            minimum = 2 if field == "run_merge_fanout" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise BoundedMetadataError(f"{field} must be an integer >= {minimum}")


@dataclass(frozen=True, slots=True)
class _SnapshotIndex:
    root: ContentKeyTreeRoot | None
    count: int


class BoundedIcebergMetadata:
    """A pinned metadata pointer and bounded snapshot lookup index."""

    def __init__(
        self,
        *,
        name: str,
        metadata_location: str,
        metadata: TableMetadata,
        snapshots: _SnapshotIndex,
        storage: StorageAdapter,
        limits: BoundedMetadataLimits,
        file_io: FileIO,
        catalog: SqlCatalog,
        selected_snapshot_id: str | None,
    ) -> None:
        self.name = name
        self.metadata_location = metadata_location
        self.metadata = metadata
        self.snapshot_count = snapshots.count
        self._snapshot_root = snapshots.root
        self._storage = storage
        self._limits = limits
        self.file_io = file_io
        self.catalog = catalog
        # The one snapshot this caller's pinned view is allowed to observe.  The
        # metadata pointer and external index still contain its complete ancestry.
        self._selected_snapshot_id = selected_snapshot_id

    @property
    def selected_snapshot_id(self) -> str | None:
        """The immutable snapshot selection made when this metadata view was pinned."""
        return self._selected_snapshot_id

    def snapshot_by_id(self, snapshot_id: int) -> Snapshot | None:
        """Return the first-listed snapshot with this ID, matching PyIceberg's lookup rule."""
        value = self._index().get((str(snapshot_id),))
        if value is None:
            return None
        try:
            return Snapshot.model_validate_json(value)
        except Exception as exc:  # persisted tree bytes are integrity checked by the reader
            raise BoundedMetadataError(
                "snapshot index contains an invalid PyIceberg Snapshot"
            ) from exc

    def require_snapshot(self, snapshot_id: str) -> Snapshot:
        """Resolve a canonical positive ID, failing closed when it is absent."""
        if not isinstance(snapshot_id, str) or _SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None:
            raise SnapshotNotFound(f"table {self.name} has no snapshot {snapshot_id!r}")
        snapshot = self.snapshot_by_id(int(snapshot_id))
        if snapshot is None:
            raise SnapshotNotFound(f"table {self.name} has no snapshot {snapshot_id!r}")
        return snapshot

    def compact_table(self, snapshot_id: str) -> Table:
        """Build a validated read-only PyIceberg table view containing only one snapshot.

        The returned metadata is deliberately not serializable as an authoritative table
        metadata document: its snapshots list is a projection. It is suitable only for the
        adapter's pinned read planner, which needs the selected Snapshot plus schema/spec/property
        metadata. The caller must retain this restriction.
        """
        selected = self.require_snapshot(snapshot_id)
        raw = self.metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
        raw["snapshots"] = [json.loads(selected.model_dump_json(by_alias=True, exclude_none=True))]
        # TableMetadata validators still check current schema/spec/order and construct refs. They
        # do not require that current_snapshot_id be present in the snapshots array.
        compact = TableMetadataUtil.parse_raw(json.dumps(raw, separators=(",", ":")))
        io = load_file_io(
            {**self.catalog.properties, **compact.properties}, location=self.metadata_location
        )
        identifier = _table_identifier(self.name)
        return Table(
            identifier=identifier,
            metadata=compact,
            metadata_location=self.metadata_location,
            io=io,
            catalog=self.catalog,
        )

    def verification_table(self) -> Table:
        """Return a normal PyIceberg Table wrapper for the fully validated bounded projection.

        It has no snapshots by design. It is only for the adapter's schema/spec/property binding
        verifier; use :meth:`compact_table` for a fixed-snapshot scan.
        """
        return Table(
            identifier=_table_identifier(self.name),
            metadata=self.metadata,
            metadata_location=self.metadata_location,
            io=self.file_io,
            catalog=self.catalog,
        )

    def iter_history(self, snapshot_id: str) -> Iterator[Snapshot]:
        """Yield a start snapshot and its parent chain, newest first, with O(1) Python state."""
        first = self.require_snapshot(snapshot_id)
        start_id = first.snapshot_id
        wanted: int | None = start_id
        ahead: int | None = start_id
        walked = 0
        cycle_length: int | None = None

        def parent_of(snapshot_id_value: int | None) -> int | None:
            if snapshot_id_value is None:
                return None
            current = self.snapshot_by_id(snapshot_id_value)
            return None if current is None else current.parent_snapshot_id

        while wanted is not None:
            snapshot = self.snapshot_by_id(wanted)
            if snapshot is None:
                raise SnapshotNotFound(f"table {self.name} has no snapshot {wanted!r}")
            if snapshot.parent_snapshot_id == snapshot.snapshot_id:
                raise CatalogIntegrityError(f"table {self.name} snapshot names itself as parent")
            if cycle_length is None and walked and snapshot.snapshot_id == ahead:
                # Determine the cycle's distinct-node count to match history()'s fail-closed
                # behavior: every distinct member is yielded, then the repeated pointer errors.
                behind: int | None = start_id
                lead: int | None = snapshot.snapshot_id
                entry = 0
                while behind != lead:
                    behind, lead, entry = parent_of(behind), parent_of(lead), entry + 1
                lead, length = parent_of(behind), 1
                while lead != behind:
                    lead, length = parent_of(lead), length + 1
                cycle_length = entry + length
            if walked == cycle_length:
                raise CatalogIntegrityError(f"table {self.name} has a cycle in snapshot history")
            yield snapshot
            walked += 1
            wanted = snapshot.parent_snapshot_id
            if cycle_length is None:
                ahead = parent_of(parent_of(ahead))

    def _index(self) -> ContentKeyTree:
        if self._snapshot_root is None:
            return ContentKeyTree(self._storage, self._limits.key_tree_params)
        return ContentKeyTree.open(self._storage, self._limits.key_tree_params, self._snapshot_root)


def pin_bounded_sql_table(
    catalog: SqlCatalog,
    table: str,
    *,
    storage: StorageAdapter,
    limits: BoundedMetadataLimits,
    selected_snapshot_id: str | None = None,
    select_current_head: bool = True,
) -> BoundedIcebergMetadata:
    """Read one SqlCatalog table row, pin its immutable metadata file, and build bounded indexes."""
    if not isinstance(catalog, SqlCatalog):
        raise TypeError("bounded metadata pinning currently requires a PyIceberg SqlCatalog")
    name = validate_table_name(table)
    identifier = _table_identifier(name)
    namespace = Catalog.namespace_to_string(identifier[:-1])
    with Session(catalog.engine) as session:
        row = session.scalar(
            select(IcebergTables).where(
                IcebergTables.catalog_name == catalog.name,
                IcebergTables.table_namespace == namespace,
                IcebergTables.table_name == identifier[-1],
            )
        )
        if row is None:
            raise TableNotFound(f"table {name} does not exist")
        metadata_location = row.metadata_location
    if not isinstance(metadata_location, str) or not metadata_location:
        raise CatalogIntegrityError(f"table {name} has no metadata location")

    input_io = load_file_io(properties=catalog.properties, location=metadata_location)
    input_file = input_io.new_input(metadata_location)
    snapshots_run: RunRef | None = None
    with _snapshot_run_builder(storage, limits) as builder:
        try:
            with input_file.open() as raw_stream:
                stream = (
                    gzip.GzipFile(fileobj=raw_stream)
                    if metadata_location.endswith(".gz.metadata.json")
                    else raw_stream
                )
                reader = _ByteReader(
                    stream,
                    max_bytes=limits.max_metadata_bytes,
                    max_item_bytes=limits.max_item_bytes,
                    chunk_size=limits.read_chunk_bytes,
                )
                metadata_dict = _parse_metadata(reader, builder, limits)
        except BoundedMetadataError:
            raise
        except Exception as exc:
            raise CatalogIntegrityError(f"table {name} metadata is malformed") from exc
        try:
            snapshots_run = builder.finish()
        except (RunWriteError, ValueError) as exc:
            raise CatalogIntegrityError(
                f"table {name} snapshot run could not be finalized"
            ) from exc

    if snapshots_run is None:
        tree, snapshot_count = ContentKeyTree(storage, limits.key_tree_params), 0
    else:
        tree, snapshot_count = _build_snapshot_tree(storage, snapshots_run, limits)
    try:
        # This is the same PyIceberg metadata parser and all of its whole-model validators. The
        # only removed list is snapshots, whose individual members were all Snapshot-validated.
        metadata_dict["snapshots"] = []
        metadata = TableMetadataUtil.parse_raw(json.dumps(metadata_dict, separators=(",", ":")))
    except Exception as exc:
        raise CatalogIntegrityError(
            f"table {name} metadata failed PyIceberg model validation"
        ) from exc
    selected = (
        (None if metadata.current_snapshot_id is None else str(metadata.current_snapshot_id))
        if select_current_head
        else selected_snapshot_id
    )
    bounded = BoundedIcebergMetadata(
        name=name,
        metadata_location=metadata_location,
        metadata=metadata,
        snapshots=_SnapshotIndex(root=tree.root_ref, count=snapshot_count),
        storage=storage,
        limits=limits,
        file_io=load_file_io(
            {**catalog.properties, **metadata.properties}, location=metadata_location
        ),
        catalog=catalog,
        selected_snapshot_id=selected,
    )
    if selected is not None:
        bounded.require_snapshot(selected)
    return bounded


class _ByteReader:
    """A chunked byte reader with a hard cap on decompressed metadata bytes."""

    def __init__(
        self,
        stream: Any,
        *,
        max_bytes: int,
        max_item_bytes: int,
        chunk_size: int,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._max_item_bytes = max_item_bytes
        self._chunk_size = chunk_size
        self._buffer = b""
        self._offset = 0
        self._total = 0

    def _check_total(self) -> None:
        """Fail closed once the decompressed document passes its cap, naming both values."""
        if self._total > self._max_bytes:
            raise BoundedMetadataError(
                f"Iceberg metadata exceeds max_metadata_bytes "
                f"(limit {self._max_bytes}, read at least {self._total} bytes)"
            )

    def byte(self) -> int | None:
        if self._offset >= len(self._buffer):
            remaining = self._max_bytes - self._total
            chunk = self._stream.read(min(self._chunk_size, remaining + 1))
            if not chunk:
                return None
            self._total += len(chunk)
            self._check_total()
            self._buffer = chunk
            self._offset = 0
        value = self._buffer[self._offset]
        self._offset += 1
        return value

    def peek(self) -> int | None:
        """The next raw byte (whitespace included) without consuming it; ``None`` at the end."""
        if self._offset >= len(self._buffer):
            remaining = self._max_bytes - self._total
            chunk = self._stream.read(min(self._chunk_size, remaining + 1))
            if not chunk:
                return None
            self._total += len(chunk)
            self._check_total()
            self._buffer = chunk
            self._offset = 0
        return self._buffer[self._offset]

    def peek_non_whitespace(self) -> int | None:
        while True:
            if self._offset >= len(self._buffer):
                remaining = self._max_bytes - self._total
                chunk = self._stream.read(min(self._chunk_size, remaining + 1))
                if not chunk:
                    return None
                self._total += len(chunk)
                self._check_total()
                self._buffer = chunk
                self._offset = 0
            while self._offset < len(self._buffer) and self._buffer[self._offset] in b" \t\r\n":
                self._offset += 1
            if self._offset < len(self._buffer):
                return self._buffer[self._offset]

    def whitespace(self) -> None:
        while self.peek_non_whitespace() in (ord(" "), ord("\t"), ord("\r"), ord("\n")):
            self.byte()

    def take(self) -> int:
        value = self.byte()
        if value is None:
            raise BoundedMetadataError("unexpected end of Iceberg metadata JSON")
        return value

    def raw_value(self, *, max_bytes: int) -> bytes:
        self.whitespace()
        first = self.take()
        out = bytearray([first])
        if first in (ord("{"), ord("[")):
            depth = 1
            quoted = False
            escaped = False
            while depth:
                char = self.take()
                out.append(char)
                if len(out) > max_bytes:
                    raise _item_exceeds(max_bytes, len(out))
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == ord("\\"):
                        escaped = True
                    elif char == ord('"'):
                        quoted = False
                elif char == ord('"'):
                    quoted = True
                elif char in (ord("{"), ord("[")):
                    depth += 1
                elif char in (ord("}"), ord("]")):
                    depth -= 1
        elif first == ord('"'):
            escaped = False
            while True:
                char = self.take()
                out.append(char)
                if len(out) > max_bytes:
                    raise _item_exceeds(max_bytes, len(out))
                if escaped:
                    escaped = False
                elif char == ord("\\"):
                    escaped = True
                elif char == ord('"'):
                    break
        else:
            # A scalar ends at the first raw delimiter or whitespace byte: peeking past
            # whitespace would splice ``12 34`` into ``1234`` instead of failing closed.
            while True:
                next_char = self.peek()
                if next_char is None or next_char in b",]} \t\r\n":
                    break
                out.append(self.take())
                if len(out) > max_bytes:
                    raise _item_exceeds(max_bytes, len(out))
        if len(out) > max_bytes:
            raise _item_exceeds(max_bytes, len(out))
        return bytes(out)

    def json_value(self, *, max_bytes: int) -> Any:
        raw = self.raw_value(max_bytes=max_bytes)
        try:
            return json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, BoundedMetadataError) as exc:
            raise BoundedMetadataError("Iceberg metadata contains invalid JSON") from exc

    def array(
        self,
        callback: Callable[[Any, int, int], None],
        *,
        max_items: int,
        cap_name: str = "array item cap",
    ) -> int:
        self.whitespace()
        if self.take() != ord("["):
            raise BoundedMetadataError("expected an Iceberg metadata array")
        count = 0
        self.whitespace()
        if self.peek_non_whitespace() == ord("]"):
            self.take()
            return count
        while True:
            raw = self.raw_value(max_bytes=self._max_item_bytes)
            value = self._decode(raw)
            if count >= max_items:
                raise BoundedMetadataError(
                    f"Iceberg metadata array exceeds {cap_name} "
                    f"(limit {max_items}, observed at least {count + 1} items)"
                )
            callback(value, count, len(raw))
            count += 1
            self.whitespace()
            delimiter = self.take()
            if delimiter == ord("]"):
                return count
            if delimiter != ord(","):
                raise BoundedMetadataError("expected comma or closing bracket in metadata array")

    def object(self, callback: Callable[[str, Any, int, int], None], *, max_items: int) -> int:
        self.whitespace()
        if self.take() != ord("{"):
            raise BoundedMetadataError("expected an Iceberg metadata object")
        count = 0
        self.whitespace()
        if self.peek_non_whitespace() == ord("}"):
            self.take()
            return count
        seen: set[str] = set()
        while True:
            key_raw = self.raw_value(max_bytes=self._max_item_bytes)
            try:
                key = json.loads(key_raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BoundedMetadataError("metadata object has an invalid key") from exc
            if not isinstance(key, str) or key in seen:
                raise BoundedMetadataError("metadata object has a duplicate or non-string key")
            seen.add(key)
            self.whitespace()
            if self.take() != ord(":"):
                raise BoundedMetadataError("expected colon in metadata object")
            raw_value = self.raw_value(max_bytes=self._max_item_bytes)
            value = self._decode(raw_value)
            if count >= max_items:
                raise BoundedMetadataError(
                    "Iceberg metadata map exceeds its configured item cap "
                    f"(max_map_items limit {max_items}, observed at least {count + 1} items)"
                )
            callback(key, value, count, len(key_raw) + len(raw_value))
            count += 1
            self.whitespace()
            delimiter = self.take()
            if delimiter == ord("}"):
                return count
            if delimiter != ord(","):
                raise BoundedMetadataError("expected comma or closing brace in metadata object")

    @staticmethod
    def _decode(raw: bytes) -> Any:
        try:
            return json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, BoundedMetadataError) as exc:
            raise BoundedMetadataError("Iceberg metadata contains invalid JSON") from exc

    def json_value_with_size(self, *, max_bytes: int) -> tuple[Any, int]:
        raw = self.raw_value(max_bytes=max_bytes)
        return self._decode(raw), len(raw)


def _item_exceeds(limit: int, observed: int) -> BoundedMetadataError:
    """The one-item cap error, naming the configured limit and the bytes seen when it tripped."""
    return BoundedMetadataError(
        f"Iceberg metadata item exceeds max_item_bytes "
        f"(limit {limit}, observed at least {observed} bytes)"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BoundedMetadataError("metadata JSON contains a duplicate object key")
        result[key] = value
    return result


def _parse_metadata(
    reader: _ByteReader, builder: RunSetBuilder, limits: BoundedMetadataLimits
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    seen: set[str] = set()
    retained_json_bytes = 0

    def retain(size: int) -> None:
        nonlocal retained_json_bytes
        retained_json_bytes += size
        if retained_json_bytes > limits.max_retained_json_bytes:
            raise BoundedMetadataError(
                "retained Iceberg metadata exceeds max_retained_json_bytes "
                f"(limit {limits.max_retained_json_bytes}, observed {retained_json_bytes} bytes)"
            )

    reader.whitespace()
    if reader.take() != ord("{"):
        raise BoundedMetadataError("Iceberg metadata root must be a JSON object")
    reader.whitespace()
    if reader.peek_non_whitespace() == ord("}"):
        reader.take()
        raise BoundedMetadataError("Iceberg metadata object is empty")
    while True:
        raw_key = reader.raw_value(max_bytes=limits.max_item_bytes)
        try:
            key = json.loads(raw_key)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BoundedMetadataError("Iceberg metadata has an invalid top-level key") from exc
        if not isinstance(key, str) or key in seen:
            raise BoundedMetadataError(
                "Iceberg metadata has a duplicate or non-string top-level key"
            )
        if key not in _KNOWN_FIELDS:
            raise BoundedMetadataError(f"Iceberg metadata has unsupported top-level field {key!r}")
        seen.add(key)
        reader.whitespace()
        if reader.take() != ord(":"):
            raise BoundedMetadataError("expected colon after Iceberg metadata key")
        first = reader.peek_non_whitespace()
        if key == _SNAPSHOTS:

            def add_snapshot(value: Any, ordinal: int, _encoded_size: int) -> None:
                try:
                    snapshot = Snapshot.model_validate(value)
                    encoded = snapshot.model_dump_json(by_alias=True, exclude_none=True)
                except Exception as exc:
                    raise BoundedMetadataError(
                        "snapshot fails PyIceberg Snapshot validation"
                    ) from exc
                builder.add(
                    {
                        "snapshot_id": str(snapshot.snapshot_id),
                        "ordinal": ordinal,
                        "snapshot": encoded,
                    }
                )

            reader.array(
                add_snapshot,
                max_items=limits.max_snapshots,
                cap_name="max_snapshots",
            )
            document[key] = []
        elif key in _ARRAY_MODELS:
            model = _ARRAY_MODELS[key]
            values: list[Any] = []

            def add_model(
                value: Any,
                _ordinal: int,
                encoded_size: int,
                model: type[Any] = model,
                field_name: str = key,
                output: list[Any] = values,
            ) -> None:
                try:
                    model.model_validate(value)
                except Exception as exc:
                    raise BoundedMetadataError(
                        f"metadata {field_name} entry fails PyIceberg model validation"
                    ) from exc
                retain(encoded_size)
                output.append(value)

            reader.array(
                add_model,
                max_items=limits.max_small_array_items,
                cap_name="max_small_array_items",
            )
            document[key] = values
        elif key in _MAP_FIELDS:
            values_map: dict[str, Any] = {}

            def add_map(
                map_key: str,
                value: Any,
                _ordinal: int,
                encoded_size: int,
                output: dict[str, Any] = values_map,
            ) -> None:
                retain(encoded_size)
                output[map_key] = value

            reader.object(add_map, max_items=limits.max_map_items)
            document[key] = values_map
        elif first in (ord("["), ord("{")):
            raise BoundedMetadataError(f"Iceberg metadata field {key!r} has the wrong JSON shape")
        else:
            value, encoded_size = reader.json_value_with_size(max_bytes=limits.max_item_bytes)
            retain(encoded_size)
            document[key] = value
        reader.whitespace()
        delimiter = reader.take()
        if delimiter == ord("}"):
            break
        if delimiter != ord(","):
            raise BoundedMetadataError("expected comma or closing brace in Iceberg metadata")
    if reader.peek_non_whitespace() is not None:
        raise BoundedMetadataError("trailing data follows Iceberg metadata object")
    if document.get("format-version") != 2:
        raise BoundedMetadataError("bounded metadata path supports Iceberg format version 2 only")
    return document


def _snapshot_run_builder(storage: StorageAdapter, limits: BoundedMetadataLimits) -> RunSetBuilder:
    return RunSetBuilder(
        storage,
        key=lambda row: (row["snapshot_id"], row["ordinal"]),
        capacity=limits.run_capacity,
        limits=limits.run_limits,
        merge_fanout=limits.run_merge_fanout,
    )


def _build_snapshot_tree(
    storage: StorageAdapter, run: RunRef, limits: BoundedMetadataLimits
) -> tuple[ContentKeyTree, int]:
    def rows() -> Iterator[tuple[tuple[str, ...], str]]:
        previous: str | None = None
        with iter_run(storage, run) as records:
            for row in records:
                snapshot_id = row["snapshot_id"]
                if snapshot_id == previous:
                    # The run's ordinal tie break preserves TableMetadata.snapshot_by_id's
                    # first-listed duplicate behavior.
                    continue
                previous = snapshot_id
                yield (snapshot_id,), row["snapshot"]

    try:
        tree = ContentKeyTree.build(storage, rows(), params=limits.key_tree_params)
        return tree, run.record_count
    except (ContentKeyTreeError, RunWriteError) as exc:
        raise BoundedMetadataError(
            f"snapshot index failed its configured bounds ({limits.key_tree_params!r}, "
            f"{run.record_count} snapshot records): {exc}"
        ) from exc


def _table_identifier(name: str) -> tuple[str, ...]:
    parts = tuple(name.split("."))
    return parts
