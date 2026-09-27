"""G4 compares the execution model's impact coefficient with the explicit parameter exactly
(ADR-0041 implementation note, review fixes 3, 2026-09-26): ``Decimal`` against ``Decimal`` (a
``float`` parameter read as its exact text), never through a ``float`` round trip.

The coefficients below are TEST ONLY, arbitrary and uncalibrated.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from core.domain.research import Verdict
from research.validation import RobustnessResult, run_robustness
from research.validation.g4 import _resolved_impact
from tests.research.validation import robustness_fixtures as rf

MISMATCH = "impact_coefficient_mismatch"


def _with_impact(explicit: object, model: Decimal | float | None) -> RobustnessResult:
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    # A Decimal / int parameter is deliberate here: it too must compare exactly.
    params = replace(rf.G4_TEST_ONLY_PARAMS, impact_coefficient=explicit)  # type: ignore[arg-type]
    inp = rf.robustness_input(trials, chosen.params, {"lookback": rf.LOOKBACKS}, params=params)
    return run_robustness(replace(inp, execution_impact_coefficient=model))


def _impact_gate(result: RobustnessResult) -> Verdict | None:
    gates = {g.gate_id: g for g in result.gates}
    gate = gates.get("G4.capacity.impact_estimated")
    if gate is None:
        return None
    assert gate.metric == MISMATCH
    return gate.verdict


@pytest.mark.parametrize(
    ("explicit", "model"),
    [
        (0.1, Decimal("0.1")),  # float parameter, Decimal model: the same number
        (0.1, Decimal("0.10")),  # trailing zeros are the same number
        (Decimal("0.1"), Decimal("0.1")),  # a Decimal parameter used to mismatch a float model
        (Decimal("0.1"), 0.1),
        (1, Decimal("1.000")),
        (1e-05, Decimal("0.00001")),
    ],
)
def test_equal_coefficients_are_not_a_mismatch(explicit: object, model: Decimal | float) -> None:
    coefficient, source, conflict = _resolved_impact(
        replace(rf.G4_TEST_ONLY_PARAMS, impact_coefficient=explicit),  # type: ignore[arg-type]
        model,
    )
    assert conflict is None and source == "execution_model"
    assert coefficient == float(model)
    result = _with_impact(explicit, model)
    assert _impact_gate(result) is None
    details = {c.check_id: c for c in result.checks}["capacity"].details
    assert details["impact_coefficient_source"] == "execution_model"
    assert details["impact_cost_per_period_at_capacity"] is not None


@pytest.mark.parametrize(
    ("explicit", "model"),
    [
        (0.5, Decimal("0.1")),  # a real difference, as before
        # Differs from 0.1 only beyond float precision: float(model) == 0.1 used to hide it.
        (0.1, Decimal("0.1000000000000000000001")),
        (Decimal("0.1"), Decimal("0.1000000000000000000001")),
    ],
)
def test_a_real_difference_is_inconclusive(explicit: object, model: Decimal) -> None:
    coefficient, _, conflict = _resolved_impact(
        replace(rf.G4_TEST_ONLY_PARAMS, impact_coefficient=explicit),  # type: ignore[arg-type]
        model,
    )
    assert coefficient is None and conflict == (float(explicit), float(model))  # type: ignore[arg-type]
    result = _with_impact(explicit, model)
    assert _impact_gate(result) is Verdict.INCONCLUSIVE
    details = {c.check_id: c for c in result.checks}["capacity"].details
    assert details["impact_not_estimated"] == MISMATCH
    assert details["impact_cost_per_period_at_capacity"] is None


def test_a_non_numeric_coefficient_is_refused() -> None:
    with pytest.raises(TypeError, match="impact_coefficient"):
        _resolved_impact(
            replace(rf.G4_TEST_ONLY_PARAMS, impact_coefficient=True),
            Decimal("1"),
        )
    with pytest.raises(TypeError, match="execution_impact_coefficient"):
        _resolved_impact(rf.G4_TEST_ONLY_PARAMS, "0.1")  # type: ignore[arg-type]
