"""Research Loop wiring of a compiled P7 plan (ADR-0100 item 1), default OFF.

The loop gains no new stage, field or trial rule. A compiled plan whose root is a composed
``StrategySpec`` (conditioning / ensemble / negation) enters the loop only as an ordinary
``StrategyCandidate`` through the existing ``LoopWiring.strategies`` extension point: the caller
appends the returned candidate to ``LoopWiring.strategies``. From there every existing rule
applies unchanged — the candidate is registered and counted as a trial by the hypothesis /
experiment stages exactly like any library strategy, and its spec hash enters the loop settings
fingerprint (``compose.settings_fingerprint``).

``p7_strategy_candidates`` refuses (``PlanCompileRefused``) unless the same
``P7ExecutionSwitch`` that compiled the plan is passed enabled, the plan is runnable, and the
root Provider built by ``CompiledPlan.build_providers`` serves the root spec's exact hash. A plan
whose root is a feature or event is not a strategy and is refused here (feature / event outputs
are consumed by the strategies that reference them, not by the loop directly).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.contracts.strategy import RiskProvider
from core.domain.specs import RiskPolicy, StrategySpec
from research.hypotheses.typed_plan_compiler import (
    CompiledPlan,
    P7ExecutionSwitch,
    PlanCompileRefused,
)
from research.strategies.pipeline import StrategyCandidate

__all__ = ["p7_strategy_candidates"]


def p7_strategy_candidates(
    compiled: CompiledPlan,
    providers: Mapping[str, Any],
    *,
    switch: P7ExecutionSwitch | None = None,
    hypothesis_family_id: str,
    risk_policy: RiskPolicy | None = None,
    risk: RiskProvider | None = None,
) -> tuple[StrategyCandidate, ...]:
    """The loop candidate for a compiled plan's strategy root, or a precise refusal."""
    if not isinstance(compiled, CompiledPlan):
        raise TypeError("compiled must be a CompiledPlan")
    root = compiled.plan.root
    if switch is None or type(switch) is not P7ExecutionSwitch or switch.enabled is not True:
        raise PlanCompileRefused(
            "execution_disabled", root, "P7 plans enter the loop only with the switch enabled"
        )
    if not compiled.runnable:
        raise PlanCompileRefused("not_runnable", root, "the compiled plan is not runnable")
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
    return (
        StrategyCandidate(
            spec=node.spec,
            strategy=provider,
            hypothesis_family_id=hypothesis_family_id,
            risk_policy=risk_policy,
            risk=risk,
        ),
    )
