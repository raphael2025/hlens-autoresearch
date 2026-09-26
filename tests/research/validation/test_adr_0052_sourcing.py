"""ADR-0052 research-side sourcing: exact comparison, Profile fields, negative-control threshold.

!!! TEST ONLY !!! Every number below (thresholds, partitions, budgets, coefficients) is an
arbitrary, uncalibrated fixture value chosen to make the tests readable; none is a proposal or a
calibration result (Profile numbers stay TBD until the Step 2 freeze, ADR-0007 / ADR-0052).

For each Profile-sourced rule: **present** (the value and its source come from the Profile),
**absent** (an old Profile: the ``param:`` / ``INCONCLUSIVE`` behaviour, bit for bit) and
**conflict** (Profile field + explicit ``param:`` → ``ExplicitParamRefused``, C-A4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.validation_profile import (
    CapacityParams,
    CrossAssetParams,
    ValidationProfile,
)
from core.domain.research import GateResult, Verdict
from plugins.backtest import IMPACT_MODEL
from research.loop.trials import OosUnsealBudget
from research.outcomes import OutcomeTable
from research.validation.calibration import MomentumSignStudy
from research.validation.g4 import RobustnessParams, run_robustness
from research.validation.gates import (
    GATE_VALUE_QUANTIZATION,
    Direction,
    ExplicitParamRefused,
    compare_gate,
    explicit_threshold,
    quantize_gate_value,
    sourced_parameter,
    sourced_threshold,
    threshold,
)
from research.validation.pipeline import negative_control_threshold, run_in_sample
from research.validation.robustness import IMPACT_MODELS
from research.validation.sealed_oos import InMemoryUnsealingLedger, SealedOosVault
from research.validation.stats import UnsupportedMethod
from tests.research.validation import robustness_fixtures as rf
from tests.research.validation.fixtures import (
    TEST_ONLY_PROFILE,
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
)

AT = datetime(2024, 1, 3, tzinfo=UTC)


def _with(profile: ValidationProfile, group: str | None = None, **update: Any) -> ValidationProfile:
    if group is None:
        return profile.model_copy(update=update)
    block = getattr(profile, group).model_copy(update=update)
    return profile.model_copy(update={group: block})


# ======================================================================================
# the quantization rule and exact comparison (ADR-0052 §1)
# ======================================================================================


def test_the_quantization_rule_is_versioned_and_exact() -> None:
    assert GATE_VALUE_QUANTIZATION == "hlens.validation.gate-value-quantization@1.0.0"
    assert quantize_gate_value(0.1) == Decimal("0.1")  # the binary noise is below the quantum
    assert quantize_gate_value(0.1 + 0.2) == Decimal("0.3")
    assert quantize_gate_value(Decimal("0.25")) == Decimal("0.25")
    assert quantize_gate_value(2) == Decimal(2)
    assert quantize_gate_value(-0.0) == Decimal(0)  # canonical: no -0
    for bad in (float("nan"), float("inf"), True, "0.1"):
        with pytest.raises(ValueError):
            quantize_gate_value(bad)  # type: ignore[arg-type]


def test_an_old_profile_threshold_stays_a_float_threshold() -> None:
    limit = threshold(TEST_ONLY_PROFILE, "significance.overfitting_threshold")
    assert (limit.value, limit.source, limit.exact) == (
        0.5,
        "significance.overfitting_threshold",
        None,
    )


def test_an_exact_sibling_makes_the_threshold_exact() -> None:
    profile = _with(TEST_ONLY_PROFILE, "significance", overfitting_threshold_exact=Decimal("0.5"))
    limit = threshold(profile, "significance.overfitting_threshold")
    assert limit.exact == Decimal("0.5") and limit.value == 0.5
    assert limit.source == "significance.overfitting_threshold_exact"


def test_exact_comparison_is_immune_to_float_noise() -> None:
    old = _with(TEST_ONLY_PROFILE, "significance", overfitting_threshold=0.3)
    new = _with(old, "significance", overfitting_threshold_exact=Decimal("0.3"))
    value = 0.1 + 0.2  # 0.30000000000000004 in binary floating point
    float_gate = compare_gate(
        old,
        "G4.overfitting",
        "m",
        value,
        threshold(old, "significance.overfitting_threshold"),
        Direction.AT_MOST,
    )
    exact_gate = compare_gate(
        new,
        "G4.overfitting",
        "m",
        value,
        threshold(new, "significance.overfitting_threshold"),
        Direction.AT_MOST,
    )
    assert float_gate.verdict is Verdict.FAIL and float_gate.value_exact is None
    assert exact_gate.verdict is Verdict.PASS
    assert (exact_gate.value_exact, exact_gate.threshold_exact) == (Decimal("0.3"), Decimal("0.3"))
    assert exact_gate.value == 0.3 and exact_gate.threshold == 0.3
    assert exact_gate.schema_version == "2.1.0"
    assert GateResult.model_validate_json(exact_gate.model_dump_json()) == exact_gate


def test_the_exact_inconclusive_band_is_used() -> None:
    profile = _with(
        TEST_ONLY_PROFILE,
        inconclusive_bands={"G4.overfitting": 0.05},
        inconclusive_bands_exact={"G4.overfitting": Decimal("0.05")},
    )
    profile = _with(profile, "significance", overfitting_threshold_exact=Decimal("0.5"))
    limit = threshold(profile, "significance.overfitting_threshold")
    near = compare_gate(profile, "G4.overfitting", "m", 0.55, limit, Direction.AT_MOST)
    far = compare_gate(profile, "G4.overfitting", "m", 0.56, limit, Direction.AT_MOST)
    assert near.verdict is Verdict.INCONCLUSIVE  # |0.55 - 0.5| = 0.05 exactly
    assert far.verdict is Verdict.FAIL


# ======================================================================================
# sourcing helpers (ADR-0052 §2, C-A4)
# ======================================================================================

CAPACITY = CapacityParams(
    min_capacity=Decimal("1000"),
    max_participation_rate=Decimal("0.01"),
    impact_coefficient=Decimal("0.1"),
    impact_model="square_root",
)


def test_sourced_threshold_present_absent_conflict() -> None:
    path, name = "cross_asset.min_positive_fraction", "cross_asset.min_positive_fraction"
    explicit = explicit_threshold(name, 0.5)
    absent = sourced_threshold(TEST_ONLY_PROFILE, path, explicit, name)
    assert absent == explicit and absent.source == f"param:{name}"
    assert sourced_threshold(TEST_ONLY_PROFILE, path, None, name) is None
    profile = _with(
        TEST_ONLY_PROFILE, cross_asset=CrossAssetParams(min_positive_fraction=Decimal("0.6"))
    )
    present = sourced_threshold(profile, path, None, name)
    assert present is not None and present.source == path and present.exact == Decimal("0.6")
    with pytest.raises(ExplicitParamRefused, match="C-A4"):
        sourced_threshold(profile, path, explicit, name)


def test_sourced_parameter_present_absent_conflict() -> None:
    path = "significance.cscv_partitions"
    assert sourced_parameter(TEST_ONLY_PROFILE, path, 10, "cscv_partitions") == (
        10,
        "param:cscv_partitions",
    )
    assert sourced_parameter(TEST_ONLY_PROFILE, path, None, "cscv_partitions") == (
        None,
        "param:cscv_partitions",
    )
    profile = _with(TEST_ONLY_PROFILE, "significance", cscv_partitions=8)
    assert sourced_parameter(profile, path, None, "cscv_partitions") == (8, path)
    with pytest.raises(ExplicitParamRefused):
        sourced_parameter(profile, path, 10, "cscv_partitions")


# ======================================================================================
# G1 negative controls (ADR-0052 §3, D-CTRL)
# ======================================================================================


@pytest.fixture(scope="module")
def planted() -> tuple[Any, OutcomeTable]:
    market = generate(seed=3, strength="0.6")
    return market, outcome_table(market, research_events(market))


def _in_sample(planted: tuple[Any, OutcomeTable], profile: ValidationProfile) -> dict[str, Any]:
    from core.contracts.outcome import OutcomeEvent

    market, table = planted
    events = [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]
    study = MomentumSignStudy(market, events)
    gates = run_in_sample(in_sample_input(table, study, ctx=context(profile=profile)))
    return {gate.gate_id: gate for gate in gates}


def test_an_old_profile_keeps_the_shared_control_threshold(planted: Any) -> None:
    limit = negative_control_threshold(TEST_ONLY_PROFILE)
    assert limit.source == "significance.multiple_testing_threshold" and limit.exact is None
    gates = _in_sample(planted, TEST_ONLY_PROFILE)
    for gate_id in ("G1.shuffle_control", "G1.shift_control", "G3.adjusted_p_value"):
        assert gates[gate_id].threshold_source == "significance.multiple_testing_threshold"


def test_the_controls_use_their_own_field(planted: Any) -> None:
    # TEST ONLY: an impossible control threshold (1) fails the controls (p < 1 when applicable).
    profile = _with(TEST_ONLY_PROFILE, "significance", negative_control_threshold=Decimal(1))
    limit = negative_control_threshold(profile)
    assert limit.source == "significance.negative_control_threshold"
    new = _in_sample(planted, profile)
    for gate_id in ("G1.shuffle_control", "G1.shift_control"):
        gate = new[gate_id]
        assert gate.threshold_source == "significance.negative_control_threshold"
        assert gate.threshold_exact == Decimal(1) and gate.value_exact is not None
        assert gate.verdict is Verdict.FAIL


def test_the_controls_and_g3_are_tuned_independently(planted: Any) -> None:
    # TEST ONLY: a control threshold of 0 never fails a control; G3 keeps its own field and value.
    profile = _with(TEST_ONLY_PROFILE, "significance", negative_control_threshold=Decimal(0))
    old, new = _in_sample(planted, TEST_ONLY_PROFILE), _in_sample(planted, profile)
    for gate_id in ("G1.shuffle_control", "G1.shift_control"):
        assert new[gate_id].verdict is not Verdict.FAIL
        assert new[gate_id].threshold == 0.0
    g3_old, g3_new = old["G3.adjusted_p_value"], new["G3.adjusted_p_value"]
    assert g3_new.threshold_source == "significance.multiple_testing_threshold"
    assert (g3_new.value, g3_new.threshold, g3_new.verdict) == (
        g3_old.value,
        g3_old.threshold,
        g3_old.verdict,
    )


# ======================================================================================
# G4 (ADR-0052 §2)
# ======================================================================================

G4_FIELDS_PROFILE = _with(
    _with(
        rf.G4_TEST_ONLY_PROFILE,
        capacity=CAPACITY,
        cross_asset=CrossAssetParams(min_positive_fraction=Decimal("0.5")),
    ),
    "significance",
    cscv_partitions=10,
)
G4_FIELDS_PROFILE = _with(
    G4_FIELDS_PROFILE, "sample_size", max_undersampled_pnl_share=Decimal("0.5")
)


def _g4(profile: ValidationProfile, params: RobustnessParams, **kwargs: Any) -> Any:
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    other = rf.best(rf.noise_family(5)[1]).returns
    inp = rf.robustness_input(
        trials,
        chosen.params,
        {"lookback": rf.LOOKBACKS},
        profile=profile,
        params=params,
        per_asset={"SYN-USDT": chosen.returns, "SYN2-USDT": other},
        declared=("SYN-USDT", "SYN2-USDT"),
    )
    if kwargs:
        from dataclasses import replace

        inp = replace(inp, **kwargs)
    return run_robustness(inp)


def _by_id(result: Any) -> dict[str, GateResult]:
    return {gate.gate_id: gate for gate in result.gates}


def test_g4_reads_every_rule_from_the_profile_when_present() -> None:
    result = _g4(G4_FIELDS_PROFILE, rf.NO_EXPLICIT_PARAMS)
    gates = _by_id(result)
    checks = {check.check_id: check for check in result.checks}
    assert checks["overfitting"].details["pbo"]["partitions_source"] == (
        "significance.cscv_partitions"
    )
    assert gates["G4.overfitting"].threshold_source == "significance.overfitting_threshold"
    required = gates["G4.capacity.required"]
    assert required.threshold_source == "capacity.min_capacity"
    assert required.threshold_exact == Decimal("1000")
    capacity = checks["capacity"]
    assert capacity.details["impact_coefficient_source"] == "capacity.impact_coefficient"
    assert capacity.details["impact_model"] == "square_root"
    uses = {use.name: use.source for use in capacity.thresholds}
    assert uses["max_participation_rate"] == "capacity.max_participation_rate"
    fraction = gates["G4.cross_asset.positive_fraction"]
    assert fraction.threshold_source == "cross_asset.min_positive_fraction"
    assert fraction.threshold_exact == Decimal("0.5")
    assert result.to_dict()["profile_params"] == {
        "capacity.impact_coefficient": "0.1",
        "capacity.impact_model": "square_root",
        "capacity.max_participation_rate": "0.01",
        "capacity.min_capacity": "1000",
        "cross_asset.min_positive_fraction": "0.5",
        "sample_size.max_undersampled_pnl_share": "0.5",
        "significance.cscv_partitions": "10",
    }


def test_an_old_profile_keeps_the_explicit_params_bit_for_bit() -> None:
    result = _g4(rf.G4_TEST_ONLY_PROFILE, rf.G4_TEST_ONLY_PARAMS)
    checks = {check.check_id: check for check in result.checks}
    assert checks["overfitting"].details["pbo"]["partitions_source"] == "param:cscv_partitions"
    assert checks["capacity"].details["impact_coefficient_source"] == (
        "param:capacity.impact_coefficient"
    )
    assert "impact_model" not in checks["capacity"].details
    assert "profile_params" not in result.to_dict()
    fraction = _by_id(result)["G4.cross_asset.positive_fraction"]
    assert fraction.threshold_source == "param:cross_asset.min_positive_fraction"
    assert fraction.value_exact is None and fraction.threshold_exact is None


@pytest.mark.parametrize(
    "explicit",
    [
        {"cscv_partitions": 10},
        {"max_participation_rate": 0.01},
        {"min_capacity": 1000.0},
        {"impact_coefficient": 0.1},
        {"cross_asset_min_positive_fraction": 0.5},
        {"max_undersampled_pnl_share": 0.5},
    ],
    ids=lambda item: next(iter(item)),
)
def test_g4_refuses_an_explicit_param_the_profile_supplies(explicit: dict[str, Any]) -> None:
    from dataclasses import replace

    with pytest.raises(ExplicitParamRefused, match="C-A4"):
        _g4(G4_FIELDS_PROFILE, replace(rf.NO_EXPLICIT_PARAMS, **explicit))


def test_an_unimplemented_impact_model_is_refused() -> None:
    assert IMPACT_MODEL in IMPACT_MODELS  # the backtester's law is the one G4 implements
    profile = _with(
        G4_FIELDS_PROFILE,
        capacity=CAPACITY.model_copy(update={"impact_model": "linear"}),
    )
    with pytest.raises(UnsupportedMethod, match="capacity.impact_model"):
        _g4(profile, rf.NO_EXPLICIT_PARAMS)


def test_a_profile_coefficient_disagreeing_with_the_execution_model_is_a_mismatch() -> None:
    result = _g4(
        G4_FIELDS_PROFILE, rf.NO_EXPLICIT_PARAMS, execution_impact_coefficient=Decimal("0.2")
    )
    capacity = next(check for check in result.checks if check.check_id == "capacity")
    gate = _by_id(result)["G4.capacity.impact_estimated"]
    assert gate.verdict is Verdict.INCONCLUSIVE and gate.metric == "impact_coefficient_mismatch"
    assert capacity.details["impact_coefficient_conflict"] == {
        "profile_capacity_impact_coefficient": 0.1,
        "execution_model_impact_coefficient": 0.2,
    }
    agreed = _g4(
        G4_FIELDS_PROFILE, rf.NO_EXPLICIT_PARAMS, execution_impact_coefficient=Decimal("0.10")
    )
    agreed_capacity = next(check for check in agreed.checks if check.check_id == "capacity")
    assert agreed_capacity.details["impact_coefficient_source"] == "execution_model"


# ======================================================================================
# sealed OOS budget (ADR-0052 §2, C-S2)
# ======================================================================================


def test_the_sealed_oos_budget_present_absent_conflict() -> None:
    old = SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=2)
    assert (old.max_unsealings, old.budget_source) == (2, "param:max_unsealings")
    with pytest.raises(TypeError, match="max_unsealings"):  # unchanged for old Profiles
        SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger())
    profile = _with(TEST_ONLY_PROFILE, "data_split", sealed_oos_max_unsealings=1)
    vault = SealedOosVault(profile, InMemoryUnsealingLedger())
    assert (vault.max_unsealings, vault.budget_source) == (
        1,
        "data_split.sealed_oos_max_unsealings",
    )
    vault.unseal("family-a", "raphael", AT)
    with pytest.raises(Exception, match=r"1 unsealings, data_split\.sealed_oos_max_unsealings"):
        vault.unseal("family-b", "raphael", AT)
    with pytest.raises(ExplicitParamRefused, match="max_unsealings"):
        SealedOosVault(profile, InMemoryUnsealingLedger(), max_unsealings=2)


def test_a_loop_unseal_budget_may_defer_to_the_profile() -> None:
    budget = OosUnsealBudget(max_unsealings=None, approved_families={"family-a": "raphael"})
    assert budget.max_unsealings is None
    with pytest.raises(ValueError, match="positive int"):
        OosUnsealBudget(max_unsealings=0, approved_families={"family-a": "raphael"})
