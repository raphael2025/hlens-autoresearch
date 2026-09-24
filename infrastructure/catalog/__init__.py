"""Data Plane Iceberg catalog adapter: PostgreSQL-backed PyIceberg SQL catalog (Phase 1 C2 / C3).

C2: adapter + definition registry mechanism. C3 / D3B: the twelve production tables
(``PHASE1_TABLES`` / ``PHASE1_REGISTRY`` / ``ensure_phase1_tables``), the frozen batch fingerprint
rule ``hlens.pyarrow-batch-sha256@1.0.0`` and explicit partition-spec evolution.
"""

from infrastructure.catalog.definitions import (
    BatchFingerprintRule,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    require_partition_only_change,
)
from infrastructure.catalog.fingerprint import (
    PYARROW_BATCH_FINGERPRINT,
    PYARROW_BATCH_FINGERPRINT_RULE_ID,
    PyArrowBatchFingerprint,
)
from infrastructure.catalog.iceberg_adapter import (
    CatalogIntegrityError,
    CatalogUnavailable,
    DefinitionEvolutionError,
    EvolutionOutcome,
    PartitionEvolutionResult,
    PyIcebergCatalogAdapter,
    connect_postgres_catalog,
    open_postgres_catalog_adapter,
)
from infrastructure.catalog.phase1_tables import (
    PHASE1_REGISTRY,
    PHASE1_TABLES,
    Phase1TableState,
    describe_partition_spec,
    ensure_phase1_tables,
)

__all__ = [
    "PHASE1_REGISTRY",
    "PHASE1_TABLES",
    "PYARROW_BATCH_FINGERPRINT",
    "PYARROW_BATCH_FINGERPRINT_RULE_ID",
    "BatchFingerprintRule",
    "CatalogIntegrityError",
    "CatalogUnavailable",
    "DefinitionEvolutionError",
    "EvolutionOutcome",
    "PartitionEvolutionResult",
    "Phase1TableState",
    "PyArrowBatchFingerprint",
    "PyIcebergCatalogAdapter",
    "RegisteredTableDefinition",
    "TableDefinitionRegistry",
    "connect_postgres_catalog",
    "describe_partition_spec",
    "ensure_phase1_tables",
    "open_postgres_catalog_adapter",
    "require_partition_only_change",
]
