"""Rollback evidence for a migration (Phase 14; 10-migration.md §4, ADR-0047).

A migration that is rolled back must leave the golden experiments exactly as they were.
``rollback_evidence`` combines two golden comparisons against the **same** golden record — the rerun
on the migrated stack and the rerun after rolling back — into an immutable, hashed record:

- ``RESTORED`` iff the rolled-back rerun is bit-identical to the golden record. A rollback restores
  the old stack, so nothing short of bit-identity counts: the rolled-back comparison must have been
  made with tolerance 0 (otherwise it is refused), and every output that differs is listed;
- ``NOT_RESTORED`` otherwise.

This is **evidence only**. It performs no rollback, changes no state, and decides nothing about the
migration itself; the migration ADR cites it (roadmap P14: "迁移导致历史实验不可复现" is the
failure mode it detects).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from core.domain.base import content_hash
from infrastructure.migration.golden import GoldenDiff, GoldenError, GoldenRecord

__all__ = ["RollbackEvidence", "RollbackVerdict", "rollback_evidence"]


class RollbackVerdict(StrEnum):
    RESTORED = "restored"
    NOT_RESTORED = "not_restored"


@dataclass(frozen=True, slots=True)
class RollbackEvidence:
    migration_id: str
    golden_name: str
    golden_record_hash: str
    golden_hash: str
    migrated_hash: str
    migrated_tolerance: Decimal
    migrated_passed: bool
    rolled_back_hash: str
    verdict: RollbackVerdict
    #: Output names that still differ after the rollback (sorted; empty when restored).
    residual_differences: tuple[str, ...]

    @property
    def evidence_hash(self) -> str:
        return content_hash(
            {
                "migration_id": self.migration_id,
                "golden_name": self.golden_name,
                "golden_record_hash": self.golden_record_hash,
                "golden_hash": self.golden_hash,
                "migrated_hash": self.migrated_hash,
                "migrated_tolerance": str(self.migrated_tolerance),
                "migrated_passed": self.migrated_passed,
                "rolled_back_hash": self.rolled_back_hash,
                "verdict": self.verdict.value,
                "residual_differences": list(self.residual_differences),
            }
        )


def rollback_evidence(
    migration_id: str, golden: GoldenRecord, migrated: GoldenDiff, rolled_back: GoldenDiff
) -> RollbackEvidence:
    """Evidence that rolling back restored ``golden`` (see the module docstring)."""
    if not isinstance(migration_id, str) or not migration_id.strip():
        raise GoldenError("a rollback needs a migration id")
    for label, diff in (("migrated", migrated), ("rolled_back", rolled_back)):
        if not isinstance(diff, GoldenDiff):
            raise GoldenError(f"{label} must be a GoldenDiff")
        if diff.name != golden.name or diff.golden_hash != golden.outputs_hash:
            raise GoldenError(f"{label} was not compared against the golden record {golden.name}")
        if not diff.rerun_hash:
            raise GoldenError(f"{label} carries no rerun hash")
        if diff.bit_identical != (diff.rerun_hash == golden.outputs_hash):
            raise GoldenError(f"{label} is inconsistent (bit_identical vs hashes)")
    if rolled_back.tolerance != 0:
        raise GoldenError("the rolled-back rerun must be compared with tolerance 0 (bit-exact)")
    restored = rolled_back.bit_identical
    residual = tuple(sorted(rolled_back.differences))
    if restored and residual:
        raise GoldenError("rolled_back is bit-identical but reports differences")
    return RollbackEvidence(
        migration_id=migration_id.strip(),
        golden_name=golden.name,
        golden_record_hash=golden.record_hash,
        golden_hash=golden.outputs_hash,
        migrated_hash=migrated.rerun_hash,
        migrated_tolerance=migrated.tolerance,
        migrated_passed=migrated.passed,
        rolled_back_hash=rolled_back.rerun_hash,
        verdict=RollbackVerdict.RESTORED if restored else RollbackVerdict.NOT_RESTORED,
        residual_differences=residual,
    )
