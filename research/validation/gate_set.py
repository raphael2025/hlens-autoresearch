"""ADR-0086 decision 1: the gate set a validation report must carry, by pipeline version.

07-validation.md §2.1 named the gap this module closes: "尚未实现：门集完整性（Profile 要求哪些门
尚无已接受规则）". ADR-0086 decision 1 defines it:

- **stage, not literal gate id, granularity.** A pipeline version's *stages* (``G0`` … ``G5``) are
  the part of the gate set that is fixed by the pipeline code alone. The literal gate ids inside a
  stage are not: how many a report carries depends on the bound Profile instance
  (``G2.cost_stress.<i>`` / ``.cost_report.<i>`` — one pair per ``cost_stress.stress_multipliers`` /
  ``.reported_only_multipliers`` entry — ``G2.market_benchmark(.<rule>)?`` / ``G2.inverse_control`` —
  only when the Profile calls for them, ADR-0060, already checked by ``research.promotion`` itself —
  the multi-seed ``G1.shuffle_control.seed.<n>`` / ``G1.shift_control.seed.<n>`` extras, ADR-0041's
  per-check G4 items) and on what happened while the pipeline ran (an isolated G4 check that raised
  is reported under ``<prefix>.check_error`` instead of its normal gates; no robustness input at all
  collapses every G4 check to the single ``G4.robustness_input`` gate; a consumed sealed evaluation
  that produced no statistic is ``G5.oos_evaluation`` instead of the two ordinary G5 statistic
  gates). Every one of these variants keeps its gate under the *same stage prefix*
  (``gate_id.split(".", 1)[0]``), so stage membership is the invariant a pipeline version can
  promise without a Profile instance in hand. Finer, Profile-specific gate completeness (the ADR-0060
  benchmark items, the ADR-0013 threshold binding) stays a promotion-time check that does hold a
  Profile instance (``research.validation.verification``) — it is not part of the version-level
  required set this module defines.

- **mode from the report's own gates, not a declared field.** No domain field records which "mode"
  (07-validation.md §2.1: "样本内模式" / "封存 OOS 模式") a report is in (H1: no contract change);
  it is read off the stages the report's own gates show, the same way
  ``research.validation.report.promotion_blocked_reason`` reads "did this report reach G5" off the
  gates rather than a flag. Three shapes occur in this codebase (``research/strategies/validation.py``
  builds a G0 – G4 report through ``run_validation``; ``research/loop`` — out of this module's scope —
  builds a **separate** G5-only report through ``run_sealed_oos``/``sealed_oos_without_result``, the
  shape ``research.promotion``'s own evidence chain and ``tests/promotion/fixtures.py::toy_evidence``
  already rely on for "sealed_oos_passed"; a report could in principle carry both at once):

  | report's own stages | mode | required |
  |---|---|---|
  | any of G0 – G4, no G5 | in-sample | G0 – G4 (07-validation.md §2.1 "样本内模式") |
  | any of G0 – G4, and G5 | sealed OOS | G0 – G4 + G5 ("封存 OOS 模式：在样本内门集基础上加上 G5") |
  | G5 only | sealed OOS, standalone | G5 (the report is already exactly what it claims to be) |
  | neither | unscoped | none (only the "unknown stage" check can fire) |

  The third row is not literally one of the ADR's two named modes; without it this module would
  reject every sealed-OOS report the current pipeline (and ``research.promotion``'s own tested happy
  path) actually produces, which decision 1 does not intend ("报告不能再漏门蒙混晋升", not "a report
  must repeat evidence another report already carries"). It is documented here rather than silently
  assumed; see ``docs/architecture/07-validation.md`` §2.1 and the PM report of this change.

- **pipeline version from the report's own field.** ``ValidationReport.constitution_version`` is
  what decision 1 means by "报告按它自己记录的流水线版本核验": there is no separate pipeline-version
  field (H1) and Constitution / pipeline code have always moved together in this codebase (每个实验
  绑定 Constitution 版本与 Profile 版本，07-validation.md §1). A version this module has no row for
  is unregistered — fail closed, never a silent pass (module-common.md: no default value). Decision
  1's "旧报告不追溯" is what the per-version rows give: a future stricter row does not reach into an
  older version's already-registered row.

``_REQUIRED_STAGES`` has one row today: ``"0.2.0-draft"`` — the constitution version every existing
caller records (``tests.factories``, and every ``ValidationContext`` built through
``research.validation`` fixtures / ``ExperimentMetadata``); there has been exactly one pipeline
implementation so far. Raphael's Constitution v1.0.0 freeze (docs/research/constitution.md §"版本")
is not yet the ``constitution_version`` any caller in this codebase records — adding its row now
could not be exercised or tested against a real report, so it is a FOLLOW-UP (the PM report of this
change), not done here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "GateSetResult",
    "gate_set_completeness",
    "stage_of",
]

#: Every stage id this pipeline can ever emit, in flow order (07-validation.md §2).
_ALL_STAGES: Final[tuple[str, ...]] = ("G0", "G1", "G2", "G3", "G4", "G5")

#: The in-sample stage set (07-validation.md §2.1 "样本内模式"): G5 is never part of it.
_IN_SAMPLE_STAGES: Final[frozenset[str]] = frozenset(_ALL_STAGES[:5])
assert "G5" not in _IN_SAMPLE_STAGES

#: pipeline version (``ValidationReport.constitution_version``) -> its known in-sample stage set
#: (module docs). A version not listed here is unregistered: ``gate_set_completeness`` reports it,
#: never silently passes it.
_REQUIRED_STAGES: Final[dict[str, frozenset[str]]] = {
    "0.2.0-draft": _IN_SAMPLE_STAGES,
}

Mode = Literal["in_sample", "sealed_oos", "sealed_oos_only", "unscoped"]


def stage_of(gate_id: str) -> str:
    """The stage prefix of ``gate_id`` (``gate_id.split(".", 1)[0]``)."""
    return gate_id.split(".", 1)[0]


@dataclass(frozen=True)
class GateSetResult:
    """The outcome of checking one report's stage set against its own pipeline version (module
    docs). ``registered`` is ``False`` exactly when ``pipeline_version`` has no row in
    ``_REQUIRED_STAGES``; ``required`` / ``missing`` are then empty (there is nothing to check
    against) and ``ok`` is ``False`` (an unregistered version is never silently complete)."""

    pipeline_version: str
    registered: bool
    mode: Mode
    required: frozenset[str]
    missing: frozenset[str]
    unknown: frozenset[str]

    @property
    def ok(self) -> bool:
        return self.registered and not self.missing and not self.unknown


def gate_set_completeness(pipeline_version: str, gate_ids: Iterable[str]) -> GateSetResult:
    """Whether ``gate_ids`` (a report's ``gate_id`` values) cover every stage its own
    ``pipeline_version`` requires for the mode its own gates show, and name no stage this pipeline
    version does not know (module docs). Pure; never raises — an unregistered version or an
    incomplete / unknown stage set is reported as data, the same way
    ``research.validation.verification.verify_report`` never raises for a discrepancy."""
    stages = {stage_of(gate_id) for gate_id in gate_ids}
    known = _REQUIRED_STAGES.get(pipeline_version)
    if known is None:
        sealed_oos = "G5" in stages
        return GateSetResult(
            pipeline_version=pipeline_version,
            registered=False,
            mode="sealed_oos" if sealed_oos else "in_sample" if stages else "unscoped",
            required=frozenset(),
            missing=frozenset(),
            unknown=frozenset(),
        )
    all_known = known | {"G5"}
    in_sample_present = bool(stages & known)
    g5_present = "G5" in stages
    mode: Mode
    if in_sample_present and g5_present:
        mode, required = "sealed_oos", known | {"G5"}
    elif in_sample_present:
        mode, required = "in_sample", known
    elif g5_present:
        mode, required = "sealed_oos_only", frozenset({"G5"})
    else:
        mode, required = "unscoped", frozenset()
    return GateSetResult(
        pipeline_version=pipeline_version,
        registered=True,
        mode=mode,
        required=frozenset(required),
        missing=frozenset(required - stages),
        unknown=frozenset(stages - all_known),
    )
