"""Technology-migration tooling (Phase 14, ADR-0047 / ADR-0106): golden reruns, adapter
conformance, rollback evidence, migration targets and migration reports."""

from infrastructure.migration.conformance import ConformanceReport, run_conformance
from infrastructure.migration.golden import (
    GoldenDiff,
    GoldenError,
    GoldenRecord,
    compare_golden,
    load_golden,
    record_golden,
    save_golden,
)
from infrastructure.migration.rollback import RollbackEvidence, RollbackVerdict, rollback_evidence
from infrastructure.migration.target import (
    MigrationReport,
    MigrationTarget,
    load_migration_report,
    save_migration_report,
)

__all__ = [
    "ConformanceReport",
    "GoldenDiff",
    "GoldenError",
    "GoldenRecord",
    "MigrationReport",
    "MigrationTarget",
    "RollbackEvidence",
    "RollbackVerdict",
    "compare_golden",
    "load_golden",
    "load_migration_report",
    "record_golden",
    "rollback_evidence",
    "run_conformance",
    "save_golden",
    "save_migration_report",
]
