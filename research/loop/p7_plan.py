"""The Research Loop candidate of an admitted, compiled P7 plan (ADR-0100 item 1, ADR-0103 D3).

The loop gains no new stage. A compiled plan whose root is a composed ``StrategySpec``
(conditioning / ensemble / negation) becomes an ordinary ``StrategyCandidate`` carrying the plan's
``hlens.p7.plan@1.0.0`` record (``StrategyCandidate.plan_record``) — but only **after** the plan
was admitted: ``p7_strategy_candidates`` requires the ``CommittedAdmission`` (ADR-0073 PREPARE /
COMMIT) of exactly this plan in the current round. The one caller is the admission composition
``research.loop.p7_admission`` (``LoopWiring.p7_plans``), which adds the candidate to the loop's
catalog after the COMMIT; a candidate built any other way — e.g. appended to
``LoopWiring.strategies`` — is refused by the composition (ADR-0103 D3 closes that bypass).

``p7_strategy_candidates`` refuses (``PlanCompileRefused``) unless the same ``P7ExecutionSwitch``
that compiled the plan is passed enabled, the plan is runnable and has no cross-sectional node
(``cross_sectional_loop_unsupported``: the loop is single-instrument, ADR-0103 D4), the root is a
strategy whose Provider built by ``CompiledPlan.build_providers`` serves the root spec's exact hash,
and the admission is this plan's COMMIT of ``round_index``. A plan whose root is a feature or
event is not a strategy and is refused here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.contracts.strategy import RiskProvider
from core.domain.specs import RiskPolicy, StrategySpec
from research.hypotheses.p7_binding import P7PlanRecord
from research.hypotheses.p7_evidence import outputs_evidence
from research.hypotheses.typed_plan import CROSS_SECTIONAL_TRANSFORMS, PlanOperator
from research.hypotheses.typed_plan_audit import CommittedAdmission, PlanAdmissionEvidence
from research.hypotheses.typed_plan_compiler import (
    CompiledPlan,
    P7ExecutionSwitch,
    PlanCompileRefused,
)
from research.strategies.pipeline import StrategyCandidate

__all__ = [
    "CROSS_SECTIONAL_LOOP_UNSUPPORTED",
    "has_cross_sectional_node",
    "p7_strategy_candidates",
]

#: The refusal code of a plan with a cross-sectional node entering the single-instrument loop.
CROSS_SECTIONAL_LOOP_UNSUPPORTED = "cross_sectional_loop_unsupported"


def has_cross_sectional_node(compiled_or_plan: Any) -> str | None:
    """The first cross-sectional (``rank_cs`` / ``quantile_cs``) node id, or ``None``."""
    plan = getattr(compiled_or_plan, "plan", compiled_or_plan)
    for node in plan.nodes:
        if (
            node.operator is PlanOperator.TRANSFORMATION
            and node.parameters.get("transform") in CROSS_SECTIONAL_TRANSFORMS
        ):
            return str(node.node_id)
    return None


def _require_admission(compiled: CompiledPlan, admission: object, round_index: object) -> None:
    """``admission`` is this plan's COMMIT in round ``round_index`` (ADR-0103 D3)."""
    root = compiled.plan.root
    if type(admission) is not CommittedAdmission:
        raise PlanCompileRefused(
            "admission_missing", root, "a P7 candidate needs the plan's CommittedAdmission"
        )
    prepared = admission.prepare
    if type(round_index) is not int or prepared.round.round_index != round_index:
        raise PlanCompileRefused(
            "admission_not_this_round",
            root,
            f"the COMMIT belongs to round {prepared.round.round_index}, not {round_index!r}",
        )
    plan_evidence = PlanAdmissionEvidence.from_data(compiled.plan.payload())
    if prepared.plan.content_hash != plan_evidence.content_hash:
        raise PlanCompileRefused("admission_plan_mismatch", root, "the COMMIT is another plan")
    if prepared.compiler.content_hash != compiled.compiler_evidence().content_hash or [
        item.content_hash for item in prepared.outputs
    ] != [item.content_hash for item in outputs_evidence(compiled)]:
        raise PlanCompileRefused(
            "admission_evidence_mismatch",
            root,
            "the COMMIT's compiler / output evidence is not this compiled plan's",
        )


def p7_strategy_candidates(
    compiled: CompiledPlan,
    providers: Mapping[str, Any],
    *,
    switch: P7ExecutionSwitch | None = None,
    admission: CommittedAdmission | None = None,
    round_index: int | None = None,
    hypothesis_family_id: str,
    risk_policy: RiskPolicy | None = None,
    risk: RiskProvider | None = None,
) -> tuple[StrategyCandidate, ...]:
    """The loop candidate for an admitted plan's strategy root, or a precise refusal."""
    if not isinstance(compiled, CompiledPlan):
        raise TypeError("compiled must be a CompiledPlan")
    root = compiled.plan.root
    if switch is None or type(switch) is not P7ExecutionSwitch or switch.enabled is not True:
        raise PlanCompileRefused(
            "execution_disabled", root, "P7 plans enter the loop only with the switch enabled"
        )
    if not compiled.runnable:
        raise PlanCompileRefused("not_runnable", root, "the compiled plan is not runnable")
    cross_sectional = has_cross_sectional_node(compiled)
    if cross_sectional is not None:
        raise PlanCompileRefused(
            CROSS_SECTIONAL_LOOP_UNSUPPORTED,
            cross_sectional,
            "the research loop is single-instrument; a cross-sectional plan needs its own ADR",
        )
    node = compiled.root
    if type(node.spec) is not StrategySpec:
        raise PlanCompileRefused(
            "root_not_strategy", root, "only a strategy root can enter the loop as a candidate"
        )
    provider = providers.get(root)
    if provider is None or not provider.descriptor.supports(
        node.spec.ref, node.spec.content_hash()
    ):
        raise PlanCompileRefused(
            "root_provider_mismatch", root, "the root Provider does not serve the root spec"
        )
    if not isinstance(hypothesis_family_id, str) or not hypothesis_family_id.strip():
        raise PlanCompileRefused(
            "invalid_family", root, "hypothesis_family_id must be explicit, non-blank text"
        )
    _require_admission(compiled, admission, round_index)
    return (
        StrategyCandidate(
            spec=node.spec,
            strategy=provider,
            hypothesis_family_id=hypothesis_family_id,
            risk_policy=risk_policy,
            risk=risk,
            plan_record=P7PlanRecord.from_compiled(compiled),
        ),
    )
