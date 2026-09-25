"""Provider-agnostic contract suite for ``BacktestProvider`` (ADR-0038, Phase 5).

A compliant backtester is simulation only and deterministic, answers with a ``BacktestResult`` that
survives re-validation and ``check_answers`` (every fill at the execution-model bar's open, never
before its decision), and passes the **benchmark consistency** checks on the subject's bars:

- buy-and-hold with zero costs equals ``initial_equity × last_close / first_open``;
- buy-and-hold with costs equals the closed form of one buy at ``open × (1 + slippage)`` paying
  ``fee_rate`` on the fill notional;
- zero targets give zero PnL, exactly, whatever the prices do;
- a round trip on flat prices loses exactly its fees and slippage;
- no look-ahead: changing bars after ``t`` leaves every equity point up to ``t`` unchanged.

Equalities are compared after quantizing both sides to ``tolerance`` (the backtester's documented
monetary quantum is finer), so they hold for any exact-``Decimal`` implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext

import pytest

from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    TargetPosition,
)
from tests.contract_suites._support import call_ok, require, revalidated

__all__ = ["BACKTEST_CHECKS", "BacktestCheck", "BacktestProviderContract", "BacktestSubject"]

ZERO_COST = BacktestCostModel(
    name="zero_cost", version="1.0.0", fee_rate=Decimal(0), slippage_rate=Decimal(0)
)


@dataclass(frozen=True)
class BacktestSubject:
    """``bars``: one instrument, one aligned grid, at least four bars; ``costs`` is non-zero."""

    open: Callable[[], BacktestProvider]
    bars: tuple[PriceBar, ...]
    costs: BacktestCostModel
    initial_equity: Decimal
    tolerance: Decimal


type BacktestCheck = Callable[[BacktestSubject], None]


def _run(provider: BacktestProvider, request: BacktestRequest) -> BacktestResult:
    result = call_ok("run", lambda: provider.run(request))
    result = revalidated(BacktestResult, result, "run result")
    call_ok("check_answers", lambda: result.check_answers(request, provider.descriptor))
    return result


def _target(bar: PriceBar, weight: Decimal) -> TargetPosition:
    """A target decided when the bar opens (it executes at this bar's open)."""
    return TargetPosition(
        decision_time=bar.interval_start,
        instrument=bar.instrument,
        target_weight=weight,
        inputs_used=1,
        latest_input_available_time=bar.interval_start,
    )


def _request(
    subject: BacktestSubject, targets: tuple[TargetPosition, ...], costs: BacktestCostModel
) -> BacktestRequest:
    return BacktestRequest(
        cost_model=costs, initial_equity=subject.initial_equity, bars=subject.bars, targets=targets
    )


def _same(left: Decimal, right: Decimal, subject: BacktestSubject, what: str) -> None:
    require(
        left.quantize(subject.tolerance) == right.quantize(subject.tolerance),
        f"{what}: {left} != {right} at {subject.tolerance}",
    )


def check_descriptor(subject: BacktestSubject) -> None:
    descriptor = subject.open().descriptor
    revalidated(BacktestProviderDescriptor, descriptor, "descriptor")
    require(descriptor.simulation_only is True, "a backtester is simulation only")


def check_determinism(subject: BacktestSubject) -> None:
    request = _request(subject, (_target(subject.bars[0], Decimal(1)),), subject.costs)
    first = _run(subject.open(), request)
    second = _run(subject.open(), request)
    require(first.result_hash == second.result_hash, "equal requests must give equal results")


def check_buy_and_hold_is_price_ratio(subject: BacktestSubject) -> None:
    first, last = subject.bars[0], subject.bars[-1]
    result = _run(subject.open(), _request(subject, (_target(first, Decimal(1)),), ZERO_COST))
    with localcontext() as ctx:
        ctx.prec = 60
        expected = subject.initial_equity * last.close / first.open
    _same(result.final_equity, expected, subject, "buy-and-hold vs price ratio")
    require(len(result.fills) == 1 and result.total_fees == 0, "one fill, no fees")


def check_buy_and_hold_with_costs(subject: BacktestSubject) -> None:
    first, last = subject.bars[0], subject.bars[-1]
    fee, slip = subject.costs.fee_rate, subject.costs.slippage_rate
    result = _run(subject.open(), _request(subject, (_target(first, Decimal(1)),), subject.costs))
    with localcontext() as ctx:
        ctx.prec = 60
        quantity = subject.initial_equity / first.open
        paid = quantity * first.open * (1 + slip) * (1 + fee)
        expected = subject.initial_equity - paid + quantity * last.close
        expected_fee = quantity * first.open * (1 + slip) * fee
        expected_slippage = quantity * first.open * slip
    _same(result.final_equity, expected, subject, "buy-and-hold with costs")
    _same(result.total_fees, expected_fee, subject, "fees")
    _same(result.total_slippage, expected_slippage, subject, "slippage")


def check_zero_positions_zero_pnl(subject: BacktestSubject) -> None:
    targets = tuple(_target(bar, Decimal(0)) for bar in subject.bars)
    result = _run(subject.open(), _request(subject, targets, subject.costs))
    require(result.pnl == 0, f"zero positions must give zero PnL, got {result.pnl}")
    require(not result.fills and result.total_fees == 0, "zero positions must not trade")
    require(
        all(point.equity == subject.initial_equity for point in result.equity_curve),
        "equity must stay at initial equity",
    )


def check_round_trip_on_flat_prices_costs_only(subject: BacktestSubject) -> None:
    template = subject.bars[0]
    price = template.open
    flat = tuple(
        bar.model_copy(update={"open": price, "high": price, "low": price, "close": price})
        for bar in subject.bars
    )
    flat_subject = BacktestSubject(
        subject.open, flat, subject.costs, subject.initial_equity, subject.tolerance
    )
    targets = (_target(flat[0], Decimal(1)), _target(flat[1], Decimal(0)))
    result = _run(subject.open(), _request(flat_subject, targets, subject.costs))
    require(len(result.fills) == 2, "a round trip is two fills")
    with localcontext() as ctx:
        ctx.prec = 60
        costs = result.total_fees + result.total_slippage
    require(costs > 0, "the subject's cost model must be non-zero")
    _same(result.pnl, -costs, subject, "round trip on flat prices loses exactly its costs")


def check_no_look_ahead(subject: BacktestSubject) -> None:
    targets = tuple(
        _target(bar, Decimal(1) if index % 2 == 0 else Decimal(-1))
        for index, bar in enumerate(subject.bars)
    )
    base = _run(subject.open(), _request(subject, targets, subject.costs))
    cut = len(subject.bars) // 2
    changed = subject.bars[:cut] + tuple(
        bar.model_copy(
            update={
                "open": bar.open * 2,
                "high": bar.high * 2,
                "low": bar.low * 2,
                "close": bar.close * 2,
            }
        )
        for bar in subject.bars[cut:]
    )
    moved_subject = BacktestSubject(
        subject.open, changed, subject.costs, subject.initial_equity, subject.tolerance
    )
    moved = _run(subject.open(), _request(moved_subject, targets, subject.costs))
    horizon = subject.bars[cut - 1].interval_end
    require(
        [p for p in base.equity_curve if p.time <= horizon]
        == [p for p in moved.equity_curve if p.time <= horizon],
        "equity up to t changed when only later bars changed",
    )
    require(
        all(fill.fill_time >= fill.decision_time for fill in base.fills),
        "no fill may precede its decision",
    )


BACKTEST_CHECKS: tuple[BacktestCheck, ...] = (
    check_descriptor,
    check_determinism,
    check_buy_and_hold_is_price_ratio,
    check_buy_and_hold_with_costs,
    check_zero_positions_zero_pnl,
    check_round_trip_on_flat_prices_costs_only,
    check_no_look_ahead,
)


class BacktestProviderContract:
    """pytest entry point: subclass as ``Test*`` and provide ``backtest_subject``."""

    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        raise NotImplementedError("subclasses provide backtest_subject")

    @pytest.mark.parametrize("check", BACKTEST_CHECKS, ids=lambda check: check.__name__)
    def test_backtest_contract(
        self, backtest_subject: BacktestSubject, check: BacktestCheck
    ) -> None:
        check(backtest_subject)
