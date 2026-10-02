"""Combination / transformation operators (04-research-loop.md §4; roadmap Phase 7).

Each operator turns upstream objects (``Ref``s of features, states, events, strategies) into a
``Hypothesis`` with ``origin = combination`` and the upstream refs as ``origin_refs``, so every
generated hypothesis is traceable and, once registered in the ``TrialLedger``, counted as a trial.
Operators only produce specifications; nothing generated is ever executed as code.
"""

from __future__ import annotations

from core.domain.base import Ref
from core.domain.research import Hypothesis, HypothesisOrigin

__all__ = ["conditioning", "ensemble", "interaction", "negation", "temporal", "transformation"]


def _hypothesis(
    name: str,
    family_id: str,
    statement: str,
    refs: tuple[Ref, ...],
    direction: str,
    minimum_effect: str,
    conditions: tuple[str, ...] = (),
) -> Hypothesis:
    return Hypothesis(
        name=name,
        version="1.0.0",
        family_id=family_id,
        statement=statement,
        conditions=conditions,
        expected_direction=direction,
        minimum_meaningful_effect=minimum_effect,
        origin=HypothesisOrigin.COMBINATION,
        origin_refs=refs,
    )


def conditioning(
    name: str, family_id: str, strategy: Ref, state: Ref, state_value: str, minimum_effect: str
) -> Hypothesis:
    return _hypothesis(
        name,
        family_id,
        f"{strategy} performs better when {state} = {state_value} than otherwise",
        (strategy, state),
        "higher",
        minimum_effect,
        (f"{state} = {state_value}",),
    )


def interaction(
    name: str, family_id: str, first: Ref, second: Ref, minimum_effect: str
) -> Hypothesis:
    return _hypothesis(
        name,
        family_id,
        f"the product of {first} and {second} carries information neither carries alone",
        (first, second),
        "non-zero",
        minimum_effect,
    )


def temporal(
    name: str, family_id: str, first: Ref, then: Ref, within_bars: int, minimum_effect: str
) -> Hypothesis:
    if within_bars < 1:
        raise ValueError("within_bars must be positive")
    return _hypothesis(
        name,
        family_id,
        f"{then} within {within_bars} bars after {first} is followed by a different outcome",
        (first, then),
        "different",
        minimum_effect,
        (f"window = {within_bars} bars",),
    )


def transformation(
    name: str, family_id: str, source: Ref, transform: str, minimum_effect: str
) -> Hypothesis:
    allowed = {
        "standardize",
        "rank",
        "quantile",
        "difference",
        "smooth",
        "rank_cs",
        "quantile_cs",
    }
    if transform not in allowed:
        raise ValueError(f"unknown transformation {transform!r}")
    return _hypothesis(
        name,
        family_id,
        f"the {transform} of {source} is more informative than the raw series",
        (source,),
        "higher",
        minimum_effect,
        (f"transform = {transform}",),
    )


def ensemble(
    name: str, family_id: str, members: tuple[Ref, ...], minimum_effect: str
) -> Hypothesis:
    if len(set(members)) < 2:
        raise ValueError("an ensemble needs at least two distinct members")
    return _hypothesis(
        name,
        family_id,
        "an equal-weight vote of the members beats each member alone",
        tuple(sorted(set(members), key=str)),
        "higher",
        minimum_effect,
    )


def negation(name: str, family_id: str, strategy: Ref, minimum_effect: str) -> Hypothesis:
    return _hypothesis(
        name,
        family_id,
        f"the inverse of {strategy} loses what {strategy} gains "
        "(a candidate strategy, not a validation negative control)",
        (strategy,),
        "opposite",
        minimum_effect,
    )
