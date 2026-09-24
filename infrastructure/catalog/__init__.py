"""Data Plane Iceberg catalog adapter (Phase 1 C2): PostgreSQL-backed PyIceberg SQL catalog."""

from infrastructure.catalog.definitions import (
    BatchFingerprintRule,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
)
from infrastructure.catalog.iceberg_adapter import (
    CatalogIntegrityError,
    CatalogUnavailable,
    PyIcebergCatalogAdapter,
    connect_postgres_catalog,
    open_postgres_catalog_adapter,
)

__all__ = [
    "BatchFingerprintRule",
    "CatalogIntegrityError",
    "CatalogUnavailable",
    "PyIcebergCatalogAdapter",
    "RegisteredTableDefinition",
    "TableDefinitionRegistry",
    "connect_postgres_catalog",
    "open_postgres_catalog_adapter",
]
