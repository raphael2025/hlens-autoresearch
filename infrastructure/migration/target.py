"""Migration target description and migration report (Phase 14; ADR-0106 decision 4, ADR-0047).

``MigrationTarget`` states what one migration is: an id, the source and target identities, the
golden tolerance (declared by the migration's own ADR; it is not a validation threshold),
the **declared** ``excluded_outputs`` (outputs a migration cannot reproduce by construction, e.g. a
hash that embeds the provider identity) and the scope / out-of-scope / limitation statements. It
projects and compares golden outputs: an excluded output is removed from both sides of the
comparison, and an exclusion that names nothing in the golden record is refused (no vacuous
declarations).

``MigrationReport`` summarizes one migration drill: the conformance runs, the golden diff
(exclusions applied) and the rollback evidence. It is **evidence only** — it decides nothing and
runs nothing.
``passed`` iff every conformance run passed, the golden diff has no difference and the rollback is
``RESTORED``; a failed report is still a complete, saveable record (failures are never dropped).

Persistence follows ``golden.py``: canonical JSON, ``report_hash`` = SHA-256 of the file bytes,
``save_migration_report`` is write-once and no-clobber (an identical re-save is a no-op),
``load_migration_report`` re-hashes the file, rebuilds every part and re-derives the target hash,
the evidence hash and the verdict, refusing any mismatch. The migration matrix itself (which
targets, which checks) lives in ``tests/``; this module imports nothing from there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from core.domain.base import canonical_json, content_hash
from infrastructure.migration.conformance import ConformanceReport
from infrastructure.migration.golden import (
    GoldenDiff,
    GoldenError,
    GoldenRecord,
    compare_golden,
    record_golden,
)
from infrastructure.migration.rollback import RollbackEvidence, RollbackVerdict

__all__ = [
    "MigrationReport",
    "MigrationTarget",
    "load_migration_report",
    "save_migration_report",
]

_FORMAT: Final = "hlens.migration_report"
_SCHEMA_VERSION: Final = "1.0.0"
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_TARGET_KEYS: Final = frozenset(
    {
        "migration_id",
        "source",
        "target",
        "tolerance",
        "excluded_outputs",
        "scope",
        "out_of_scope",
        "limitations",
        "target_hash",
    }
)
_REPORT_KEYS: Final = frozenset(
    {
        "format",
        "schema_version",
        "target",
        "golden",
        "conformance",
        "golden_diff",
        "rollback",
        "verdict",
        "failures",
    }
)
_GOLDEN_KEYS: Final = frozenset({"name", "record_hash"})
_CONFORMANCE_KEYS: Final = frozenset({"candidate", "checks", "failures", "passed"})
_DIFF_KEYS: Final = frozenset(
    {"name", "tolerance", "bit_identical", "golden_hash", "rerun_hash", "differences"}
)
_ROLLBACK_KEYS: Final = frozenset(
    {
        "migration_id",
        "golden_name",
        "golden_record_hash",
        "golden_hash",
        "migrated_hash",
        "migrated_tolerance",
        "migrated_passed",
        "rolled_back_hash",
        "verdict",
        "residual_differences",
        "evidence_hash",
    }
)


def _texts(label: str, values: Sequence[str], *, sort: bool) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise GoldenError(f"{label} must be a sequence of strings")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise GoldenError(f"{label} entries must be non-empty strings")
        cleaned.append(value.strip())
    if len(set(cleaned)) != len(cleaned):
        raise GoldenError(f"{label} must not repeat an entry")
    return tuple(sorted(cleaned)) if sort else tuple(cleaned)


@dataclass(frozen=True, slots=True)
class MigrationTarget:
    """What one migration is (see the module docstring)."""

    migration_id: str
    source: str
    target: str
    tolerance: Decimal
    excluded_outputs: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in ("migration_id", "source", "target"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise GoldenError(f"a migration target needs a non-empty {label}")
            object.__setattr__(self, label, value.strip())
        if self.source == self.target:
            raise GoldenError("a migration's source and target identities must differ")
        if (
            not isinstance(self.tolerance, Decimal)
            or not self.tolerance.is_finite()
            or self.tolerance < 0
        ):
            raise GoldenError("tolerance must be a finite, non-negative Decimal")
        object.__setattr__(
            self, "excluded_outputs", _texts("excluded_outputs", self.excluded_outputs, sort=True)
        )
        if not self.scope:
            raise GoldenError("a migration target must declare its scope")
        object.__setattr__(self, "scope", _texts("scope", self.scope, sort=False))
        object.__setattr__(
            self, "out_of_scope", _texts("out_of_scope", self.out_of_scope, sort=False)
        )
        object.__setattr__(self, "limitations", _texts("limitations", self.limitations, sort=False))

    def _payload(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "source": self.source,
            "target": self.target,
            "tolerance": str(self.tolerance),
            "excluded_outputs": list(self.excluded_outputs),
            "scope": list(self.scope),
            "out_of_scope": list(self.out_of_scope),
            "limitations": list(self.limitations),
        }

    @property
    def target_hash(self) -> str:
        return content_hash(self._payload())

    def project(self, outputs: Mapping[str, Decimal]) -> dict[str, Decimal]:
        """``outputs`` without the declared exclusions."""
        skipped = set(self.excluded_outputs)
        return {key: value for key, value in outputs.items() if key not in skipped}

    def compare(
        self, golden: GoldenRecord, rerun: Callable[[], Mapping[str, Decimal]]
    ) -> GoldenDiff:
        """``compare_golden`` at this migration's tolerance with the exclusions removed from both
        sides. Every excluded name must exist in the golden record (and is reported as excluded by
        ``MigrationReport``, never silently dropped)."""
        if not isinstance(golden, GoldenRecord):
            raise GoldenError("compare needs a GoldenRecord")
        unknown = sorted(set(self.excluded_outputs) - set(golden.outputs))
        if unknown:
            raise GoldenError(f"excluded outputs not in the golden record {golden.name}: {unknown}")
        kept = record_golden(golden.name, lambda: self.project(golden.outputs))
        return compare_golden(kept, lambda: self.project(rerun()), self.tolerance)


def _conformance_payload(report: ConformanceReport) -> dict[str, Any]:
    return {
        "candidate": report.candidate,
        "checks": list(report.checks),
        "failures": [[name, message] for name, message in report.failures],
        "passed": report.passed,
    }


def _diff_payload(diff: GoldenDiff) -> dict[str, Any]:
    return {
        "name": diff.name,
        "tolerance": str(diff.tolerance),
        "bit_identical": diff.bit_identical,
        "golden_hash": diff.golden_hash,
        "rerun_hash": diff.rerun_hash,
        "differences": {
            key: [None if value is None else str(value) for value in pair]
            for key, pair in sorted(diff.differences.items())
        },
    }


def _rollback_payload(evidence: RollbackEvidence) -> dict[str, Any]:
    return {
        "migration_id": evidence.migration_id,
        "golden_name": evidence.golden_name,
        "golden_record_hash": evidence.golden_record_hash,
        "golden_hash": evidence.golden_hash,
        "migrated_hash": evidence.migrated_hash,
        "migrated_tolerance": str(evidence.migrated_tolerance),
        "migrated_passed": evidence.migrated_passed,
        "rolled_back_hash": evidence.rolled_back_hash,
        "verdict": evidence.verdict.value,
        "residual_differences": list(evidence.residual_differences),
        "evidence_hash": evidence.evidence_hash,
    }


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """The evidence of one migration drill (see the module docstring)."""

    target: MigrationTarget
    golden_name: str
    golden_record_hash: str
    conformance: tuple[ConformanceReport, ...]
    #: The golden diff with ``target.excluded_outputs`` removed from both sides.
    golden_diff: GoldenDiff
    rollback: RollbackEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.target, MigrationTarget):
            raise GoldenError("a report needs a MigrationTarget")
        if not isinstance(self.golden_name, str) or not self.golden_name:
            raise GoldenError("a report needs the golden record's name")
        if not isinstance(self.golden_record_hash, str) or not _HEX64.fullmatch(
            self.golden_record_hash
        ):
            raise GoldenError("a report needs the golden record's hash")
        conformance = tuple(self.conformance)
        if not conformance or not all(isinstance(item, ConformanceReport) for item in conformance):
            raise GoldenError("a report needs at least one ConformanceReport")
        if self.target.target not in {item.candidate for item in conformance}:
            raise GoldenError(
                f"the migration target {self.target.target} was not run through conformance"
            )
        object.__setattr__(self, "conformance", conformance)
        if not isinstance(self.golden_diff, GoldenDiff) or not isinstance(
            self.rollback, RollbackEvidence
        ):
            raise GoldenError("a report needs a GoldenDiff and a RollbackEvidence")
        if (
            self.golden_diff.name != self.golden_name
            or self.rollback.golden_name != self.golden_name
        ):
            raise GoldenError(
                "the golden diff and the rollback evidence must name the golden record"
            )
        if self.rollback.golden_record_hash != self.golden_record_hash:
            raise GoldenError("the rollback evidence was made against another golden record")
        if self.rollback.migration_id != self.target.migration_id:
            raise GoldenError("the rollback evidence belongs to another migration")
        if self.golden_diff.tolerance != self.target.tolerance:
            raise GoldenError("the golden diff was not compared at the target's tolerance")
        if set(self.golden_diff.differences) & set(self.target.excluded_outputs):
            raise GoldenError("the golden diff reports an output the target declares excluded")

    @property
    def failures(self) -> tuple[str, ...]:
        """Why the migration did not pass (empty when it did), in a fixed order."""
        reasons: list[str] = []
        for report in self.conformance:
            if not report.checks:
                reasons.append(f"conformance:{report.candidate}:no checks")
            reasons.extend(f"conformance:{report.candidate}:{name}" for name, _ in report.failures)
        reasons.extend(f"golden:{key}" for key in sorted(self.golden_diff.differences))
        if self.rollback.verdict is not RollbackVerdict.RESTORED:
            reasons.append("rollback:not_restored")
        return tuple(reasons)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def verdict(self) -> str:
        return "passed" if self.passed else "failed"

    def _payload(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "schema_version": _SCHEMA_VERSION,
            "target": {**self.target._payload(), "target_hash": self.target.target_hash},
            "golden": {"name": self.golden_name, "record_hash": self.golden_record_hash},
            "conformance": [_conformance_payload(item) for item in self.conformance],
            "golden_diff": _diff_payload(self.golden_diff),
            "rollback": _rollback_payload(self.rollback),
            "verdict": self.verdict,
            "failures": list(self.failures),
        }

    @property
    def report_hash(self) -> str:
        """SHA-256 of the saved file (canonical JSON of the whole report)."""
        return content_hash(self._payload())

    def render(self) -> str:
        """Deterministic plain-text summary (the migration report a migration ADR cites)."""
        lines = [
            f"migration report: {self.target.migration_id}",
            f"source: {self.target.source}",
            f"target: {self.target.target}",
            f"verdict: {self.verdict.upper()}",
            f"golden: {self.golden_name} ({self.golden_record_hash})",
            f"tolerance: {self.target.tolerance}",
            f"excluded outputs: {', '.join(self.target.excluded_outputs) or '<none>'}",
        ]
        for item in self.conformance:
            lines.append(
                f"conformance {item.candidate}: {'PASS' if item.passed else 'FAIL'} "
                f"({len(item.checks)} checks, {len(item.failures)} failures)"
            )
        lines.append(f"golden differences: {len(self.golden_diff.differences)}")
        lines.append(f"rollback: {self.rollback.verdict.value}")
        lines.extend(f"failure: {reason}" for reason in self.failures)
        lines.extend(f"limitation: {text}" for text in self.target.limitations)
        return "\n".join(lines) + "\n"


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_migration_report(report: MigrationReport, directory: Path) -> Path:
    """Write ``<report_hash>.json`` once; an identical re-save is a no-op, never an overwrite."""
    if not isinstance(report, MigrationReport):
        raise GoldenError("save_migration_report needs a MigrationReport")
    data = canonical_json(report._payload()).encode("utf-8")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report.report_hash}.json"
    if target.exists():
        if target.read_bytes() != data:
            raise GoldenError(f"{target.name}: a different file already sits at this address")
        return target
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            if target.read_bytes() != data:
                raise GoldenError(f"{target.name}: a different file already sits here") from None
            return target
        _fsync_directory(directory)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _keys(raw: object, expected: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != expected:
        raise GoldenError(f"{what}: not a migration report")
    return raw


def _decimal(value: object, what: str) -> Decimal:
    if not isinstance(value, str):
        raise GoldenError(f"{what}: not a decimal")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise GoldenError(f"{what}: not a decimal") from None


def _strings(value: object, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GoldenError(f"{what}: not a list of strings")
    return tuple(value)


def _rebuild(raw: dict[str, Any], name: str) -> MigrationReport:
    target_raw = _keys(raw["target"], _TARGET_KEYS, name)
    target = MigrationTarget(
        migration_id=target_raw["migration_id"],
        source=target_raw["source"],
        target=target_raw["target"],
        tolerance=_decimal(target_raw["tolerance"], name),
        excluded_outputs=_strings(target_raw["excluded_outputs"], name),
        scope=_strings(target_raw["scope"], name),
        out_of_scope=_strings(target_raw["out_of_scope"], name),
        limitations=_strings(target_raw["limitations"], name),
    )
    if target.target_hash != target_raw["target_hash"]:
        raise GoldenError(f"{name}: target_hash does not match the target")
    golden = _keys(raw["golden"], _GOLDEN_KEYS, name)
    conformance: list[ConformanceReport] = []
    if not isinstance(raw["conformance"], list):
        raise GoldenError(f"{name}: not a migration report")
    for item in raw["conformance"]:
        entry = _keys(item, _CONFORMANCE_KEYS, name)
        failures = entry["failures"]
        if not isinstance(failures, list) or not all(
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(part, str) for part in pair)
            for pair in failures
        ):
            raise GoldenError(f"{name}: conformance failures are malformed")
        report = ConformanceReport(
            candidate=entry["candidate"],
            checks=_strings(entry["checks"], name),
            failures=tuple((pair[0], pair[1]) for pair in failures),
        )
        if report.passed is not entry["passed"]:
            raise GoldenError(f"{name}: a conformance verdict does not match its failures")
        conformance.append(report)
    diff_raw = _keys(raw["golden_diff"], _DIFF_KEYS, name)
    raw_differences = diff_raw["differences"]
    if not isinstance(raw_differences, dict):
        raise GoldenError(f"{name}: golden differences are malformed")
    differences: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for key, pair in raw_differences.items():
        if not isinstance(pair, list) or len(pair) != 2:
            raise GoldenError(f"{name}: golden difference {key!r} is malformed")
        golden_value, rerun_value = (
            None if part is None else _decimal(part, name) for part in pair
        )
        differences[key] = (golden_value, rerun_value)
    diff = GoldenDiff(
        name=diff_raw["name"],
        tolerance=_decimal(diff_raw["tolerance"], name),
        bit_identical=diff_raw["bit_identical"],
        differences=differences,
        golden_hash=diff_raw["golden_hash"],
        rerun_hash=diff_raw["rerun_hash"],
    )
    rollback_raw = _keys(raw["rollback"], _ROLLBACK_KEYS, name)
    try:
        verdict = RollbackVerdict(rollback_raw["verdict"])
    except ValueError:
        raise GoldenError(f"{name}: unknown rollback verdict") from None
    evidence = RollbackEvidence(
        migration_id=rollback_raw["migration_id"],
        golden_name=rollback_raw["golden_name"],
        golden_record_hash=rollback_raw["golden_record_hash"],
        golden_hash=rollback_raw["golden_hash"],
        migrated_hash=rollback_raw["migrated_hash"],
        migrated_tolerance=_decimal(rollback_raw["migrated_tolerance"], name),
        migrated_passed=rollback_raw["migrated_passed"],
        rolled_back_hash=rollback_raw["rolled_back_hash"],
        verdict=verdict,
        residual_differences=_strings(rollback_raw["residual_differences"], name),
    )
    if evidence.evidence_hash != rollback_raw["evidence_hash"]:
        raise GoldenError(f"{name}: evidence_hash does not match the rollback evidence")
    return MigrationReport(
        target=target,
        golden_name=golden["name"],
        golden_record_hash=golden["record_hash"],
        conformance=tuple(conformance),
        golden_diff=diff,
        rollback=evidence,
    )


def load_migration_report(directory: Path, report_hash: str) -> MigrationReport:
    """Load and verify a saved report; any tampering raises ``GoldenError``."""
    if not isinstance(report_hash, str) or not _HEX64.fullmatch(report_hash):
        raise GoldenError(f"not a migration report hash: {report_hash!r}")
    path = Path(directory) / f"{report_hash}.json"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GoldenError(f"{path.name}: unreadable migration report: {exc}") from None
    if hashlib.sha256(data).hexdigest() != report_hash:
        raise GoldenError(f"{path.name}: the file does not hash to its name")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GoldenError(f"{path.name}: not JSON: {exc}") from None
    raw = _keys(raw, _REPORT_KEYS, path.name)
    if raw["format"] != _FORMAT or raw["schema_version"] != _SCHEMA_VERSION:
        raise GoldenError(f"{path.name}: not a migration report")
    try:
        report = _rebuild(raw, path.name)
    except (KeyError, TypeError, AttributeError) as exc:
        raise GoldenError(f"{path.name}: malformed migration report: {exc!r}") from None
    if raw["verdict"] != report.verdict or raw["failures"] != list(report.failures):
        raise GoldenError(f"{path.name}: the stored verdict does not match the evidence")
    if report.report_hash != report_hash:
        raise GoldenError(f"{path.name}: not in canonical form")
    return report
