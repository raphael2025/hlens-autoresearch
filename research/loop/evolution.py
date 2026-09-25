"""Evolution stage of the loop (optional; Phase 12 operators, ADR-0045; W2 wiring of ADR-0049).

When due (round ``r > 0`` with ``r % every_rounds == 0``) the stage takes the best earlier
validated trials that were **not refuted** (verdict PASS or INCONCLUSIVE; ranked PASS first, then
by the G3 adjusted p-value, then by round and name) and, for up to ``parents_per_round`` parents
whose spec was never evolved before, applies ``mutate``: the first declared parameter value (params
in name order, values in declared order) that differs from both the parent's default and the
parent's tested point and whose resulting point was never tested for that strategy name.

Every offspring:

- is a **new version** (``require_new_version(parent, child)`` must accept it; the catalog refuses
  the same ref with other content) with ``lineage`` naming its parent, and the whole
  ``LineageGraph`` of the loop's specs must trace it back (no missing strategy link);
- is registered in the ``TrialLedger`` as its own hypothesis (``origin = combination``, parameter
  point pinned in its conditions) **before** it runs, so it counts as a trial of the family;
- enters the lifecycle at ``IDEA`` → ``CANDIDATE`` and is experimented and validated in **this**
  round on this round's (new) data like any other hypothesis — it never inherits its parent's
  verdict (the parent's report id is only cited as the reason it was chosen).

The stage declares its trials before running (budget-checked by the scheduler); a round in which it
is not due declares and spends nothing. ``combine`` and ``retire`` are not used by the loop:
combining two variants of one strategy always clashes on parameters, and retirement applies to
ACTIVE strategies, which the loop can never reach.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.worker.loop import RoundContext, StageResult, StageUsage
from core.contracts.strategy import StrategyProvider
from core.domain.base import Kind
from core.domain.research import Hypothesis, HypothesisOrigin, Verdict
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleState
from research.evolution import (
    EvolutionError,
    LineageGraph,
    Offspring,
    mutate,
    require_new_version,
)
from research.loop.memory import ResearchMemory
from research.loop.segment import decimal_text
from research.loop.trials import ValidationOutcome
from research.strategies.pipeline import StrategyCandidate

__all__ = ["EvolutionPlan", "EvolutionStage"]

Scalar = str | int | float | bool


@dataclass(frozen=True)
class EvolutionPlan:
    """When and how much to evolve (no defaults). ``provider_for`` serves an offspring spec."""

    every_rounds: int
    parents_per_round: int
    compute_seconds: Decimal
    provider_for: Callable[[StrategySpec], StrategyProvider]
    minimum_meaningful_effect: str

    def __post_init__(self) -> None:
        for name in ("every_rounds", "parents_per_round"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive int")


@dataclass(frozen=True)
class _Child:
    parent: ValidationOutcome
    offspring: Offspring
    point: Mapping[str, Scalar]
    hypothesis: Hypothesis


def _same(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _text(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _point_key(name: str, point: Mapping[str, Scalar]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(sorted((k, f"{type(v).__name__}:{v}") for k, v in point.items()))


def _rank(result: ValidationOutcome) -> tuple[int, float, int, str]:
    p = result.gate_value("G3.adjusted_p_value")
    return (
        0 if result.verdict is Verdict.PASS else 1,
        float("inf") if p is None else p,
        result.round_index,
        str(result.outcome.hypothesis.ref),
    )


class EvolutionStage:
    name = "evolution"

    def __init__(self, memory: ResearchMemory, plan: EvolutionPlan) -> None:
        self._memory = memory
        self._plan_config = plan

    def _due(self, ctx: RoundContext) -> bool:
        every = self._plan_config.every_rounds
        return ctx.round_index > 0 and ctx.round_index % every == 0

    def _hypothesis(
        self, parent: ValidationOutcome, off: Offspring, point: Mapping[str, Scalar]
    ) -> Hypothesis:
        child, source = off.spec, parent.outcome.hypothesis
        space = child.param_search_space
        return Hypothesis(
            name=f"h_{child.name}_v{child.version.replace('.', '_')}",
            version="1.0.0",
            family_id=source.family_id,
            statement=(
                f"{off.operator} of {off.parents[0]} gives {child.ref}; it is tested afresh at "
                f"{dict(sorted(point.items()))} and inherits no verdict"
            ),
            conditions=(
                f"strategy = {child.name}@{child.version}",
                *(f"param {k} = {_text(point[k])}" for k in sorted(point) if k in space),
            ),
            expected_direction="higher",
            minimum_meaningful_effect=self._plan_config.minimum_meaningful_effect,
            origin=HypothesisOrigin.COMBINATION,
            origin_refs=(off.parents[0], child.ref, source.ref),
        )

    def _children(self, ctx: RoundContext) -> tuple[_Child, ...]:
        if not self._due(ctx):
            return ()
        memory = self._memory
        evolved = {str(record["parent"]) for record in memory.offspring}
        tested = {
            _point_key(o.candidate.spec.name, dict(o.run.repro.params))
            for o in memory.trials
            if o.candidate is not None
        }
        eligible = sorted(
            (
                v
                for v in memory.validations
                if v.verdict in {Verdict.PASS, Verdict.INCONCLUSIVE}
                and v.outcome.candidate is not None
            ),
            key=_rank,
        )
        children: list[_Child] = []
        for parent in eligible:
            if len(children) >= self._plan_config.parents_per_round:
                break
            assert parent.outcome.candidate is not None
            spec = parent.outcome.candidate.spec
            if str(spec.ref) in evolved:
                continue
            child = self._mutation(parent, spec, tested)
            if child is not None:
                children.append(child)
                evolved.add(str(spec.ref))
                tested.add(_point_key(child.offspring.spec.name, child.point))
        return tuple(children)

    def _mutation(
        self,
        parent: ValidationOutcome,
        spec: StrategySpec,
        tested: set[tuple[str, tuple[tuple[str, str], ...]]],
    ) -> _Child | None:
        point: dict[str, Scalar] = dict(parent.outcome.run.repro.params)
        for param in sorted(spec.param_search_space):
            for value in spec.param_search_space[param]:
                if _same(value, spec.params.get(param)) or _same(value, point.get(param)):
                    continue
                new_point = {**point, param: value}
                if _point_key(spec.name, new_point) in tested:
                    continue
                try:
                    off = mutate(spec, param, value)
                    require_new_version(spec, off.spec)
                except EvolutionError:
                    continue
                known = self._memory.strategies.get(str(off.spec.ref))
                if known is not None:
                    continue  # that version exists already: never overwrite it
                return _Child(parent, off, new_point, self._hypothesis(parent, off, new_point))
        return None

    def estimate(self, ctx: RoundContext) -> StageUsage:
        if not self._due(ctx):
            return StageUsage()
        return StageUsage(
            trials=len(self._children(ctx)), compute_seconds=self._plan_config.compute_seconds
        )

    def run(self, ctx: RoundContext) -> StageResult:
        if not self._due(ctx):
            return StageResult({"due": False, "offspring": []}, StageUsage(), {"registered": ()})
        memory, children = self._memory, self._children(ctx)
        registered: list[Hypothesis] = []
        rows: list[dict[str, Any]] = []
        for child in children:
            parent_candidate = child.parent.outcome.candidate
            assert parent_candidate is not None
            spec = child.offspring.spec
            memory.ledger.register(child.hypothesis)  # pre-registered before it runs
            memory.add_strategy(
                StrategyCandidate(
                    spec=spec,
                    strategy=self._plan_config.provider_for(spec),
                    hypothesis_family_id=parent_candidate.hypothesis_family_id,
                    risk_policy=parent_candidate.risk_policy,
                    risk=parent_candidate.risk,
                )
            )
            for known in (parent_candidate.spec, spec):
                if all(s.ref != known.ref for s in memory.lineage):
                    memory.lineage.append(known)
            graph = LineageGraph(memory.lineage)
            missing = [r for r in graph.missing() if r.kind is Kind.STRATEGY]
            if missing:
                raise EvolutionError(f"{spec.ref} has untraceable strategy ancestry: {missing}")
            report_id = None if child.parent.report is None else child.parent.report.report_id
            ctx.open_subject(child.hypothesis.ref)
            ctx.advance(
                child.hypothesis.ref,
                LifecycleState.CANDIDATE,
                reason="evolution offspring pre-registered; to be validated afresh",
                evidence=(
                    f"hypothesis:{child.hypothesis.ref}#{child.hypothesis.content_hash()}",
                    f"evolution:{child.offspring.operator}:{child.offspring.parents[0]}"
                    f"->{spec.ref}",
                    f"parent_report:{report_id}",
                ),
            )
            row = {
                "parent": str(parent_candidate.spec.ref),
                "parent_hypothesis": str(child.parent.outcome.hypothesis.ref),
                "parent_report_id": report_id,
                "parent_verdict": None
                if child.parent.verdict is None
                else child.parent.verdict.value,
                "child": str(spec.ref),
                "child_spec_hash": spec.content_hash(),
                "operator": child.offspring.operator,
                "hypothesis": str(child.hypothesis.ref),
                "hypothesis_hash": child.hypothesis.content_hash(),
                "point": {k: _param(v) for k, v in sorted(child.point.items())},
                "ancestors": [str(r) for r in graph.ancestors(spec.ref)],
                "lifecycle_entry": child.offspring.lifecycle_entry.value,
            }
            memory.offspring.append({"round": ctx.round_index, **row})
            rows.append(row)
            registered.append(child.hypothesis)
        summary = {
            "due": True,
            "offspring": rows,
            "family_trials": {
                family: memory.ledger.trials(family)
                for family in sorted({h.family_id for h in registered})
            },
        }
        return StageResult(
            summary,
            StageUsage(trials=len(registered), compute_seconds=self._plan_config.compute_seconds),
            {"registered": tuple(registered)},
        )


def _param(value: Scalar) -> Any:
    if isinstance(value, float):
        return decimal_text(value)
    return value
