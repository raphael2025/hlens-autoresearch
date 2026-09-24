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
  catalog and no local state besides the Iceberg files PyIceberg itself writes.

Runtime construction goes through ``open_postgres_catalog_adapter`` (PostgreSQL only). Tests may
inject any PyIceberg ``Catalog`` (for example a temporary SQLite ``SqlCatalog``) through the
``PyIcebergCatalogAdapter`` constructor; SQLite results are not PostgreSQL evidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Final, Self
from urllib.parse import urlparse

import pyarrow as pa  # type: ignore[import-untyped]
from pydantic import ValidationError
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import (
    CommitFailedException,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.table import Table as IcebergTable
from pyiceberg.table.snapshots import Snapshot, ancestors_of

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
)
from infrastructure.settings import Settings

__all__ = [
    "SUMMARY_BATCH_FINGERPRINT",
    "SUMMARY_BATCH_ID",
    "SUMMARY_BATCH_ROW_COUNT",
    "SUMMARY_FINGERPRINT_RULE",
    "CatalogIntegrityError",
    "CatalogUnavailable",
    "PyIcebergCatalogAdapter",
    "connect_postgres_catalog",
    "open_postgres_catalog_adapter",
]

SUMMARY_BATCH_ID: Final = "hlens.batch.id"
SUMMARY_BATCH_FINGERPRINT: Final = "hlens.batch.fingerprint"
SUMMARY_BATCH_ROW_COUNT: Final = "hlens.batch.row-count"
SUMMARY_FINGERPRINT_RULE: Final = "hlens.batch.fingerprint-rule"
_ADDED_RECORDS: Final = "added-records"
_TOTAL_RECORDS: Final = "total-records"

_SNAPSHOT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Top-level packages whose exceptions mean "the catalog database failed". They are matched by
#: module name so project code does not import these transitive-only packages (03-data.md §6.1).
_BACKEND_PACKAGES: Final = frozenset({"sqlalchemy", "psycopg2"})


class CatalogUnavailable(CatalogError):
    """The catalog database could not be reached or failed; nothing is assumed committed."""


class CatalogIntegrityError(CatalogError):
    """Stored Iceberg metadata disagrees with the registered definition or batch metadata."""


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


def _revalidated[M: (TableDefinition, CommitRequest)](model: type[M], value: object) -> M:
    if type(value) is not model:
        raise TypeError(f"expected {model.__name__}, got {type(value).__name__}")
    assert isinstance(value, model)
    return model.model_validate_json(value.model_dump_json())


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
        if iceberg.spec().fields != registered.partition_spec.fields:
            raise CatalogIntegrityError(f"table {name} partition spec differs from its definition")
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
        """Return the first commit of ``request.batch_id`` on the main branch, if any."""
        matches = [
            snapshot
            for snapshot in ancestors_of(iceberg.current_snapshot(), iceberg.metadata)
            if snapshot.summary is not None
            and snapshot.summary.additional_properties.get(SUMMARY_BATCH_ID) == request.batch_id
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise CatalogIntegrityError(f"batch {request.batch_id} was committed twice to {name}")
        info = self._snapshot_info(name, matches[0])
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
