"""Test-only definitions, batches and catalog harnesses for the C2 PyIceberg catalog.

- The minimal test definitions below are **not** C3 production tables.
- ``SqliteCatalogHarness`` injects a temporary SQLite ``SqlCatalog`` explicitly (fast unit tests;
  never PostgreSQL evidence). ``PostgresCatalogHarness`` goes through the runtime factory with a
  dedicated PostgreSQL *test* database from ``HLENS_TEST_CATALOG_URI``.
- Each harness uses a unique PyIceberg ``catalog_name`` and its own ``file://`` warehouse under
  ``tmp_path``; cleanup drops only that catalog's tables / namespaces through PyIceberg.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pydantic import SecretStr
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import DataScan
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DoubleType, LongType, NestedField, StringType

from infrastructure.catalog import (
    PyIcebergCatalogAdapter,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    connect_postgres_catalog,
)
from infrastructure.settings import Settings
from tests.contract_suites.catalog import CatalogSubject

TEST_CATALOG_URI_ENV: Final = "HLENS_TEST_CATALOG_URI"


@dataclass(frozen=True)
class RowsJsonSha256:
    """Minimal test fingerprint rule: SHA-256 of canonical JSON of column types + rows."""

    rule_id: str = "c2test.rows-json-sha256@1.0.0"

    def fingerprint(self, batch: pa.Table) -> str:
        document = {
            "columns": [[f.name, str(f.type), f.nullable] for f in batch.schema],
            "rows": batch.to_pylist(),
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


RULE: Final = RowsJsonSha256()


def _schema(*extra: NestedField) -> Schema:
    return Schema(
        NestedField(1, "seq", LongType(), required=True),
        NestedField(2, "tag", StringType(), required=True),
        NestedField(3, "price", DoubleType(), required=False),
        *extra,
    )


ALPHA: Final = RegisteredTableDefinition(
    table="c2test.alpha",
    definition_id="c2test.alpha",
    version="1.0.0",
    schema=_schema(),
    fingerprint_rule=RULE,
    properties={"write.parquet.compression-codec": "zstd"},
)
BETA: Final = RegisteredTableDefinition(
    table="c2test.beta",
    definition_id="c2test.beta",
    version="1.0.0",
    schema=_schema(),
    fingerprint_rule=RULE,
    partition_spec=PartitionSpec(
        PartitionField(source_id=2, field_id=1000, transform=IdentityTransform(), name="tag")
    ),
)
#: Same table as ALPHA, different binding (extra column).
ALPHA_V110: Final = RegisteredTableDefinition(
    table="c2test.alpha",
    definition_id="c2test.alpha",
    version="1.1.0",
    schema=_schema(NestedField(4, "note", StringType(), required=False)),
    fingerprint_rule=RULE,
)
REGISTRY: Final = TableDefinitionRegistry((ALPHA, BETA, ALPHA_V110))


def make_batch(rows: int, tag: str) -> pa.Table:
    """Deterministic batch for ALPHA / BETA: ``rows`` rows, content depends on ``tag``."""
    return pa.Table.from_pydict(
        {
            "seq": list(range(rows)),
            "tag": [tag] * rows,
            "price": [index * 1.25 for index in range(rows)],
        },
        schema=ALPHA.arrow_schema,
    )


def rows_of(table: pa.Table) -> list[tuple[Any, ...]]:
    """Sorted ``(tag, seq, price)`` rows for data-level comparisons."""
    return sorted((row["tag"], row["seq"], row["price"]) for row in table.to_pylist())


@dataclass
class _Harness:
    tmp_path: Path
    registry: TableDefinitionRegistry = REGISTRY
    catalog_name: str = field(default_factory=lambda: f"c2t_{uuid.uuid4().hex[:16]}")
    _opened: list[Catalog] = field(default_factory=list)

    @property
    def warehouse(self) -> Path:
        return self.tmp_path / "warehouse"

    @property
    def warehouse_uri(self) -> str:
        return self.warehouse.as_uri()

    def sql_catalog(self) -> SqlCatalog:
        raise NotImplementedError

    def open_adapter(
        self, registry: TableDefinitionRegistry | None = None
    ) -> PyIcebergCatalogAdapter:
        catalog = self.sql_catalog()
        return PyIcebergCatalogAdapter(catalog, self.registry if registry is None else registry)

    def subject(self) -> CatalogSubject[pa.Table]:
        return CatalogSubject(
            open=self.open_adapter,
            make_batch=make_batch,
            fingerprint=RULE.fingerprint,
            definitions=(ALPHA.binding, BETA.binding),
            conflicting_definition=ALPHA_V110.binding,
        )

    def cleanup(self) -> None:
        """Drop this harness's tables / namespaces through PyIceberg, close connections and
        delete its ``file://`` warehouse (Iceberg metadata + Parquet)."""
        try:
            catalog = self.sql_catalog()
            for namespace in catalog.list_namespaces():
                for identifier in catalog.list_tables(namespace):
                    catalog.drop_table(identifier)
                catalog.drop_namespace(namespace)
            leftover = catalog.list_namespaces()
            if leftover:
                raise AssertionError(f"cleanup left namespaces: {leftover}")
        finally:
            for opened in self._opened:
                opened.close()
            self._opened.clear()
        shutil.rmtree(self.warehouse, ignore_errors=True)
        if self.warehouse.exists():
            raise AssertionError("cleanup left the test warehouse behind")

    def cleanup_owned_tables(self, identifiers: tuple[tuple[str, ...], ...]) -> None:
        """Drop only explicitly owned tables; never sweep unrelated test catalog data."""
        try:
            catalog = self.sql_catalog()
            dropped_namespaces: set[tuple[str, ...]] = set()
            for identifier in identifiers:
                namespace = identifier[:-1]
                if namespace not in catalog.list_namespaces():
                    continue
                if identifier in catalog.list_tables(namespace):
                    catalog.drop_table(identifier)
                    dropped_namespaces.add(namespace)
            for namespace in dropped_namespaces:
                if namespace in catalog.list_namespaces() and not catalog.list_tables(namespace):
                    catalog.drop_namespace(namespace)
        finally:
            for opened in self._opened:
                opened.close()
            self._opened.clear()
        shutil.rmtree(self.warehouse, ignore_errors=True)
        if self.warehouse.exists():
            raise AssertionError("cleanup left the test warehouse behind")


@dataclass
class SqliteCatalogHarness(_Harness):
    """Explicitly injected temporary SQLite catalog (unit tests only)."""

    def sql_catalog(self) -> SqlCatalog:
        catalog = SqlCatalog(
            self.catalog_name,
            uri=f"sqlite:///{self.tmp_path / 'catalog.sqlite'}",
            warehouse=self.warehouse_uri,
        )
        self._opened.append(catalog)
        return catalog


def postgres_test_catalog_uri() -> str:
    """The dedicated PostgreSQL *test* database DSN, or skip when it is not configured."""
    uri = os.environ.get(TEST_CATALOG_URI_ENV, "").strip()
    if not uri:
        pytest.skip(f"{TEST_CATALOG_URI_ENV} not set: PostgreSQL catalog integration skipped")
    parsed = urlparse(uri)
    if not parsed.scheme.startswith("postgresql") or not parsed.path.rstrip("/").endswith("_test"):
        # Never print the DSN; only say what is wrong with it.
        raise pytest.UsageError(f"{TEST_CATALOG_URI_ENV} must name a PostgreSQL *_test database")
    return uri


@dataclass
class PostgresCatalogHarness(_Harness):
    """Runtime connection path (``connect_postgres_catalog``) against the PostgreSQL test DB."""

    uri: str = ""

    def settings(self) -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            catalog_uri=SecretStr(self.uri),
            catalog_name=self.catalog_name,
            warehouse_uri=self.warehouse_uri,
            staging_uri=(self.tmp_path / "staging").as_uri(),
        )

    def sql_catalog(self) -> SqlCatalog:
        catalog = connect_postgres_catalog(self.settings())
        self._opened.append(catalog)
        return catalog


class ScanSpy:
    """Records how PyIceberg scans are consumed: whole tables vs. streamed record batches.

    Installed with ``monkeypatch``, it wraps the adapter's streaming scan entrypoint and the
    legacy ``DataScan`` materializers so tests can prove that reads use bounded batches and do
    not materialise a whole history as one Arrow table.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.to_arrow_rows: list[int] = []
        self.readers = 0
        self.batch_rows: list[int] = []
        spy = self
        real_to_arrow = DataScan.to_arrow
        real_reader = DataScan.to_arrow_batch_reader
        real_scan_batches = PyIcebergCatalogAdapter.scan_column_batches

        def to_arrow(scan: DataScan, *args: Any, **kwargs: Any) -> pa.Table:
            table = real_to_arrow(scan, *args, **kwargs)
            spy.to_arrow_rows.append(table.num_rows)
            return table

        def to_arrow_batch_reader(
            scan: DataScan, *args: Any, **kwargs: Any
        ) -> pa.RecordBatchReader:
            spy.readers += 1
            reader = real_reader(scan, *args, **kwargs)
            batches = []
            for batch in reader:
                spy.batch_rows.append(batch.num_rows)
                batches.append(batch)
            return pa.RecordBatchReader.from_batches(reader.schema, batches)

        def scan_column_batches(adapter: PyIcebergCatalogAdapter, *args: Any, **kwargs: Any) -> Any:
            spy.readers += 1
            batches = real_scan_batches(adapter, *args, **kwargs)

            def tracked() -> Any:
                try:
                    for batch in batches:
                        spy.batch_rows.append(batch.num_rows)
                        yield batch
                finally:
                    close = getattr(batches, "close", None)
                    if callable(close):
                        close()

            return tracked()

        monkeypatch.setattr(DataScan, "to_arrow", to_arrow)
        monkeypatch.setattr(DataScan, "to_arrow_batch_reader", to_arrow_batch_reader)
        monkeypatch.setattr(PyIcebergCatalogAdapter, "scan_column_batches", scan_column_batches)

    @property
    def largest_table(self) -> int:
        """Rows of the largest single Arrow table materialised through ``to_arrow``."""
        return max(self.to_arrow_rows, default=0)
