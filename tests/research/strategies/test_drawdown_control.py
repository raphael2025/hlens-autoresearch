"""``drawdown_control@1.0.0`` (ADR-0085 ``RSK-DD-CONTROL-001``, ADR-0088 decision 3): contract
suite, hand-computed drawdown and scaling, the strict threshold boundary, missing equity / peak
refusals, explicit parameters, and the realized-path run through ``BarBacktester.run_with_risk``
(no look-ahead)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.strategy import (
    BacktestRequest,
    PortfolioState,
    RiskInputError,
    RiskRequest,
    RiskResult,
    SignalObservation,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import RiskPolicy, StrategySpec
from plugins.backtest import BarBacktester, RiskLoopRun, realized_portfolio_state
from research.strategies.drawdown_control import (
    DRAWDOWN_POLICY_REF,
    DRAWDOWN_RULES,
    DrawdownControlRiskProvider,
    drawdown,
    drawdown_control_policy,
)
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import (
    EvaluationInputs,
    EvaluationStatus,
    StrategyCandidate,
    evaluate_strategy,
)
from research.strategies.signals import LOG_RETURN_SIGNAL
from tests.contract_suites.risk import RiskProviderContract, RiskSubject
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars

CUTOFF = T0 + timedelta(days=1)
POLICY = drawdown_control_policy(max_drawdown="0.1", reduced_fraction="0.5")
DECIDED = T0 + 10 * MINUTE


def _target(instrument: str, weight: str) -> TargetPosition:
    return TargetPosition(
        decision_time=DECIDED,
        instrument=instrument,
        target_weight=Decimal(weight),
        inputs_used=1,
        latest_input_available_time=DECIDED,
    )


def _request(
    *,
    equity: str | None,
    peak: str | None,
    weights: Sequence[tuple[str, str]] = (("BTCUSDT", "0.8"), ("ETHUSDT", "-0.3")),
    policy: RiskPolicy = POLICY,
    signals: tuple[SignalObservation, ...] = (),
) -> RiskRequest:
    return RiskRequest(
        policy=policy.ref,
        policy_hash=policy.content_hash(),
        decision_time=DECIDED,
        knowledge_cutoff=CUTOFF,
        targets=tuple(_target(name, weight) for name, weight in weights),
        portfolio=PortfolioState(
            as_of=DECIDED,
            equity=None if equity is None else Decimal(equity),
            peak_equity=None if peak is None else Decimal(peak),
        ),
        signals=signals,
    )


def _run(request: RiskRequest, policy: RiskPolicy = POLICY) -> RiskResult:
    provider = DrawdownControlRiskProvider((policy,))
    result = provider.constrain(request)
    result.check_answers(request, provider.descriptor)
    return result


def _weights(result: RiskResult) -> list[tuple[Decimal, tuple[str, ...]]]:
    return [(item.constrained_weight, item.binding_rules) for item in result.positions]


class TestDrawdownControlContract(RiskProviderContract):
    @pytest.fixture
    def risk_subject(self) -> RiskSubject:
        other = drawdown_control_policy(max_drawdown="0.2", reduced_fraction="0.5")
        return RiskSubject(
            open=lambda: DrawdownControlRiskProvider((POLICY,)),
            requests=(
                _request(equity="800", peak="1000"),
                _request(equity="1000", peak="1000"),
                _request(equity="950", peak="1000"),
            ),
            unsupported=_request(equity="800", peak="1000", policy=other),
            rules=frozenset(DRAWDOWN_RULES),
        )


def test_hand_computed_drawdown() -> None:
    assert drawdown(Decimal(900), Decimal(1000)) == Decimal("0.1")
    assert drawdown(Decimal(1000), Decimal(1000)) == 0
    assert drawdown(Decimal(750), Decimal(1200)) == Decimal("0.375")


def test_breach_scales_every_target_by_the_reduced_fraction() -> None:
    # drawdown = 1 - 800 / 1000 = 0.2 > 0.1 -> x 0.5
    result = _run(_request(equity="800", peak="1000"))
    assert _weights(result) == [
        (Decimal("0.4"), ("drawdown_scaling",)),
        (Decimal("-0.15"), ("drawdown_scaling",)),
    ]
    assert all(item.inputs_used == 0 for item in result.positions)


def test_threshold_is_strict() -> None:
    # exactly at max_drawdown: 1 - 900 / 1000 = 0.1, not > 0.1 -> unchanged
    at = _run(_request(equity="900", peak="1000"))
    assert _weights(at) == [(Decimal("0.8"), ()), (Decimal("-0.3"), ())]
    # just past it
    past = _run(_request(equity="899.999", peak="1000"))
    assert _weights(past) == [
        (Decimal("0.4"), ("drawdown_scaling",)),
        (Decimal("-0.15"), ("drawdown_scaling",)),
    ]


def test_no_drawdown_leaves_targets_unchanged() -> None:
    result = _run(_request(equity="1000", peak="1000"))
    assert _weights(result) == [(Decimal("0.8"), ()), (Decimal("-0.3"), ())]


def test_scaling_rounds_toward_zero() -> None:
    # -3e-18 x 0.5 = -1.5e-18 -> toward zero -1e-18 (half-even would give -2e-18)
    result = _run(_request(equity="500", peak="1000", weights=(("BTCUSDT", "-3E-18"),)))
    assert _weights(result) == [(Decimal("-1E-18"), ("drawdown_scaling",))]


def test_zero_fraction_flattens_and_zero_targets_are_not_adjusted() -> None:
    flat = drawdown_control_policy(max_drawdown="0.1", reduced_fraction="0")
    result = _run(
        _request(
            equity="500",
            peak="1000",
            weights=(("BTCUSDT", "0.8"), ("ETHUSDT", "0")),
            policy=flat,
        ),
        flat,
    )
    assert _weights(result) == [(Decimal(0), ("drawdown_scaling",)), (Decimal(0), ())]
    assert not result.positions[0].constrained_weight.is_signed()


def test_missing_equity_is_refused() -> None:
    with pytest.raises(RiskInputError, match="equity"):
        DrawdownControlRiskProvider((POLICY,)).constrain(_request(equity=None, peak=None))


def test_missing_peak_equity_is_refused() -> None:
    with pytest.raises(RiskInputError, match="peak_equity"):
        DrawdownControlRiskProvider((POLICY,)).constrain(_request(equity="900", peak=None))


def test_visible_signals_do_not_change_the_answer() -> None:
    signal = SignalObservation(
        signal=Ref(kind=Kind.FEATURE, name="bar_realized_vol_60", version="1.0.0"),
        instrument="BTCUSDT",
        event_time=T0,
        available_time=T0,
        knowledge_time=T0,
        value=Decimal("0.02"),
    )
    plain = _run(_request(equity="800", peak="1000"))
    noisy = _run(_request(equity="800", peak="1000", signals=(signal,)))
    assert _weights(plain) == _weights(noisy)


@pytest.mark.parametrize(
    ("max_drawdown", "reduced_fraction"),
    [("0", "0.5"), ("1", "0.5"), ("-0.1", "0.5"), ("0.1", "1"), ("0.1", "-0.1"), ("x", "0.5")],
)
def test_out_of_range_parameters_are_refused(max_drawdown: str, reduced_fraction: str) -> None:
    with pytest.raises(ValueError):
        drawdown_control_policy(max_drawdown=max_drawdown, reduced_fraction=reduced_fraction)


def _hand_policy(params: dict[str, str | int | float | bool]) -> RiskPolicy:
    return RiskPolicy(
        name=DRAWDOWN_POLICY_REF.name,
        version=DRAWDOWN_POLICY_REF.version,
        rules=DRAWDOWN_RULES,
        params=FrozenMapping(params),
    )


def test_parameters_have_no_default() -> None:
    with pytest.raises(ValueError, match="explicitly"):
        DrawdownControlRiskProvider((_hand_policy({"max_drawdown": "0.1"}),))
    with pytest.raises(ValueError, match="unknown"):
        DrawdownControlRiskProvider(
            (_hand_policy({"max_drawdown": "0.1", "reduced_fraction": "0.5", "floor": "1"}),)
        )
    with pytest.raises(ValueError, match="at least one"):
        DrawdownControlRiskProvider(())
    with pytest.raises(ValueError, match="more than once"):
        DrawdownControlRiskProvider((POLICY, POLICY))


# ---------------------------------------------------------------------------------------------
# realized equity path: BarBacktester.run_with_risk (ADR-0088 decision 3)
# ---------------------------------------------------------------------------------------------

UP_DOWN = tuple(Decimal(value) for value in ("100", "110", "120", "90", "80", "100", "130"))


def _long_targets(count: int) -> tuple[TargetPosition, ...]:
    return tuple(
        TargetPosition(
            decision_time=T0 + minute * MINUTE,
            instrument="BTCUSDT",
            target_weight=Decimal(1),
            inputs_used=1,
            latest_input_available_time=T0 + minute * MINUTE,
        )
        for minute in range(count)
    )


def _loop(closes: Sequence[Decimal]) -> tuple[BacktestRequest, RiskLoopRun]:
    request = BacktestRequest(
        cost_model=COSTS,
        initial_equity=Decimal(10000),
        bars=make_bars("BTCUSDT", closes),
        targets=_long_targets(len(closes)),
    )
    provider = DrawdownControlRiskProvider((POLICY,))
    run = BarBacktester().run_with_risk(
        request,
        risk=provider,
        policy=POLICY.ref,
        policy_hash=POLICY.content_hash(),
        knowledge_cutoff=CUTOFF,
        signals=(),
    )
    run.result.check_answers(run.request, BarBacktester().descriptor)
    return request, run


def test_backtest_drives_drawdown_control_from_the_realized_path() -> None:
    request, run = _loop(UP_DOWN)
    assert run.result == BarBacktester().run(run.request)
    states = run.portfolio_states
    for state in states:
        assert state == realized_portfolio_state(
            request.initial_equity, run.result.equity_curve, state.as_of, state.current_weights
        )
    # the drop from 120 to 80 breaches 10 %: a later target is halved
    scaled = [
        position
        for result in run.risk_results
        for position in result.positions
        if position.binding_rules == ("drawdown_scaling",)
    ]
    assert scaled
    assert all(position.constrained_weight == Decimal("0.5") for position in scaled)
    for state, result in zip(states, run.risk_results, strict=True):
        assert state.equity is not None and state.peak_equity is not None
        breached = drawdown(state.equity, state.peak_equity) > Decimal("0.1")
        assert breached == bool(result.positions[0].binding_rules)


def test_future_prices_do_not_change_earlier_risk_decisions() -> None:
    _, base = _loop(UP_DOWN)
    moved_closes = (*UP_DOWN[:4], Decimal(200), Decimal(60), Decimal(300))
    _, moved = _loop(moved_closes)
    # decisions at t0 .. t4 only see equity points ending at or before t4 (bars 0 .. 3)
    assert base.portfolio_states[:5] == moved.portfolio_states[:5]
    assert base.risk_results[:5] == moved.risk_results[:5]
    assert base.portfolio_states != moved.portfolio_states


# ---------------------------------------------------------------------------------------------
# PM F-A / ADR-0088 decision 3: research.strategies.pipeline now wires its risk step through
# BarBacktester.run_with_risk (above) instead of running risk ahead of the simulation with
# PortfolioState.equity=None, which previously forced this path-dependent policy to fail closed
# inside evaluate_strategy / CandidateTrialRunner every time. These tests exercise that pipeline
# entry point directly (not BarBacktester.run_with_risk, already covered above).
# ---------------------------------------------------------------------------------------------

_PIPELINE_SPEC = StrategySpec(
    name="test_pipeline_constant_long",
    version="1.0.0",
    created_at=T0,
    signals=(LOG_RETURN_SIGNAL,),
    risk_policy=DRAWDOWN_POLICY_REF,
)


class _ConstantLongStrategy:
    """TEST ONLY: always targets a full long position, one ``TargetPosition`` per decision time ×
    instrument matching ``_long_targets`` exactly, so the pipeline's upstream targets line up with
    what ``_loop`` above feeds ``run_with_risk`` directly."""

    def __init__(self, spec: StrategySpec) -> None:
        self._descriptor = StrategyProviderDescriptor(
            name="test_constant_long",
            version="1.0.0",
            deterministic=True,
            supported_strategies=FrozenMapping({str(spec.ref): spec.content_hash()}),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        positions = [
            TargetPosition(
                decision_time=t,
                instrument=name,
                target_weight=Decimal(1),
                inputs_used=1,
                latest_input_available_time=t,
            )
            for t in request.decision_times
            for name in request.instruments
        ]
        return StrategyResult.build(request, self._descriptor, positions)


def _pipeline_candidate() -> StrategyCandidate:
    return StrategyCandidate(
        spec=_PIPELINE_SPEC,
        strategy=_ConstantLongStrategy(_PIPELINE_SPEC),
        hypothesis_family_id="test_drawdown_control_pipeline",
        risk_policy=POLICY,
        risk=DrawdownControlRiskProvider((POLICY,)),
    )


def _pipeline_inputs(closes: Sequence[Decimal]) -> EvaluationInputs:
    decisions = tuple(T0 + minute * MINUTE for minute in range(len(closes)))
    signals = tuple(
        SignalObservation(
            signal=LOG_RETURN_SIGNAL,
            instrument="BTCUSDT",
            event_time=t,
            available_time=t,
            knowledge_time=t,
            value=Decimal("0"),
        )
        for t in decisions
    )
    return EvaluationInputs(
        instruments=("BTCUSDT",),
        bars=make_bars("BTCUSDT", closes),
        decision_times=decisions,
        knowledge_cutoff=CUTOFF,
        cost_model=COSTS,
        initial_equity=Decimal(10000),
        signals=signals,
    )


def test_pipeline_wires_drawdown_control_through_run_with_risk(tmp_path: Path) -> None:
    """``evaluate_strategy`` no longer runs risk ahead of the simulation: it reaches exactly the
    ``_loop(UP_DOWN)`` result above (same targets, same policy, same signals), so it never fails
    closed and the same drawdown-triggered scaling shows up through the pipeline entry point."""
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        _pipeline_candidate(),
        _pipeline_inputs(UP_DOWN),
        backtester=BarBacktester(),
        registry=registry,
    )

    assert result.failure is None
    assert result.status is EvaluationStatus.NOT_VALIDATED  # no validator given: never FAILED
    assert registry.records() == ()
    assert result.strategy_result is not None
    assert result.strategy_result.positions == _long_targets(len(UP_DOWN))

    _, run = _loop(UP_DOWN)  # the plugin-level run this module already verified
    assert result.risk_results == run.risk_results
    assert result.backtest == run.result

    scaled = [
        position
        for risk_result in result.risk_results
        for position in risk_result.positions
        if position.binding_rules == ("drawdown_scaling",)
    ]
    assert scaled
    assert all(position.constrained_weight == Decimal("0.5") for position in scaled)


def test_pipeline_risk_decisions_use_only_realized_history(tmp_path: Path) -> None:
    """Through the pipeline too (mirrors ``test_future_prices_do_not_change_earlier_risk_decisions``
    above at the ``BarBacktester.run_with_risk`` layer): a price change after t4 never moves the
    risk decisions the pipeline already made at or before t4."""
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    base = evaluate_strategy(
        _pipeline_candidate(),
        _pipeline_inputs(UP_DOWN),
        backtester=BarBacktester(),
        registry=registry,
    )
    moved_closes = (*UP_DOWN[:4], Decimal(200), Decimal(60), Decimal(300))
    moved = evaluate_strategy(
        _pipeline_candidate(),
        _pipeline_inputs(moved_closes),
        backtester=BarBacktester(),
        registry=registry,
    )
    assert base.risk_results[:5] == moved.risk_results[:5]
    assert base.risk_results != moved.risk_results
