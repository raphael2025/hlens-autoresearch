"""ADR-0088 decision 3: the backtest supplies ``PortfolioState.equity`` / ``peak_equity`` from the
realized equity path (``BarBacktester.run_with_risk``, ``plugins.backtest.risk_loop``).

Checks: hand-computed path values; ``peak_equity`` never decreases and is never below ``equity``;
each state equals the reference built from the final curve up to ``as_of`` only; later prices never
change an earlier state; the result equals ``run`` on the constrained request (no other backtest
semantics change); targets after the last bar are still constrained; non-positive realized equity
and backwards time are refused."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise

import pytest

from core.contracts.strategy import (
    BacktestInputError,
    BacktestRequest,
    ConstrainedPosition,
    EquityPoint,
    PortfolioState,
    RiskProviderDescriptor,
    RiskRequest,
    RiskResult,
    TargetPosition,
)
from core.domain.base import FrozenMapping
from core.domain.specs import RiskPolicy
from plugins.backtest import (
    MONEY_QUANTUM,
    BarBacktester,
    RealizedEquityPath,
    RiskLoopRun,
    realized_portfolio_state,
)
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars

CUTOFF = T0 + timedelta(days=1)
INITIAL = Decimal(10000)
CLOSES = tuple(Decimal(value) for value in ("100", "110", "120", "90", "80", "100", "130"))


class ScalingRisk:
    """Test double for the injected risk policy (not the logic under test): multiplies every target
    by ``fraction`` and records the ``PortfolioState`` it was given."""

    def __init__(self, fraction: Decimal) -> None:
        self.fraction = fraction
        self.policy = RiskPolicy(
            name="test_scaling",
            version="1.0.0",
            created_at=T0,
            rules=("scale",),
            params=FrozenMapping({"fraction": str(fraction)}),
        )
        self._descriptor = RiskProviderDescriptor(
            name="test_scaling_risk",
            version="0.1.0",
            deterministic=True,
            supported_policies=FrozenMapping({str(self.policy.ref): self.policy.content_hash()}),
        )
        self.seen: list[PortfolioState] = []

    @property
    def descriptor(self) -> RiskProviderDescriptor:
        return self._descriptor

    def constrain(self, request: RiskRequest) -> RiskResult:
        self.seen.append(request.portfolio)
        positions: list[ConstrainedPosition] = []
        for target in request.targets:
            weight = (target.target_weight * self.fraction).quantize(
                MONEY_QUANTUM, rounding=ROUND_DOWN
            )
            if weight == target.target_weight:
                weight = target.target_weight  # keep the upstream representation
            positions.append(
                ConstrainedPosition(
                    instrument=target.instrument,
                    requested_weight=target.target_weight,
                    constrained_weight=weight,
                    binding_rules=("scale",) if weight != target.target_weight else (),
                    inputs_used=0,
                )
            )
        return RiskResult.build(request, self._descriptor, positions)


def _targets(minutes: Sequence[int], weight: str = "1") -> tuple[TargetPosition, ...]:
    return tuple(
        TargetPosition(
            decision_time=T0 + minute * MINUTE,
            instrument="BTCUSDT",
            target_weight=Decimal(weight),
            inputs_used=1,
            latest_input_available_time=T0 + minute * MINUTE,
        )
        for minute in minutes
    )


def _request(closes: Sequence[Decimal], minutes: Sequence[int]) -> BacktestRequest:
    return BacktestRequest(
        cost_model=COSTS,
        initial_equity=INITIAL,
        bars=make_bars("BTCUSDT", closes),
        targets=_targets(minutes),
    )


def _run(request: BacktestRequest, risk: ScalingRisk) -> RiskLoopRun:
    backtester = BarBacktester()
    run = backtester.run_with_risk(
        request,
        risk=risk,
        policy=risk.policy.ref,
        policy_hash=risk.policy.content_hash(),
        knowledge_cutoff=CUTOFF,
        signals=(),
    )
    run.result.check_answers(run.request, backtester.descriptor)
    return run


def test_pass_through_risk_changes_nothing() -> None:
    request = _request(CLOSES, range(len(CLOSES)))
    run = _run(request, ScalingRisk(Decimal(1)))
    assert run.request == request
    assert run.result == BarBacktester().run(request)


def test_constrained_run_equals_run_on_the_constrained_request() -> None:
    request = _request(CLOSES, range(len(CLOSES)))
    run = _run(request, ScalingRisk(Decimal("0.5")))
    assert [item.target_weight for item in run.request.targets] == [Decimal("0.5")] * len(CLOSES)
    assert run.result == BarBacktester().run(run.request)
    assert len(run.risk_results) == len(run.portfolio_states) == len(CLOSES)


def test_states_follow_the_realized_path_by_hand() -> None:
    # One decision at t0 (weight 1), bars 100 -> 100, 100 -> 110; no further trades.
    request = BacktestRequest(
        cost_model=COSTS,
        initial_equity=INITIAL,
        bars=make_bars("BTCUSDT", (Decimal(100), Decimal(110), Decimal(99))),
        targets=(*_targets((0,)), *_targets((1, 2, 3), weight="0")),
    )
    risk = ScalingRisk(Decimal(1))
    run = _run(request, risk)
    first, second, third, fourth = run.portfolio_states
    # t0: nothing realized yet -> initial equity is both value and peak.
    assert (first.equity, first.peak_equity) == (INITIAL, INITIAL)
    # buy 100 @ 100.05, fee 10.005: cash -15.005; bar 0 closes at 100.
    assert (second.equity, second.peak_equity) == (Decimal("9984.995"), INITIAL)
    assert second.current_weights == FrozenMapping({"BTCUSDT": Decimal(1)})
    # t1 target 0 sells 100 @ 99.95 (bar 1 opens 100), fee 9.995: cash 9970, flat.
    assert third.equity == Decimal("9970")
    assert third.peak_equity == INITIAL
    assert fourth.equity == Decimal("9970")
    assert risk.seen == list(run.portfolio_states)


def test_peak_is_monotone_and_uses_only_the_past() -> None:
    request = _request(CLOSES, range(len(CLOSES)))
    run = _run(request, ScalingRisk(Decimal(1)))
    peaks = [state.peak_equity for state in run.portfolio_states if state.peak_equity is not None]
    assert len(peaks) == len(run.portfolio_states)
    assert all(later >= earlier for earlier, later in pairwise(peaks))
    for state in run.portfolio_states:
        assert state.equity is not None and state.peak_equity is not None
        assert state.peak_equity >= state.equity
        realized = [INITIAL] + [
            point.equity for point in run.result.equity_curve if point.time <= state.as_of
        ]
        assert state.equity == realized[-1]
        assert state.peak_equity == max(realized)
        assert state == realized_portfolio_state(
            INITIAL, run.result.equity_curve, state.as_of, state.current_weights
        )
    # the fall from 120 to 90 leaves the peak where it was while equity drops
    below = [state for state in run.portfolio_states if state.equity != state.peak_equity]
    assert below


def test_future_prices_never_change_an_earlier_state() -> None:
    base = _run(_request(CLOSES, range(len(CLOSES))), ScalingRisk(Decimal("0.5")))
    moved_closes = (*CLOSES[:4], Decimal(200), Decimal(60), Decimal(300))
    moved = _run(_request(moved_closes, range(len(CLOSES))), ScalingRisk(Decimal("0.5")))
    # decisions t0 .. t4 see only the equity points of bars 0 .. 3 (ending at or before t4)
    assert base.portfolio_states[:5] == moved.portfolio_states[:5]
    assert base.portfolio_states[5:] != moved.portfolio_states[5:]


def test_targets_after_the_last_bar_are_constrained_and_unexecuted() -> None:
    request = _request(CLOSES[:3], (0, 1, 10))
    run = _run(request, ScalingRisk(Decimal("0.5")))
    assert [state.as_of for state in run.portfolio_states] == [
        T0,
        T0 + MINUTE,
        T0 + 10 * MINUTE,
    ]
    last = run.portfolio_states[-1]
    assert last.equity == run.result.equity_curve[-1].equity
    assert run.result.unexecuted_targets == 1
    assert run.result == BarBacktester().run(run.request)


def test_run_without_risk_is_unchanged() -> None:
    request = _request(CLOSES, range(len(CLOSES)))
    first = BarBacktester().run(request)
    _run(request, ScalingRisk(Decimal("0.5")))
    assert BarBacktester().run(request) == first


def test_non_positive_realized_equity_is_refused() -> None:
    path = RealizedEquityPath(INITIAL)
    curve = (
        EquityPoint(
            time=T0 + MINUTE, cash=Decimal(-5), equity=Decimal(-5), gross_exposure=Decimal(0)
        ),
    )
    path.advance(curve, T0 + MINUTE)
    with pytest.raises(BacktestInputError, match="not positive"):
        path.state(T0 + MINUTE, FrozenMapping({}))


def test_the_path_only_moves_forward() -> None:
    path = RealizedEquityPath(INITIAL)
    path.advance((), T0 + MINUTE)
    with pytest.raises(BacktestInputError, match="forward"):
        path.advance((), T0)
    with pytest.raises(BacktestInputError, match="advance"):
        path.state(T0, FrozenMapping({}))
    with pytest.raises(BacktestInputError, match="positive"):
        RealizedEquityPath(Decimal(0))
