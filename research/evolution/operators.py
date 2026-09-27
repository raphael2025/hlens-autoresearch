"""Evolution operators over ``StrategySpec`` (roadmap Phase 12; ADR-0045).

Every operator returns a **new** spec version whose ``lineage`` names its parent(s) and which must
be validated again from ``IDEA`` (an ``Offspring`` carries that lifecycle entry point); parents are
never modified. Mutation may only move a parameter inside its declared search space (so the trial
count of Constitution C-T1 covers it). ``require_new_version`` is the guard against in-place
changes of an ACTIVE strategy: any content change without a new version and a lineage link is
refused. Retirement produces the append-only ``RetirementRecord``.

``combine`` is fail closed when parent search spaces, risk policies or applicable instrument
sets conflict (ADR-0069); it never silently chooses one parent's safety boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.domain.base import Ref
from core.domain.execution import ExecutionMode
from core.domain.research import RetirementRecord
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleState

__all__ = ["EvolutionError", "Offspring", "combine", "mutate", "require_new_version", "retire"]


class EvolutionError(ValueError):
    """The operation would break lineage, versioning or the declared search space."""


@dataclass(frozen=True, slots=True)
class Offspring:
    spec: StrategySpec
    parents: tuple[Ref, ...]
    operator: str
    #: A descendant enters the lifecycle anew and is validated again (roadmap P12).
    lifecycle_entry: LifecycleState = LifecycleState.IDEA


def _bump(version: str) -> str:
    major, minor, _patch = (int(part) for part in version.split("-")[0].split("+")[0].split("."))
    return f"{major}.{minor + 1}.0"


def mutate(parent: StrategySpec, param: str, value: str | int | float | bool) -> Offspring:
    space = parent.param_search_space.get(param)
    if space is None:
        raise EvolutionError(f"{param!r} has no declared search space in {parent.ref}")
    if value not in space:
        raise EvolutionError(f"{value!r} is outside the declared search space of {param!r}")
    if parent.params.get(param) == value:
        raise EvolutionError("a mutation must change the parameter")
    params = dict(parent.params)
    params[param] = value
    child = parent.model_copy(
        update={
            "version": _bump(parent.version),
            "params": params,
            "lineage": (*parent.lineage, parent.ref),
        }
    )
    return Offspring(
        spec=StrategySpec.model_validate(child.model_dump()),
        parents=(parent.ref,),
        operator=f"mutate:{param}",
    )


def combine(first: StrategySpec, second: StrategySpec, name: str) -> Offspring:
    if first.ref == second.ref:
        raise EvolutionError("a combination needs two different strategies")
    shared = set(first.params) & set(second.params)
    clash = sorted(
        key
        for key in shared
        if type(first.params[key]) is not type(second.params[key])
        or first.params[key] != second.params[key]
    )
    if clash:
        raise EvolutionError(f"parameters {clash} disagree between the parents")
    shared_spaces = set(first.param_search_space) & set(second.param_search_space)
    space_clash = sorted(
        key
        for key in shared_spaces
        if len(first.param_search_space[key]) != len(second.param_search_space[key])
        or any(
            type(left) is not type(right) or left != right
            for left, right in zip(
                first.param_search_space[key], second.param_search_space[key], strict=True
            )
        )
    )
    if space_clash:
        raise EvolutionError(f"parameter search spaces {space_clash} disagree between the parents")
    if first.risk_policy != second.risk_policy:
        raise EvolutionError("risk_policy must match exactly between the parents")
    if first.applicable_instruments != second.applicable_instruments:
        raise EvolutionError("applicable_instruments must match exactly between the parents")

    params = {**first.params, **second.params}
    param_search_space = {**first.param_search_space, **second.param_search_space}
    outside_space = sorted(
        key
        for key, value in params.items()
        if key in param_search_space
        and not any(
            type(value) is type(candidate) and value == candidate
            for candidate in param_search_space[key]
        )
    )
    if outside_space:
        raise EvolutionError(f"parameters {outside_space} are outside the combined search spaces")

    signals = tuple(sorted({*first.signals, *second.signals}, key=str))
    child = StrategySpec.model_validate(
        {
            "name": name,
            "version": "1.0.0",
            "signals": signals,
            "params": params,
            "param_search_space": param_search_space,
            "risk_policy": first.risk_policy,
            "applicable_instruments": first.applicable_instruments,
            "lineage": (first.ref, second.ref),
        }
    )
    return Offspring(spec=child, parents=(first.ref, second.ref), operator="combine")


def require_new_version(active: StrategySpec, candidate: StrategySpec) -> None:
    """Refuse an in-place change of ``active`` (roadmap P12: no online parameter edits)."""
    if candidate.content_hash() == active.content_hash():
        return  # unchanged
    if candidate.ref == active.ref or candidate.version == active.version:
        raise EvolutionError(f"{active.ref} cannot be changed in place: publish a new version")
    if active.ref not in candidate.lineage:
        raise EvolutionError(f"{candidate.ref} does not descend from {active.ref}")


def retire(
    subject: StrategySpec,
    reason: str,
    *,
    evidence: tuple[str, ...] = (),
    active_from: datetime | None = None,
    active_to: datetime | None = None,
    execution_mode: ExecutionMode | None = None,
    lessons: str | None = None,
) -> RetirementRecord:
    return RetirementRecord(
        subject_ref=subject.ref,
        retirement_reason=reason,
        evidence=evidence,
        active_from=active_from,
        active_to=active_to,
        execution_mode=execution_mode,
        lessons=lessons,
    )
