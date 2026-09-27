"""Phase 8: G4 robustness checks, negative controls and the report view (ADR-0041).

Uses the clearly marked TEST ONLY profiles / params (``robustness_fixtures``). Proven here at
framework level: a best-of-N overfit on pure noise is rejected by G4 (PBO and DSR), a planted
genuine effect is not flagged as overfit, every G4 threshold names its source, rules without a
Profile field are reported as ``profile_field_missing`` instead of being defaulted, G4 composes
after G0 – G3, and the report view is canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.strategy import FillRemainder
from core.domain.research import Verdict, derive_verdict
from core.errors import ReasonCode
from research.validation import (
    build_report,
    failure_record,
    report_view,
    run_robustness,
    run_validation,
    to_json,
)
from research.validation.calibration import MomentumSignStudy
from research.validation.gates import (
    PARAM_SOURCE_PREFIX,
    PROFILE_FIELD_MISSING,
    ProfileFieldMissing,
    explicit_threshold,
    profile_value,
)
from research.validation.returns import PeriodReturns, TrialReturns
from research.validation.robustness import (
    CARRY_OVER_UNFILLED,
    CapacityFill,
    CheckStatus,
    RobustnessCheck,
    StateTrade,
    VolumeSourceMismatch,
    capacity_check,
    cross_asset_check,
    delay_stress_check,
    state_decomposition_check,
)
from research.validation.splits import walk_forward_windows
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

NOISE_SEEDS = (5, 6, 7)
PLANTED_SEEDS = (3, 4)


def _by_id(result: object) -> dict[str, object]:
    return {gate.gate_id: gate for gate in result.gates}  # type: ignore[attr-defined]


def _noise(seed: int, profile: object = rf.G4_TEST_ONLY_PROFILE):  # type: ignore[no-untyped-def]
    _, trials = rf.noise_family(seed)
    chosen = rf.best(trials)
    space = {"variant": tuple(range(len(trials)))}
    return run_robustness(rf.robustness_input(trials, chosen.params, space, profile=profile))


def _planted(seed: int, profile: object = rf.G4_TEST_ONLY_PROFILE):  # type: ignore[no-untyped-def]
    _, trials = rf.momentum_family(seed, "0.3")
    chosen = rf.best(trials)
    space = {"lookback": rf.LOOKBACKS}
    return chosen, run_robustness(
        rf.robustness_input(trials, chosen.params, space, profile=profile)
    )


# --------------------------------------------------------------------------------------
# negative controls (roadmap Phase 8 acceptance)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", NOISE_SEEDS)
@pytest.mark.parametrize("profile", [rf.G4_TEST_ONLY_PROFILE, rf.G4_TEST_ONLY_DSR_PROFILE])
def test_best_of_n_on_pure_noise_is_rejected_as_overfit(seed: int, profile: object) -> None:
    result = _noise(seed, profile)
    gate = _by_id(result)["G4.overfitting"]
    assert gate.verdict is Verdict.FAIL  # type: ignore[attr-defined]
    assert gate.threshold_source == "significance.overfitting_threshold"  # type: ignore[attr-defined]
    assert derive_verdict(result.gates) is Verdict.FAIL
    report = build_report(context(profile=profile), result.gates)  # type: ignore[arg-type]
    record = failure_record(report, "family-1")
    assert record is not None
    assert (record.gate_id, record.reason_code) == (
        "G4.overfitting",
        ReasonCode.NOT_SIGNIFICANT_AFTER_MTC,
    )


@pytest.mark.parametrize("seed", PLANTED_SEEDS)
@pytest.mark.parametrize("profile", [rf.G4_TEST_ONLY_PROFILE, rf.G4_TEST_ONLY_DSR_PROFILE])
def test_a_planted_effect_is_not_flagged_as_overfit(seed: int, profile: object) -> None:
    chosen, result = _planted(seed, profile)
    assert dict(chosen.params) == {"lookback": 1}  # the planted lag-1 effect
    gates = _by_id(result)
    assert gates["G4.overfitting"].verdict is Verdict.PASS  # type: ignore[attr-defined]
    for gate_id in (
        "G4.param_neighborhood.performance_ratio",
        "G4.param_neighborhood.positive_fraction",
    ):
        assert gates[gate_id].verdict is Verdict.PASS  # type: ignore[attr-defined]


def test_pbo_separates_noise_from_the_planted_effect() -> None:
    noise = [_noise(seed).checks[0].details["pbo"] for seed in NOISE_SEEDS]
    planted = [_planted(seed)[1].checks[0].details["pbo"] for seed in PLANTED_SEEDS]
    assert min(item["pbo"] for item in noise) > max(item["pbo"] for item in planted)
    assert all(item["partitions_source"] == "param:cscv_partitions" for item in noise)


# --------------------------------------------------------------------------------------
# thresholds: Profile field or explicit param, never a default
# --------------------------------------------------------------------------------------


def test_every_g4_threshold_names_its_source() -> None:
    mkt, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    lagged = rf._run(mkt, rf.momentum_positions(mkt, 1), delay=1)
    result = run_robustness(
        rf.robustness_input(
            trials,
            chosen.params,
            {"lookback": rf.LOOKBACKS},
            delayed=lagged,
            shifted={timedelta(minutes=1): lagged},
            per_asset={"SYN-USDT": chosen.returns, "SYN2-USDT": lagged},
            declared=("SYN-USDT", "SYN2-USDT"),
        )
    )
    with_threshold = [gate for gate in result.gates if gate.threshold is not None]
    assert len(with_threshold) >= 10
    explicit = {
        f"{PARAM_SOURCE_PREFIX}capacity.max_participation_rate": 0.01,
        f"{PARAM_SOURCE_PREFIX}cross_asset.min_positive_fraction": 0.5,
    }
    for gate in with_threshold:
        source = gate.threshold_source
        assert source is not None and gate.metric.endswith(("[>=]", "[<=]"))
        if source.startswith(PARAM_SOURCE_PREFIX):
            assert explicit[source] == gate.threshold
        else:
            value = profile_value(rf.G4_TEST_ONLY_PROFILE, source)
            assert float(value) == gate.threshold  # type: ignore[arg-type]
    for check in result.checks:
        for use in check.thresholds:
            assert (
                use.source.startswith(PARAM_SOURCE_PREFIX)
                or float(
                    profile_value(rf.G4_TEST_ONLY_PROFILE, use.source)  # type: ignore[arg-type]
                )
                == use.value
            )


def test_rules_without_a_profile_field_are_reported_missing_not_defaulted() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    other = rf.best(rf.noise_family(5)[1]).returns
    inp = rf.robustness_input(
        trials,
        chosen.params,
        {"lookback": rf.LOOKBACKS},
        params=rf.NO_EXPLICIT_PARAMS,
        per_asset={"SYN-USDT": chosen.returns, "SYN2-USDT": other},
        declared=("SYN-USDT", "SYN2-USDT"),
    )
    result = run_robustness(inp)
    gates = _by_id(result)
    for gate_id, field in (
        ("G4.overfitting", "cscv_partitions"),
        ("G4.capacity.estimated", "capacity.max_participation_rate"),
        ("G4.cross_asset.positive_fraction", "cross_asset.min_positive_fraction"),
    ):
        gate = gates[gate_id]
        assert gate.verdict is Verdict.INCONCLUSIVE  # type: ignore[attr-defined]
        assert gate.metric == f"{PROFILE_FIELD_MISSING}:{field}"  # type: ignore[attr-defined]
        assert gate.threshold is None  # type: ignore[attr-defined]
    statuses = {check.check_id: check.status for check in result.checks}
    assert statuses["overfitting"] is CheckStatus.PROFILE_FIELD_MISSING
    assert statuses["capacity"] is CheckStatus.PROFILE_FIELD_MISSING
    assert statuses["cross_asset"] is CheckStatus.PROFILE_FIELD_MISSING
    assert derive_verdict(result.gates) is not Verdict.PASS
    with pytest.raises(ProfileFieldMissing):
        profile_value(rf.G4_TEST_ONLY_PROFILE, "capacity.max_participation_rate")


def test_unknown_overfitting_metric_is_refused() -> None:
    with pytest.raises(UnsupportedMethod):
        _noise(5, TEST_ONLY_PROFILE)  # its overfitting_metric is "not-implemented"


def test_a_stricter_profile_changes_g4_without_code_changes() -> None:
    lax = rf.G4_TEST_ONLY_PROFILE.model_copy(
        update={
            "significance": rf.G4_TEST_ONLY_PROFILE.significance.model_copy(
                update={"overfitting_threshold": 1.0}
            )
        }
    )
    assert _by_id(_noise(5, lax))["G4.overfitting"].verdict is Verdict.PASS  # type: ignore[attr-defined]
    assert _by_id(_noise(5))["G4.overfitting"].verdict is Verdict.FAIL  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------------------


def test_delay_stress_uses_the_profile_delay_and_breakeven() -> None:
    mkt, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    delayed = rf._run(mkt, rf.momentum_positions(mkt, 1), delay=1)
    check = delay_stress_check(rf.G4_TEST_ONLY_PROFILE, delayed)
    (gate,) = check.gates
    assert gate.gate_id == "G4.delay_stress"
    assert gate.threshold_source == "cost_stress.min_breakeven_cost_multiple"
    assert check.details["delay_bars_source"] == "cost_stress.delay_stress_bars"
    before = chosen.returns.breakeven_cost_multiple()
    after = delayed.breakeven_cost_multiple()
    assert before is not None and after is not None and after < before
    missing = delay_stress_check(rf.G4_TEST_ONLY_PROFILE, None)
    assert missing.gates[0].verdict is Verdict.INCONCLUSIVE
    off = rf.G4_TEST_ONLY_PROFILE.model_copy(
        update={
            "cost_stress": rf.G4_TEST_ONLY_PROFILE.cost_stress.model_copy(
                update={"delay_stress_bars": 0}
            )
        }
    )
    # review fix R14: a disabled delay stress is never silently skipped (C-R4 is required)
    disabled = delay_stress_check(off, delayed)
    assert disabled.status is CheckStatus.INCONCLUSIVE
    (off_gate,) = disabled.gates
    assert off_gate.metric == "configuration_missing:cost_stress.delay_stress_bars"


def test_time_alignment_requires_every_profile_offset() -> None:
    mkt, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    space = {"lookback": rf.LOOKBACKS}
    shifted = rf._run(mkt, rf.momentum_positions(mkt, 1), delay=1)
    done = run_robustness(
        rf.robustness_input(trials, chosen.params, space, shifted={timedelta(minutes=1): shifted})
    )
    gate = _by_id(done)["G4.time_alignment.0"]
    assert gate.threshold_source == "parameter_stability.min_neighborhood_performance_ratio"  # type: ignore[attr-defined]
    missing = run_robustness(rf.robustness_input(trials, chosen.params, space))
    assert _by_id(missing)["G4.time_alignment.0"].verdict is Verdict.INCONCLUSIVE  # type: ignore[attr-defined]


def test_walk_forward_windows_come_from_the_profile() -> None:
    _, result = _planted(3)
    check = next(c for c in result.checks if c.check_id == "walk_forward")
    rows = check.details["windows"]
    windows = walk_forward_windows(rf.G4_TEST_ONLY_PROFILE)
    assert len(rows) == len(windows)
    for row, window in zip(rows, windows, strict=True):
        assert row["test_start"] == window.train_end.isoformat()
        assert row["test_end"] == window.test_end.isoformat()
    sources = {gate.threshold_source for gate in check.gates}
    assert sources == {
        "data_split.walk_forward.min_positive_window_fraction",
        "data_split.walk_forward.max_single_window_pnl_share",
    }


def test_state_decomposition_rejects_profit_from_an_undersampled_state() -> None:
    _, result = _planted(3)
    states = next(c for c in result.checks if c.check_id == "state_decomposition")
    assert states.thresholds[0].source == "sample_size.min_effective_trades_per_state"
    base = rf.market(3).bars[0].interval_end
    hour = timedelta(hours=1)
    common = [
        StateTrade("common", base + i * hour, base + (i + 1) * hour, Decimal("-0.001"))
        for i in range(12)
    ]
    rare = [
        StateTrade("rare", base + i * hour, base + (i + 1) * hour, Decimal("0.05"))
        for i in range(12, 14)
    ]
    check = state_decomposition_check(
        rf.G4_TEST_ONLY_PROFILE, common + rare, max_undersampled_share=None
    )
    gates = {gate.gate_id: gate for gate in check.gates}
    assert gates["G4.state.pnl_outside_undersampled_states"].verdict is Verdict.FAIL
    assert check.status is CheckStatus.FAIL
    unlabeled = state_decomposition_check(
        rf.G4_TEST_ONLY_PROFILE, None, max_undersampled_share=None
    )
    assert unlabeled.gates[0].verdict is Verdict.INCONCLUSIVE


def test_capacity_is_participation_times_the_thinnest_fill() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    space = {"lookback": rf.LOOKBACKS}
    result = run_robustness(rf.robustness_input(trials, chosen.params, space))
    check = next(c for c in result.checks if c.check_id == "capacity")
    fills = rf.capacity_fills(chosen.returns, Decimal(1_000_000))
    thinnest = min(float(f.bar_volume_notional) / float(f.traded_fraction) for f in fills)  # type: ignore[arg-type]
    assert check.details["capacity"] == pytest.approx(0.01 * thinnest)
    assert check.details["impact_cost_per_period_at_capacity"] is not None
    assert "capacity.min_capacity" in check.missing_fields  # no judgement rule: reported
    strict = rf.G4_TEST_ONLY_PARAMS.__class__(
        **{**rf.G4_TEST_ONLY_PARAMS.__dict__, "min_capacity": 1e15}
    )
    judged = run_robustness(rf.robustness_input(trials, chosen.params, space, params=strict))
    gate = _by_id(judged)["G4.capacity.required"]
    assert gate.verdict is Verdict.FAIL  # type: ignore[attr-defined]
    assert gate.threshold_source == "param:capacity.min_capacity"  # type: ignore[attr-defined]


def test_cross_asset_consistency_and_scope() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    good = rf.best(trials).returns
    bad = PeriodReturns(times=good.times, gross=tuple(-g for g in good.gross), cost=good.cost)
    fraction = rf.G4_TEST_ONLY_PARAMS.cross_asset_fraction
    check = cross_asset_check(rf.G4_TEST_ONLY_PROFILE, {"A": good, "B": bad}, ("A", "B"), fraction)
    gates = {gate.gate_id: gate for gate in check.gates}
    assert gates["G4.cross_asset.scope_covered"].verdict is Verdict.PASS
    assert gates["G4.cross_asset.positive_fraction"].value == 0.5
    untested = cross_asset_check(rf.G4_TEST_ONLY_PROFILE, {"A": good}, ("A", "B"), fraction)
    assert untested.status is CheckStatus.INCONCLUSIVE
    assert untested.details["not_tested"] == ["B"]


def test_trials_must_share_one_period_grid() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    short = trials[1].returns
    cut = PeriodReturns(times=short.times[:-1], gross=short.gross[:-1], cost=short.cost[:-1])
    broken = [trials[0], TrialReturns(params=trials[1].params, returns=cut), *trials[2:]]
    with pytest.raises(ValueError, match="period grid"):
        run_robustness(rf.robustness_input(broken, trials[0].params, {"lookback": rf.LOOKBACKS}))


# --------------------------------------------------------------------------------------
# composition G0 -> G4 and the report view
# --------------------------------------------------------------------------------------


def _p4_input(**kwargs: object):  # type: ignore[no-untyped-def]
    mkt = generate(seed=3, strength="0.6")
    table = outcome_table(mkt, research_events(mkt))
    events = [e for e in research_events(mkt)]
    ctx = context(profile=rf.G4_TEST_ONLY_PROFILE)
    return in_sample_input(table, MomentumSignStudy(mkt, events), ctx=ctx, **kwargs)  # type: ignore[arg-type]


def _g4_input():  # type: ignore[no-untyped-def]
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    return rf.robustness_input(trials, chosen.params, {"lookback": rf.LOOKBACKS})


def test_run_validation_runs_g4_after_g0_to_g3() -> None:
    run = run_validation(_p4_input(), _g4_input)
    stages = {gate.gate_id.split(".")[0] for gate in run.gates}
    assert stages == {"G0", "G1", "G2", "G3", "G4"}
    assert run.robustness is not None
    assert run.stopped_at in (None, "G4")
    assert run.gates[-len(run.robustness.gates) :] == run.robustness.gates


def test_g4_is_lazy_after_an_earlier_fail_and_never_silently_skipped() -> None:
    def explode():  # type: ignore[no-untyped-def]
        raise AssertionError("G4 input must not be built after an earlier FAIL")

    failed = run_validation(_p4_input(reproduced_hash="0" * 64), explode)
    assert failed.stopped_at == "G0" and failed.robustness is None
    absent = run_validation(_p4_input(), None)
    gate = absent.gates[-1]
    assert (gate.gate_id, gate.verdict) == ("G4.robustness_input", Verdict.INCONCLUSIVE)
    assert derive_verdict(absent.gates) is not Verdict.PASS


def test_g4_refuses_a_profile_other_than_the_bound_one() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    other = rf.robustness_input(
        trials,
        rf.best(trials).params,
        {"lookback": rf.LOOKBACKS},
        profile=rf.G4_TEST_ONLY_DSR_PROFILE,
    )
    with pytest.raises(ValueError, match="Profile bound"):
        run_validation(_p4_input(), other)


def test_report_view_is_canonical_json_for_visualization() -> None:
    run = run_validation(_p4_input(), _g4_input)
    report = build_report(context(profile=rf.G4_TEST_ONLY_PROFILE), run.gates)
    view = report_view(report, run.robustness)
    text = to_json(view)
    assert text == to_json(json.loads(text))
    loaded = json.loads(text)
    assert loaded["verdict"] == report.verdict.value
    assert loaded["status"] == "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"
    assert set(loaded["stages"]) >= {"G0", "G1", "G2", "G3", "G4", "G5"}
    assert loaded["stages"]["G4"] and not loaded["stages"]["G5"]
    checks = {c["check_id"]: c for c in loaded["robustness"]["checks"]}
    assert {"overfitting", "walk_forward", "state_decomposition", "capacity"} <= set(checks)
    assert checks["walk_forward"]["details"]["windows"]
    assert "param:capacity.max_participation_rate" in loaded["threshold_sources"]["explicit_params"]
    assert "significance.overfitting_threshold" in loaded["threshold_sources"]["profile"]


# ---- ADR-0065 (B67): carry-over remainders left unfilled -------------------------------------

_T = datetime(2026, 1, 1, tzinfo=UTC)
_PARTICIPATION = explicit_threshold("capacity.max_participation_rate", 0.01)


def _remainder(minute: int, remaining: str, *, requested: str = "10") -> FillRemainder:
    requested_q, remaining_q = Decimal(requested), Decimal(remaining)
    return FillRemainder(
        instrument="TEST-USDT",
        decision_time=_T + timedelta(minutes=minute),
        requested_quantity=requested_q,
        filled_quantity=requested_q - remaining_q.copy_sign(requested_q),
        remaining_quantity=remaining_q,
        ended_by="filled" if remaining_q == 0 else "end_of_data",
        ended_at=_T + timedelta(minutes=minute + 1),
    )


def _checked(fills: tuple[CapacityFill, ...], **kwargs: object) -> RobustnessCheck:
    return capacity_check(
        rf.G4_TEST_ONLY_PROFILE,
        fills,
        len(fills),
        max_participation=kwargs.pop("max_participation", _PARTICIPATION),  # type: ignore[arg-type]
        min_capacity=None,
        impact_coefficient=0.1,
        **kwargs,  # type: ignore[arg-type]
    )


def _fills() -> tuple[CapacityFill, ...]:
    _, trials = rf.momentum_family(3, "0.3")
    return rf.capacity_fills(rf.best(trials).returns, Decimal(1_000_000))


def _estimated(check: RobustnessCheck) -> list[tuple[str, str]]:
    return [(g.gate_id, g.metric) for g in check.gates if g.gate_id == "G4.capacity.estimated"]


#: sha256 of this capacity check's gates + details (``_payload_hash``) on the code before B67
#: (``255ce1a``, which had no ``remainders`` parameter), computed twice on that source.
PRE_B67_CAPACITY_PAYLOAD_HASH = "847eefcf40a70729c97c2dcf2bc21bf5d91e29b6cefcdddc089035360f14bf87"


def _payload_hash(check: RobustnessCheck) -> str:
    payload = {
        "gates": [g.model_dump(mode="json") for g in check.gates],
        "details": check.details,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_no_or_zero_remainders_leave_the_capacity_check_unchanged() -> None:
    fills = _fills()
    reference = _checked(fills)
    assert "capacity" in reference.details
    assert _payload_hash(reference) == PRE_B67_CAPACITY_PAYLOAD_HASH  # the pre-B67 payload
    zero = (_remainder(0, "0"), _remainder(5, "0"))
    for remainders in (None, (), zero):
        check = _checked(fills, remainders=remainders)
        assert check == reference
        assert _payload_hash(check) == PRE_B67_CAPACITY_PAYLOAD_HASH


def test_a_positive_remainder_is_inconclusive_and_computes_nothing() -> None:
    fills = _fills()
    remainders = (_remainder(0, "0"), _remainder(3, "2.5"), _remainder(7, "0.5", requested="4"))
    check = _checked(fills, remainders=remainders)
    assert [(g.gate_id, g.metric, g.verdict) for g in check.gates] == [
        ("G4.capacity.estimated", CARRY_OVER_UNFILLED, Verdict.INCONCLUSIVE)
    ]
    assert check.gates[0].value == 2.0
    assert "capacity" not in check.details
    assert "impact_cost_per_period_at_capacity" not in check.details
    assert check.details["carry_over_unfilled"] == {
        "remainders": 2,
        "first": {
            "instrument": "TEST-USDT",
            "decision_time": (_T + timedelta(minutes=3)).isoformat(),
            "requested_quantity": "10",
            "filled_quantity": "7.5",
            "remaining_quantity": "2.5",
            "ended_by": "end_of_data",
            "ended_at": (_T + timedelta(minutes=4)).isoformat(),
        },
    }


def test_carry_over_unfilled_keeps_its_place_among_the_estimated_reasons() -> None:
    fills = _fills()
    unfilled = (_remainder(3, "1"),)
    # the participation limit is still reported first
    missing_limit = _checked(fills, remainders=unfilled, max_participation=None)
    assert _estimated(missing_limit) == [
        ("G4.capacity.estimated", "profile_field_missing:capacity.max_participation_rate")
    ]
    # a volume-source conflict (ADR-0064) comes before it
    first = fills[0]
    conflict = VolumeSourceMismatch("TEST-USDT", first.time, Decimal(1), Decimal(2))
    conflicted = (replace(first, source_mismatch=conflict), *fills[1:])
    assert _estimated(_checked(conflicted, remainders=unfilled)) == [
        ("G4.capacity.estimated", "bar_volume_source_mismatch")
    ]
    # it comes before a missing volume and before no trades
    no_volume = (replace(first, bar_volume_notional=None), *fills[1:])
    assert _estimated(_checked(no_volume, remainders=unfilled)) == [
        ("G4.capacity.estimated", CARRY_OVER_UNFILLED)
    ]
    untraded = tuple(replace(fill, traded_fraction=Decimal(0)) for fill in fills)
    assert _estimated(_checked(untraded)) == [("G4.capacity.estimated", "no_trades")]
    assert _estimated(_checked(untraded, remainders=unfilled)) == [
        ("G4.capacity.estimated", CARRY_OVER_UNFILLED)
    ]
