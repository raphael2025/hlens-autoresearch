"""Regression tests for the G4 review fixes (ADR-0041 implementation note, 2026-09-25; R13 - R18).

Every test here failed before the fix. The rule under test: computing a number, or an empty /
disabled configuration of a Constitution-required check, is never by itself a PASS.

Explicit thresholds below (``explicit_threshold(...)``) are TEST ONLY, arbitrary and uncalibrated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict, derive_verdict
from research.strategies.validation import BacktestValidation
from research.validation import build_report, report_view, run_robustness
from research.validation.gates import (
    CONFIGURATION_MISSING,
    PROFILE_FIELD_MISSING,
    explicit_threshold,
    flag_gate,
)
from research.validation.overfitting import (
    CscvPurgeTooWide,
    probability_of_backtest_overfitting,
)
from research.validation.report import SEALED_OOS_NOT_EVALUATED, VERDICT_NOT_PASS
from research.validation.robustness import (
    CapacityFill,
    CheckStatus,
    RobustnessCheck,
    StateTrade,
    capacity_check,
    cross_asset_check,
    delay_stress_check,
    overfitting_check,
    parameter_neighborhood_check,
    state_decomposition_check,
    time_alignment_check,
    walk_forward_check,
)
from research.validation.splits import non_overlapping_windows, walk_forward_windows
from tests.research.validation import robustness_fixtures as rf
from tests.research.validation.fixtures import context

PROFILE = rf.G4_TEST_ONLY_PROFILE
T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _ids(gates: tuple[GateResult, ...]) -> dict[str, GateResult]:
    return {gate.gate_id: gate for gate in gates}


def _with(profile: ValidationProfile, group: str, **update: object) -> ValidationProfile:
    return profile.model_copy(update={group: getattr(profile, group).model_copy(update=update)})


# --------------------------------------------------------------------------------------
# R13 — C-R5 capacity: a missing min_capacity is INCONCLUSIVE; an estimate is never a PASS
# --------------------------------------------------------------------------------------


def _fills() -> tuple[CapacityFill, ...]:
    return tuple(
        CapacityFill(
            time=T0 + i * MINUTE, traded_fraction=Decimal(1), bar_volume_notional=Decimal(100)
        )
        for i in range(5)
    )


def test_r13_missing_min_capacity_is_an_inconclusive_gate_not_a_silent_pass() -> None:
    check = capacity_check(
        PROFILE,
        _fills(),
        10,
        max_participation=explicit_threshold("capacity.max_participation_rate", 0.01),
        min_capacity=None,
        impact_coefficient=0.1,
    )
    gates = _ids(check.gates)
    required = gates["G4.capacity.required"]
    assert required.verdict is Verdict.INCONCLUSIVE
    assert required.metric == f"{PROFILE_FIELD_MISSING}:capacity.min_capacity"
    assert derive_verdict(check.gates) is not Verdict.PASS
    assert check.status is CheckStatus.PROFILE_FIELD_MISSING
    assert check.details["capacity"] == pytest.approx(0.01 * 100)  # still reported


def test_r13_an_estimated_capacity_is_never_a_pass_gate_by_itself() -> None:
    check = capacity_check(
        PROFILE,
        _fills(),
        10,
        max_participation=explicit_threshold("capacity.max_participation_rate", 0.01),
        min_capacity=explicit_threshold("capacity.min_capacity", 0.5),  # TEST ONLY
        impact_coefficient=0.1,
    )
    gates = _ids(check.gates)
    assert "G4.capacity.estimated" not in gates  # the estimate is reported, not gated
    assert set(gates) == {"G4.capacity.required"}
    assert gates["G4.capacity.required"].threshold_source == "param:capacity.min_capacity"
    assert gates["G4.capacity.required"].verdict is Verdict.PASS  # 1.0 >= 0.5: a comparison


def test_r13_a_missing_impact_coefficient_is_inconclusive() -> None:
    check = capacity_check(
        PROFILE,
        _fills(),
        10,
        max_participation=explicit_threshold("capacity.max_participation_rate", 0.01),
        min_capacity=explicit_threshold("capacity.min_capacity", 0.5),  # TEST ONLY
        impact_coefficient=None,
    )
    gate = _ids(check.gates)["G4.capacity.impact_estimated"]
    assert gate.verdict is Verdict.INCONCLUSIVE
    assert gate.metric == f"{PROFILE_FIELD_MISSING}:capacity.impact_coefficient"
    assert derive_verdict(check.gates) is not Verdict.PASS


def test_r13_a_check_cannot_list_a_missing_field_without_an_inconclusive_gate() -> None:
    passed = flag_gate("G4.x", "m", True, 1.0)
    with pytest.raises(ValueError, match="missing field"):
        RobustnessCheck("x", ("C-R5",), (passed,), missing_fields=("capacity.min_capacity",))
    with pytest.raises(ValueError, match="NOT_APPLICABLE needs a reason"):
        RobustnessCheck("x", ("C-R5",), ())
    assert RobustnessCheck("x", ("C-R5",), (), note="reason").status is CheckStatus.NOT_APPLICABLE


# --------------------------------------------------------------------------------------
# R14 — an empty / disabled configuration of a required check is INCONCLUSIVE, never skipped
# --------------------------------------------------------------------------------------


def _configuration_missing(check: RobustnessCheck, gate_id: str, what: str) -> None:
    gate = _ids(check.gates)[gate_id]
    assert gate.verdict is Verdict.INCONCLUSIVE
    assert gate.metric == f"{CONFIGURATION_MISSING}:{what}"
    assert check.status is CheckStatus.INCONCLUSIVE
    assert derive_verdict(check.gates) is Verdict.INCONCLUSIVE


def test_r14_no_parameter_neighbours_is_inconclusive() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    chosen = trials[0]
    check = parameter_neighborhood_check(PROFILE, [chosen], chosen.params, {"lookback": (1,)})
    for gate_id in (
        "G4.param_neighborhood.performance_ratio",
        "G4.param_neighborhood.positive_fraction",
    ):
        _configuration_missing(check, gate_id, "param_search_space.neighbors")


def test_r14_no_time_alignment_offsets_is_inconclusive() -> None:
    off = _with(PROFILE, "parameter_stability", time_alignment_offsets=())
    _, trials = rf.momentum_family(3, "0.3")
    check = time_alignment_check(off, trials[0].returns, {})
    _configuration_missing(
        check, "G4.time_alignment.offsets", "parameter_stability.time_alignment_offsets"
    )


def test_r14_zero_delay_stress_is_inconclusive() -> None:
    off = _with(PROFILE, "cost_stress", delay_stress_bars=0)
    _configuration_missing(
        delay_stress_check(off, None), "G4.delay_stress", "cost_stress.delay_stress_bars"
    )


def test_r14_no_declared_instrument_scope_is_inconclusive() -> None:
    fraction = explicit_threshold("cross_asset.min_positive_fraction", 0.5)  # TEST ONLY
    check = cross_asset_check(PROFILE, {}, (), fraction)
    _configuration_missing(check, "G4.cross_asset.scope_covered", "declared_instruments")


def test_r14_a_fully_unconfigured_g4_cannot_pass() -> None:
    """Every required check still materializes a gate when its configuration is empty."""
    profile = _with(
        _with(PROFILE, "parameter_stability", time_alignment_offsets=()),
        "cost_stress",
        delay_stress_bars=0,
    )
    _, trials = rf.momentum_family(3, "0.3")
    chosen = trials[0]
    inp = rf.robustness_input(
        [chosen], chosen.params, {"lookback": (1,)}, profile=profile, per_asset={}, declared=()
    )
    result = run_robustness(inp)
    assert all(check.gates for check in result.checks)
    metrics = {gate.metric for gate in result.gates}
    for what in (
        "param_search_space.neighbors",
        "parameter_stability.time_alignment_offsets",
        "cost_stress.delay_stress_bars",
        "declared_instruments",
    ):
        assert f"{CONFIGURATION_MISSING}:{what}" in metrics
    assert derive_verdict(result.gates) is not Verdict.PASS


# --------------------------------------------------------------------------------------
# R15 — C-R2: the P&L share of undersampled states is reported and never silently ignored
# --------------------------------------------------------------------------------------


def _concentrated() -> list[StateTrade]:
    hour = timedelta(hours=1)
    common = [  # enough effective trades (TEST ONLY profile: 10), tiny positive P&L
        StateTrade("common", T0 + i * hour, T0 + (i + 1) * hour, Decimal("0.0001"))
        for i in range(12)
    ]
    rare = [  # two trades carry almost all of the P&L
        StateTrade("rare", T0 + i * hour, T0 + (i + 1) * hour, Decimal("0.05"))
        for i in range(12, 14)
    ]
    return common + rare


def test_r15_pnl_concentrated_in_undersampled_states_is_not_a_pass_without_a_rule() -> None:
    check = state_decomposition_check(PROFILE, _concentrated(), max_undersampled_share=None)
    gates = _ids(check.gates)
    assert gates["G4.state.pnl_outside_undersampled_states"].verdict is Verdict.PASS
    share = gates["G4.state.undersampled_pnl_share"]
    assert share.verdict is Verdict.INCONCLUSIVE
    assert share.metric == f"{PROFILE_FIELD_MISSING}:state.max_undersampled_pnl_share"
    assert check.status is CheckStatus.PROFILE_FIELD_MISSING
    assert derive_verdict(check.gates) is not Verdict.PASS
    expected = 0.1 / (0.1 + 12 * 0.0001)
    assert check.details["undersampled_pnl_share"] == pytest.approx(expected)


def test_r15_an_explicit_share_limit_gates_the_concentration() -> None:
    limit = explicit_threshold("state.max_undersampled_pnl_share", 0.5)  # TEST ONLY
    check = state_decomposition_check(PROFILE, _concentrated(), max_undersampled_share=limit)
    gate = _ids(check.gates)["G4.state.undersampled_pnl_share"]
    assert gate.verdict is Verdict.FAIL
    assert gate.threshold_source == "param:state.max_undersampled_pnl_share"
    assert gate.metric.endswith("[<=]")


def test_r15_undersampled_states_without_profit_need_no_share_rule() -> None:
    losing = [
        StateTrade(t.state, t.start, t.end, -t.net if t.state == "rare" else t.net)
        for t in _concentrated()
    ]
    check = state_decomposition_check(PROFILE, losing, max_undersampled_share=None)
    assert "G4.state.undersampled_pnl_share" not in _ids(check.gates)
    assert check.status is CheckStatus.PASS
    assert check.details["undersampled_pnl_share"] is None  # total net <= 0: no share


# --------------------------------------------------------------------------------------
# R16 — overlapping walk-forward windows never count a period twice
# --------------------------------------------------------------------------------------


def test_r16_overlapping_walk_forward_windows_are_counted_once() -> None:
    overlapping = PROFILE.model_copy(
        update={
            "data_split": PROFILE.data_split.model_copy(
                update={
                    "walk_forward": PROFILE.data_split.walk_forward.model_copy(
                        update={"test_window": timedelta(hours=2), "step": timedelta(hours=1)}
                    )
                }
            )
        }
    )
    windows = walk_forward_windows(overlapping)
    disjoint = non_overlapping_windows(windows)
    assert len(disjoint) < len(windows)
    _, trials = rf.momentum_family(3, "0.3")
    check = walk_forward_check(overlapping, trials[0].returns)
    rows = check.details["windows"]
    assert isinstance(rows, list) and len(rows) == len(disjoint)
    ends = [(row["test_start"], row["test_end"]) for row in rows]
    for (_, end), (start, _) in zip(ends, ends[1:], strict=False):
        assert start >= end  # pairwise disjoint test ranges
    assert check.details["windows_skipped_overlapping"] == len(windows) - len(disjoint)
    assert non_overlapping_windows(walk_forward_windows(PROFILE)) == walk_forward_windows(PROFILE)


# --------------------------------------------------------------------------------------
# R17 — CSCV purges / embargoes in-sample periods next to every out-of-sample block
# --------------------------------------------------------------------------------------


def _boundary_leak() -> tuple[list[list[float]], list[datetime]]:
    """Trial 0 earns only on the four periods around the block boundary (8 - 11); trial 1 has a
    small genuine edge elsewhere. Unpurged, the boundary leaks into both halves."""
    alternating = [0.001 if i % 2 == 0 else -0.001 for i in range(20)]
    trial0 = [0.01 if 8 <= i <= 11 else alternating[i] for i in range(20)]
    trial1 = [0.0005 + (0.002 if i % 2 == 0 else -0.002) for i in range(20)]
    return [trial0, trial1], [T0 + i * MINUTE for i in range(20)]


def test_r17_cscv_purge_removes_the_boundary_leak() -> None:
    matrix, times = _boundary_leak()
    plain = probability_of_backtest_overfitting(matrix, 2, times=times, embargo=timedelta(0))
    purged = probability_of_backtest_overfitting(matrix, 2, times=times, embargo=3 * MINUTE)
    assert plain.pbo == 0.0 and plain.purged_in_sample_periods_max == 0
    assert purged.pbo == 1.0
    assert purged.purged_in_sample_periods_max == 2  # strictly within 3 minutes of the block
    with pytest.raises(CscvPurgeTooWide):
        probability_of_backtest_overfitting(matrix, 2, times=times, embargo=timedelta(hours=1))


def test_r17_overfitting_check_purges_with_the_profile_embargo() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    check = overfitting_check(PROFILE, trials, chosen.params, len(trials), 10)
    pbo = check.details["pbo"]
    assert isinstance(pbo, dict)
    assert pbo["embargo_source"] == "data_split.embargo"
    assert pbo["embargo_seconds"] == PROFILE.data_split.embargo.total_seconds()
    assert pbo["purged_in_sample_periods_max"] > 0
    wide = PROFILE.model_copy(
        update={"data_split": PROFILE.data_split.model_copy(update={"embargo": timedelta(days=1)})}
    )
    refused = overfitting_check(wide, trials, chosen.params, len(trials), 10)
    (gate,) = refused.gates
    assert gate.verdict is Verdict.INCONCLUSIVE
    assert gate.metric == "pbo_not_computed:purge_leaves_too_few_in_sample_periods"


# --------------------------------------------------------------------------------------
# R18 — a G0 - G4 PASS is labelled not promotable without G5
# --------------------------------------------------------------------------------------


def _report(*gates: GateResult):  # type: ignore[no-untyped-def]
    return build_report(context(profile=PROFILE), gates)


def test_r18_a_pass_without_g5_is_labelled_not_promotable() -> None:
    g4_pass = _report(flag_gate("G0.bindings", "ok", True, 1.0), flag_gate("G4.x", "ok", True, 1.0))
    assert g4_pass.verdict is Verdict.PASS
    view = report_view(g4_pass)
    assert view["promotion"]["blocked_reason"] == SEALED_OOS_NOT_EVALUATED  # type: ignore[index]
    assert view["promotion"]["sealed_oos_evaluated"] is False  # type: ignore[index]
    assert BacktestValidation(report=g4_pass).promotion_blocked_reason == SEALED_OOS_NOT_EVALUATED
    with_g5 = _report(flag_gate("G4.x", "ok", True, 1.0), flag_gate("G5.y", "ok", True, 1.0))
    assert report_view(with_g5)["promotion"]["blocked_reason"] is None  # type: ignore[index]
    failed = _report(flag_gate("G4.x", "ok", False, 0.0))
    assert report_view(failed)["promotion"]["blocked_reason"] == VERDICT_NOT_PASS  # type: ignore[index]
