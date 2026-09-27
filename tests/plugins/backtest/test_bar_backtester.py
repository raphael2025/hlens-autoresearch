"""Phase 5: the bar-level backtester v1 and its benchmark consistency (ADR-0038)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.contracts.strategy import BacktestRequest, BacktestResult, TargetPosition
from plugins.backtest import MONEY_QUANTUM, BarBacktester
from tests.contract_suites import backtest as backtest_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.backtest import BacktestProviderContract, BacktestSubject
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes


class TestBarBacktester(BacktestProviderContract):
    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        return BacktestSubject(
            open=BarBacktester,
            bars=make_bars("BTCUSDT", wave_closes(12)),
            costs=COSTS,
            initial_equity=Decimal("10000"),
            tolerance=MONEY_QUANTUM * 1000,
        )


def _target(minute: int, weight: str, instrument: str = "BTCUSDT") -> TargetPosition:
    decided = T0 + minute * MINUTE
    return TargetPosition(
        decision_time=decided,
        instrument=instrument,
        target_weight=Decimal(weight),
        inputs_used=1,
        latest_input_available_time=decided,
    )


def test_a_decision_inside_a_bar_executes_at_the_next_open() -> None:
    bars = make_bars("BTCUSDT", wave_closes(5))
    target = _target(0, "1").model_copy(
        update={"decision_time": T0 + MINUTE / 2, "latest_input_available_time": T0}
    )
    result = BarBacktester().run(
        BacktestRequest(
            cost_model=COSTS, initial_equity=Decimal(1000), bars=bars, targets=(target,)
        )
    )
    assert [fill.fill_time for fill in result.fills] == [T0 + MINUTE]
    assert result.fills[0].reference_price == bars[1].open


def test_superseded_and_unpriced_targets_are_counted_unexecuted() -> None:
    bars = make_bars("BTCUSDT", wave_closes(3))
    targets = (
        _target(0, "1").model_copy(update={"decision_time": T0 + MINUTE / 3}),
        _target(0, "-1").model_copy(update={"decision_time": T0 + MINUTE / 2}),
        _target(10, "1"),  # after the last bar
    )
    result = BarBacktester().run(
        BacktestRequest(cost_model=COSTS, initial_equity=Decimal(1000), bars=bars, targets=targets)
    )
    assert len(result.fills) == 1 and result.fills[0].quantity < 0
    assert result.unexecuted_targets == 2


def test_two_instruments_share_one_pre_trade_equity() -> None:
    bars = make_bars("BTCUSDT", wave_closes(6)) + make_bars("ETHUSDT", wave_closes(6, phase=10))
    targets = (_target(0, "0.5"), _target(0, "0.5", "ETHUSDT"))
    result = BarBacktester().run(
        BacktestRequest(cost_model=COSTS, initial_equity=Decimal(1000), bars=bars, targets=targets)
    )
    assert len(result.fills) == 2
    notionals = [abs(fill.quantity) * fill.reference_price for fill in result.fills]
    assert all(n.quantize(Decimal("1e-12")) == Decimal(500) for n in notionals)
    assert len(result.equity_curve) == 6


class _IgnoresCosts(BarBacktester):
    """Faulty: simulates without the request's cost model (an optimistic execution assumption)."""

    def run(self, request: BacktestRequest) -> BacktestResult:
        cheap = super().run(request.model_copy(update={"cost_model": backtest_suite.ZERO_COST}))
        return BacktestResult.build(
            request,
            self.descriptor,
            fills=cheap.fills,
            equity_curve=cheap.equity_curve,
            unexecuted_targets=cheap.unexecuted_targets,
        )


def test_the_backtest_suite_catches_ignored_costs() -> None:
    subject = BacktestSubject(
        open=_IgnoresCosts,
        bars=make_bars("BTCUSDT", wave_closes(12)),
        costs=COSTS,
        initial_equity=Decimal("10000"),
        tolerance=MONEY_QUANTUM * 1000,
    )
    backtest_suite.check_buy_and_hold_is_price_ratio(subject)  # zero-cost benchmark still holds
    with pytest.raises(ContractSuiteFailure):
        backtest_suite.check_buy_and_hold_with_costs(subject)
    with pytest.raises(ContractSuiteFailure):
        backtest_suite.check_round_trip_on_flat_prices_costs_only(subject)
