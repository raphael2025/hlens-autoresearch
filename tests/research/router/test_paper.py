"""W1 wiring: router weights x P5 target positions -> the router's own paper backtest."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from core.contracts.state import (
    StateInput,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
)
from core.contracts.strategy import (
    BacktestCostModel,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.lifecycle.strategy import LifecycleState
from plugins.backtest import BarBacktester
from research.router import (
    ROUTER_PAPER_BACKTEST,
    RouterError,
    RouterPaperRun,
    RouterSpec,
    StrategyRouter,
    paper_run,
)
from tests.strategy_fixtures import MINUTE, T0, make_bars

A = Ref(kind=Kind.STRATEGY, name="trend_a", version="1.0.0")
B = Ref(kind=Kind.STRATEGY, name="revert_b", version="1.0.0")
STATE = Ref(kind=Kind.STATE, name="vol_regime", version="1.0.0")
LIFECYCLE = {A: LifecycleState.ACTIVE, B: LifecycleState.PRODUCTION_CANDIDATE}
ZERO = BacktestCostModel(
    name="zero_cost", version="1.0.0", fee_rate=Decimal(0), slippage_rate=Decimal(0)
)
BARS = make_bars("BTC", [Decimal(c) for c in ("100", "100", "110", "121", "110", "99", "99")])


def _t(minute: int) -> datetime:
    return T0 + minute * MINUTE


def _strategy(ref: Ref, weights: dict[int, str]) -> StrategyResult:
    times = tuple(_t(m) for m in sorted(weights))
    request = StrategyRequest(
        strategy=ref,
        spec_hash=content_hash({"fixture": ref.name}),
        instruments=("BTC",),
        knowledge_cutoff=_t(100),
        decision_times=times,
        signals=(),
    )
    descriptor = StrategyProviderDescriptor(
        name="fixture_strategies",
        version="1.0.0",
        deterministic=True,
        supported_strategies=FrozenMapping({str(ref): request.spec_hash}),
    )
    positions = [
        TargetPosition(
            decision_time=_t(m),
            instrument="BTC",
            target_weight=Decimal(weights[m]),
            inputs_used=1,
            latest_input_available_time=_t(m),
        )
        for m in sorted(weights)
    ]
    return StrategyResult.build(request, descriptor, positions)


def _states(labels: dict[int, str | None]) -> StateResult:
    feature = Ref(kind=Kind.FEATURE, name="bar_realized_vol_5", version="1.0.0")
    times = tuple(_t(m) for m in sorted(labels))
    request = StateRequest(
        state=STATE,
        spec_hash=content_hash({"fixture": "state"}),
        evaluation_times=times,
        inputs=tuple(
            StateInput(
                feature=feature,
                evaluation_time=t,
                value=Decimal(1),
                source_result_hash=content_hash({"fixture": "vol"}),
            )
            for t in times
        ),
    )
    descriptor = StateProviderDescriptor(
        name="fixture_states",
        version="1.0.0",
        deterministic=True,
        supported_states=FrozenMapping({str(STATE): request.spec_hash}),
    )
    values = [
        StateValue(evaluation_time=_t(m), state=None, inputs_used=0)
        if labels[m] is None
        else StateValue(
            evaluation_time=_t(m), state=labels[m], inputs_used=1, latest_input_time=_t(m)
        )
        for m in sorted(labels)
    ]
    return StateResult.build(request, descriptor, values)


def _router(rate: str = "0.01") -> StrategyRouter:
    spec = RouterSpec(
        name="vol_router",
        version="1.0.0",
        table={"calm": {str(A): Decimal(1)}, "wild": {str(B): Decimal("0.5")}},
        fallback={},
        switching_cost_rate=Decimal(rate),
    )
    return StrategyRouter(spec, LIFECYCLE)


# A decides at minutes 1 and 3 only (as-of in between); B decides every minute.
STRATEGIES = {
    A: _strategy(A, {1: "1", 3: "1"}),
    B: _strategy(B, {1: "-1", 2: "-1", 3: "-1", 4: "-1", 5: "-1"}),
}
STATES = _states({1: "calm", 2: "calm", 3: "wild", 4: "wild", 5: None})


def _run(rate: str = "0.01") -> RouterPaperRun:
    return paper_run(
        _router(rate),
        STATES,
        STRATEGIES,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
    )


def test_combined_targets_follow_routing_weights_and_as_of_positions() -> None:
    run = paper_run(
        _router(),
        STATES,
        STRATEGIES,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
    )
    weights = [(t.decision_time, t.target_weight, t.inputs_used) for t in run.targets]
    assert weights == [
        (_t(1), Decimal(1), 1),
        (_t(2), Decimal(1), 1),  # A has no decision at 2: its minute-1 position (as-of)
        (_t(3), Decimal("-0.5"), 1),
        (_t(4), Decimal("-0.5"), 1),
        (_t(5), Decimal(0), 0),  # unknown state -> flat fallback, no information used
    ]
    assert [d.turnover for d in run.decisions] == [
        Decimal(1),
        Decimal(0),
        Decimal("1.5"),
        Decimal(0),
        Decimal("0.5"),
    ]


def test_switching_costs_are_paid_from_the_book_after_each_decision() -> None:
    run = paper_run(
        _router(),
        STATES,
        STRATEGIES,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
    )
    # gross marks (zero cost): t1 1000 (flat), t2 1000 (long 10 @ 100), t3 1100,
    # t4 1045 (short 5 @ 110, bar closes 121), then a rebalance and the flat fallback.
    assert [p.equity for p in run.gross.equity_curve[:4]] == [
        Decimal(1000),
        Decimal(1000),
        Decimal(1100),
        Decimal(1045),
    ]
    at_t5 = run.gross.equity_curve[4].equity
    first, second, third = run.charges
    assert (first.decision_time, first.equity_base, first.charged_at) == (
        _t(1),
        Decimal(1000),
        _t(2),
    )
    assert first.amount == Decimal(10)  # 0.01 x turnover 1 x 1000
    assert (second.equity_base, second.amount, second.charged_at) == (
        Decimal(1090),  # net mark at t3: 1100 - 10
        Decimal("16.35"),  # 0.01 x 1.5 x 1090
        _t(4),
    )
    assert third.charged_at == _t(6)
    assert third.equity_base == at_t5 - Decimal(10) - Decimal("16.35")
    assert third.amount == (Decimal("0.005") * third.equity_base).quantize(Decimal("1e-18"))
    assert run.total_switching_cost == first.amount + second.amount + third.amount
    assert run.result.final_equity == run.gross.final_equity - run.total_switching_cost
    assert run.result.fills == run.gross.fills
    assert run.result.provider == ROUTER_PAPER_BACKTEST.plugin_key
    run.result.check_answers(run.request, ROUTER_PAPER_BACKTEST)


def test_the_run_links_every_input_and_is_deterministic() -> None:
    run = _run()
    again = _run()
    assert run == again and run.run_hash == again.run_hash
    assert run.state_result_hash == STATES.result_hash
    assert run.strategy_result_hashes == {
        str(key): value.result_hash for key, value in STRATEGIES.items()
    }
    assert run.result.request_hash == run.request.content_hash()
    assert run.gross.request_hash == run.request.content_hash()
    assert _run("0.02").run_hash != run.run_hash


def test_a_zero_switching_rate_leaves_the_gross_curve() -> None:
    run = _run("0")
    assert run.result.equity_curve == run.gross.equity_curve
    assert run.total_switching_cost == 0


def test_every_routed_strategy_needs_its_positions_and_nothing_else() -> None:
    with pytest.raises(RouterError, match="no StrategyResult"):
        paper_run(
            _router(),
            STATES,
            {A: STRATEGIES[A]},
            bars=BARS,
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            backtester=BarBacktester(),
        )
    stray = Ref(kind=Kind.STRATEGY, name="stray", version="1.0.0")
    with pytest.raises(RouterError, match="never routes"):
        paper_run(
            _router(),
            STATES,
            {**STRATEGIES, stray: _strategy(stray, {1: "1"})},
            bars=BARS,
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            backtester=BarBacktester(),
        )
