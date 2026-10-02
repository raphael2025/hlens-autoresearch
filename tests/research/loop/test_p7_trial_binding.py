"""ADR-0103 D1 in the loop: ``trial_point`` reads ``p7_plan``, a candidate's plan record names
its own root, and a trial runs a plan candidate only for the hypothesis that names that plan."""

from __future__ import annotations

import pytest

from research.hypotheses.p7_binding import P7PlanRecord, p7_plan_condition
from research.loop.segment import trial_point
from research.loop.trials import _require_plan_binding
from research.strategies.pipeline import StrategyCandidate
from tests.research.loop import loop_fixtures as fx
from tests.research.loop.p7_loop_fixtures import (
    TSMOM,
    compiled_plan,
    negation_plan,
    plan_hypothesis,
    root_strategy,
)


def test_a_candidate_record_must_name_its_own_root() -> None:
    compiled = compiled_plan(negation_plan())
    record = P7PlanRecord.from_compiled(compiled)
    with pytest.raises(ValueError, match="root is not this strategy"):
        StrategyCandidate(
            spec=TSMOM.spec,
            strategy=TSMOM.strategy,
            hypothesis_family_id=fx.FAMILY,
            plan_record=record,
        )
    with pytest.raises(ValueError, match="P7PlanRecord"):
        StrategyCandidate(
            spec=TSMOM.spec,
            strategy=TSMOM.strategy,
            hypothesis_family_id=fx.FAMILY,
            plan_record=record.payload(),  # type: ignore[arg-type]
        )


def test_trial_point_reads_the_p7_plan_condition() -> None:
    compiled = compiled_plan(negation_plan())
    hypothesis = plan_hypothesis(compiled)
    point = trial_point(hypothesis)
    assert point.p7_plan == compiled.plan_hash
    assert point.strategy == f"{compiled.root.spec.name}@{compiled.root.spec.version}"
    plain = plan_hypothesis(
        compiled, conditions=("strategy = tsmom_bars@1.0.0", "param lookback = 60")
    )
    assert trial_point(plain).p7_plan is None
    twice = plan_hypothesis(
        compiled,
        conditions=(
            "strategy = tsmom_bars@1.0.0",
            p7_plan_condition("a" * 64),
            p7_plan_condition("b" * 64),
        ),
    )
    with pytest.raises(ValueError, match="more than one"):
        trial_point(twice)
    malformed = plan_hypothesis(
        compiled, conditions=("strategy = tsmom_bars@1.0.0", "p7_plan = zz")
    )
    with pytest.raises(ValueError, match="unsupported condition"):
        trial_point(malformed)


def test_a_trial_runs_a_plan_candidate_only_for_its_own_plan() -> None:
    compiled = compiled_plan(negation_plan())
    candidate = StrategyCandidate(
        spec=root_strategy(compiled),
        strategy=TSMOM.strategy,
        hypothesis_family_id=fx.FAMILY,
        plan_record=P7PlanRecord.from_compiled(compiled),
    )
    hypothesis = plan_hypothesis(compiled)
    _require_plan_binding(hypothesis, compiled.plan_hash, candidate)
    _require_plan_binding(hypothesis, None, TSMOM)
    with pytest.raises(ValueError, match="belongs to plan"):
        _require_plan_binding(hypothesis, None, candidate)
    with pytest.raises(ValueError, match="belongs to plan"):
        _require_plan_binding(hypothesis, "a" * 64, candidate)
    with pytest.raises(ValueError, match="belongs to plan"):
        _require_plan_binding(hypothesis, compiled.plan_hash, TSMOM)
