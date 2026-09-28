"""Verify a ``ValidationReport``'s thresholds against the Profile instance it binds (ADR-0013).

07-validation.md §2.1 / ADR-0013: the contract layer checks only the shape of a ``GateResult``
(``threshold`` and ``threshold_source`` paired); **whether ``threshold_source`` really names a field
of the bound Profile version and whether its value equals ``threshold`` can only be checked by a
service holding the Profile instance**. This module is that check for the research plane. It is a
pure function: no I/O, no default, no threshold of its own, and it never changes a report or a
verdict — it lists discrepancies; the consumer (``research.promotion``) refuses on any.

``verify_report(report, profile)`` checks, in this order:

1. **binding** — ``report.validation_profile_hash == profile.content_hash()`` and the report's
   ``validation_profile`` is the Profile's ref (``profile_not_bound``);
2. for every gate **with a threshold**:

   - a Profile source (not ``param:``) must resolve through ``gates.threshold(profile, source)``
     (``source_not_in_profile``), be the canonical source that call returns — the ``*_exact``
     sibling when the Profile carries it, ADR-0052 §1 (``source_not_canonical``) — and carry exactly
     the Profile's value: ``threshold`` equals the Profile's float and a recorded
     ``threshold_exact`` equals its exact value (``threshold_mismatch`` /
     ``threshold_exact_mismatch``; an exact threshold recorded where the Profile has none is a
     mismatch too);
   - an explicit ``param:<name>`` source is legitimate only for a rule the Profile does
     **not** carry: for the ADR-0052 §2 G4 thresholds (``g4.PROFILE_SOURCED_THRESHOLDS``), a
     ``param:`` value under a Profile that carries the field is ``param_overrides_profile`` (C-A4;
     the pipeline refuses it at run time with ``ExplicitParamRefused``, so such a report was not
     produced by the pipeline under this Profile); other ``param:`` names have no Profile field
     and are accepted;
   - the metric names its comparison (``[>=]`` / ``[<=]``, ``gates.compare_gate``) —
     ``direction_missing`` otherwise — and the recorded verdict agrees with it: a ``PASS`` gate's
     value satisfies the comparison and a ``FAIL`` gate's value does not
     (``verdict_inconsistent``). The comparison is exact (``value_exact`` vs
     ``threshold_exact``) when the gate carries exact values, else on the floats — the same
     representation the pipeline compared. ``INCONCLUSIVE`` is never contradicted: an
     inconclusive band, a too-small sample (``FAIL`` → ``INCONCLUSIVE``) or an aggregation may
     make any value inconclusive.

Gates without a threshold (structural flags, reported-only items, missing-field /
configuration-missing gates) have nothing to compare and are not checked here.

**Gate-set completeness (ADR-0086 decision 1, 2026-09-28).** ``verify_report`` also checks the
report's gate id set against ``research.validation.gate_set.gate_set_completeness`` (see that
module for what "required" means: stage granularity, not literal gate ids — those still depend on
the bound Profile and are not re-checked here): a stage its own pipeline version
(``ValidationReport.constitution_version``) requires and the report has no gate for
(``gate_stage_missing``), a stage no known pipeline version emits
(``gate_stage_unknown``), or a ``constitution_version`` this module has no required-set row for
(``pipeline_version_unregistered``, report-level, ``gate_id=None``). ``ReportVerification.ok`` is
unaffected — it is still "no discrepancy of any kind" — but
``.threshold_discrepancies`` / ``.gate_set_discrepancies`` split the two families apart, because
``research.promotion`` rejects them under two different reasons, in order
(``report_threshold_mismatch`` then ``report_gate_set_incomplete``).

**Still not checked (no accepted rule defines it):** whether ``value`` was really computed by
``metric`` (that is a re-run: ``G0.reproducibility`` / ``retro_audit``).

Status: CODE_COMPLETE / DEBUG_PENDING (MOD-VALID, 2026-09-28; ADR-0086 decision 1, 2026-09-28). No
contract, Schema, Profile number or gate rule changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, ValidationReport, Verdict
from research.validation.g4 import PROFILE_SOURCED_THRESHOLDS
from research.validation.gate_set import gate_set_completeness
from research.validation.gates import (
    PARAM_SOURCE_PREFIX,
    Direction,
    profile_has,
    threshold,
)

__all__ = [
    "GATE_SET_PROBLEMS",
    "GateDiscrepancy",
    "ReportDiscrepancy",
    "ReportVerification",
    "verify_report",
]

#: ``param:<name>`` → the ADR-0052 §2 Profile path that replaces that explicit parameter.
_PARAM_PROFILE_PATHS: Final = {name: path for path, name in PROFILE_SOURCED_THRESHOLDS}


class ReportDiscrepancy(StrEnum):
    """What is wrong between a report and the Profile it binds (module docs)."""

    PROFILE_NOT_BOUND = "profile_not_bound"
    SOURCE_NOT_IN_PROFILE = "source_not_in_profile"
    SOURCE_NOT_CANONICAL = "source_not_canonical"
    THRESHOLD_MISMATCH = "threshold_mismatch"
    THRESHOLD_EXACT_MISMATCH = "threshold_exact_mismatch"
    PARAM_OVERRIDES_PROFILE = "param_overrides_profile"
    DIRECTION_MISSING = "direction_missing"
    VERDICT_INCONSISTENT = "verdict_inconsistent"
    #: ADR-0086 decision 1 (research.validation.gate_set): the report's own constitution_version
    #: has no registered required gate set.
    PIPELINE_VERSION_UNREGISTERED = "pipeline_version_unregistered"
    #: ADR-0086 decision 1: a stage the report's pipeline version and mode require has no gate.
    GATE_STAGE_MISSING = "gate_stage_missing"
    #: ADR-0086 decision 1: the report carries a gate under a stage its pipeline version never emits.
    GATE_STAGE_UNKNOWN = "gate_stage_unknown"


#: The ADR-0086 decision 1 discrepancies (``research.validation.gate_set``), as opposed to the
#: ADR-0013 threshold-binding discrepancies above: ``research.promotion`` rejects the two under
#: different reasons, in order (module docs).
GATE_SET_PROBLEMS: Final = frozenset(
    {
        ReportDiscrepancy.PIPELINE_VERSION_UNREGISTERED,
        ReportDiscrepancy.GATE_STAGE_MISSING,
        ReportDiscrepancy.GATE_STAGE_UNKNOWN,
    }
)


@dataclass(frozen=True)
class GateDiscrepancy:
    """One discrepancy; ``gate_id`` is ``None`` for the report-level binding check."""

    gate_id: str | None
    problem: ReportDiscrepancy
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"gate_id": self.gate_id, "problem": self.problem.value, "detail": self.detail}


@dataclass(frozen=True)
class ReportVerification:
    """The outcome of ``verify_report``; ``ok`` only without any discrepancy."""

    report_id: str
    profile: str
    profile_hash: str
    gates_checked: int
    discrepancies: tuple[GateDiscrepancy, ...]

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    @property
    def threshold_discrepancies(self) -> tuple[GateDiscrepancy, ...]:
        """The ADR-0013 discrepancies (a Profile binding / threshold / verdict problem): what
        ``research.promotion`` rejects as ``report_threshold_mismatch``."""
        return tuple(d for d in self.discrepancies if d.problem not in GATE_SET_PROBLEMS)

    @property
    def gate_set_discrepancies(self) -> tuple[GateDiscrepancy, ...]:
        """The ADR-0086 decision 1 discrepancies (a missing / unknown stage, or an unregistered
        pipeline version): what ``research.promotion`` rejects as ``report_gate_set_incomplete``,
        checked only after ``threshold_discrepancies`` is empty (module docs)."""
        return tuple(d for d in self.discrepancies if d.problem in GATE_SET_PROBLEMS)

    def to_dict(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "profile": self.profile,
            "profile_hash": self.profile_hash,
            "gates_checked": self.gates_checked,
            "ok": self.ok,
            "discrepancies": [item.to_dict() for item in self.discrepancies],
        }


def _direction(metric: str) -> Direction | None:
    for direction in Direction:
        if metric.endswith(f"[{direction.value}]"):
            return direction
    return None


def _satisfied(gate: GateResult, direction: Direction) -> bool:
    """Whether the gate's value meets its threshold, in the representation it was compared in."""
    value: Decimal | float
    limit: Decimal | float
    if gate.value_exact is not None and gate.threshold_exact is not None:
        value, limit = gate.value_exact, gate.threshold_exact
    else:
        assert gate.threshold is not None  # only thresholded gates reach here
        value, limit = gate.value, gate.threshold
    return value >= limit if direction is Direction.AT_LEAST else value <= limit


def _source_problems(gate: GateResult, profile: ValidationProfile) -> list[GateDiscrepancy]:
    source = gate.threshold_source
    assert source is not None  # paired with threshold by the contract
    gate_id = gate.gate_id
    if source.startswith(PARAM_SOURCE_PREFIX):
        name = source[len(PARAM_SOURCE_PREFIX) :]
        path = _PARAM_PROFILE_PATHS.get(name)
        if path is not None and profile_has(profile, path):
            return [
                GateDiscrepancy(
                    gate_id,
                    ReportDiscrepancy.PARAM_OVERRIDES_PROFILE,
                    f"{source} is recorded although the Profile carries {path} (C-A4)",
                )
            ]
        return []
    try:
        expected = threshold(profile, source)
    except ValueError as exc:  # ProfileFieldMissing included: the path is not in the Profile
        return [GateDiscrepancy(gate_id, ReportDiscrepancy.SOURCE_NOT_IN_PROFILE, str(exc))]
    problems: list[GateDiscrepancy] = []
    if expected.source != source:
        problems.append(
            GateDiscrepancy(
                gate_id,
                ReportDiscrepancy.SOURCE_NOT_CANONICAL,
                f"recorded {source!r}; the Profile's threshold is read from {expected.source!r}",
            )
        )
    if gate.threshold != expected.value:
        problems.append(
            GateDiscrepancy(
                gate_id,
                ReportDiscrepancy.THRESHOLD_MISMATCH,
                f"recorded {gate.threshold!r}; the Profile has {expected.value!r} at {source!r}",
            )
        )
    # A recorded exact threshold must be the Profile's exact value; an absent one is not a
    # discrepancy by itself (the multi-seed aggregate of inconclusive controls records only the
    # float, which ``threshold_mismatch`` has already compared with ``float(exact)``).
    if gate.threshold_exact is not None and gate.threshold_exact != expected.exact:
        problems.append(
            GateDiscrepancy(
                gate_id,
                ReportDiscrepancy.THRESHOLD_EXACT_MISMATCH,
                f"recorded threshold_exact {gate.threshold_exact}; "
                f"the Profile has {expected.exact} at {expected.source!r}",
            )
        )
    return problems


def _verdict_problems(gate: GateResult) -> list[GateDiscrepancy]:
    direction = _direction(gate.metric)
    if direction is None:
        return [
            GateDiscrepancy(
                gate.gate_id,
                ReportDiscrepancy.DIRECTION_MISSING,
                f"metric {gate.metric!r} of a thresholded gate names no comparison",
            )
        ]
    satisfied = _satisfied(gate, direction)
    if (gate.verdict is Verdict.PASS and not satisfied) or (
        gate.verdict is Verdict.FAIL and satisfied
    ):
        return [
            GateDiscrepancy(
                gate.gate_id,
                ReportDiscrepancy.VERDICT_INCONSISTENT,
                f"{gate.verdict.value} recorded, but value {gate.value!r} "
                f"{'meets' if satisfied else 'misses'} threshold {gate.threshold!r} "
                f"({direction.value})",
            )
        ]
    return []


def _gate_set_problems(report: ValidationReport) -> list[GateDiscrepancy]:
    """ADR-0086 decision 1: ``report``'s stage set against its own ``constitution_version``
    (module docs; ``research.validation.gate_set``)."""
    result = gate_set_completeness(
        report.constitution_version, (gate.gate_id for gate in report.gates)
    )
    if not result.registered:
        return [
            GateDiscrepancy(
                None,
                ReportDiscrepancy.PIPELINE_VERSION_UNREGISTERED,
                f"no required gate set is registered for pipeline version "
                f"{report.constitution_version!r} (research.validation.gate_set)",
            )
        ]
    problems = [
        GateDiscrepancy(
            stage,
            ReportDiscrepancy.GATE_STAGE_MISSING,
            f"{result.mode} mode of pipeline version {report.constitution_version!r} requires "
            f"stage {stage}, which this report has no gate for",
        )
        for stage in sorted(result.missing)
    ]
    problems.extend(
        GateDiscrepancy(
            stage,
            ReportDiscrepancy.GATE_STAGE_UNKNOWN,
            f"this report carries a gate under stage {stage}, unknown to pipeline version "
            f"{report.constitution_version!r}",
        )
        for stage in sorted(result.unknown)
    )
    return problems


def verify_report(report: ValidationReport, profile: ValidationProfile) -> ReportVerification:
    """Every discrepancy between ``report`` and ``profile`` (module docs); pure, never raises
    for a discrepancy."""
    if not isinstance(report, ValidationReport):
        raise TypeError("verify_report needs a ValidationReport")
    if not isinstance(profile, ValidationProfile):
        raise TypeError("verify_report needs a ValidationProfile")
    problems: list[GateDiscrepancy] = []
    profile_hash = profile.content_hash()
    if (
        report.validation_profile_hash != profile_hash
        or report.validation_profile.target_identity() != profile.ref.target_identity()
    ):
        problems.append(
            GateDiscrepancy(
                None,
                ReportDiscrepancy.PROFILE_NOT_BOUND,
                f"report binds {report.validation_profile} / {report.validation_profile_hash}; "
                f"the Profile is {profile.ref} / {profile_hash}",
            )
        )
    checked = 0
    for gate in report.gates:
        if gate.threshold is None:
            continue
        checked += 1
        problems.extend(_source_problems(gate, profile))
        problems.extend(_verdict_problems(gate))
    problems.extend(_gate_set_problems(report))
    return ReportVerification(
        report_id=report.report_id,
        profile=str(profile.ref),
        profile_hash=profile_hash,
        gates_checked=checked,
        discrepancies=tuple(problems),
    )
