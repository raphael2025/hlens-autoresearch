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

The checks are chosen by the execution model the provider **declares** (ADR-0054 §2):
``BacktestProviderContract`` runs ``BACKTEST_CHECKS`` for ``next_bar_open`` (its descriptor check
refuses any other model), ``CarryOverBacktestProviderContract`` runs ``CARRY_OVER_CHECKS`` for
``next_bar_open_participation`` — the model-agnostic checks (descriptor, determinism, zero
positions, no look-ahead) plus the carry-over semantics:

- a weight-one target at the first bar is sized once against the pre-trade equity, fills in several
  bars (never two at one bar), and its fills sum exactly to the request (``ended_by = filled``);
- a later target supersedes the remainder at its own execution bar and is sized against the
  actual holdings (re-targeting); the old remainder is cancelled with a positive remainder;
- a remainder still open at the instrument's last bar ends ``end_of_data`` there;
- no look-ahead: changing prices **and volumes** after ``t`` leaves every fill, every ended
  remainder and every equity point before ``t`` unchanged.

A carry-over subject's bars all carry ``volume``, the cap binds (a weight-one target cannot fill
in a single bar) and the bars suffice to fill a weight-one target at the first bar.
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
    FillRemainder,
    PriceBar,
    TargetPosition,
)
from tests.contract_suites._support import call_ok, require, revalidated

__all__ = [
    "BACKTEST_CHECKS",
    "CARRY_OVER_CHECKS",
    "BacktestCheck",
    "BacktestProviderContract",
    "BacktestSubject",
    "CarryOverBacktestProviderContract",
]

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
    require(
        descriptor.execution_model == "next_bar_open",
        f"these checks are for next_bar_open; a {descriptor.execution_model} provider runs "
        "CarryOverBacktestProviderContract",
    )


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


# ---------------------------------------------------------------------------------------
# next_bar_open_participation (ADR-0054): the remainder carries over to later bars
# ---------------------------------------------------------------------------------------


def _record(result: BacktestResult, target: TargetPosition) -> FillRemainder:
    found = [
        item
        for item in result.remainders
        if (item.decision_time, item.instrument) == (target.decision_time, target.instrument)
    ]
    require(len(found) == 1, f"one carry-over record per sized target, got {len(found)}")
    return found[0]


def check_carry_over_descriptor(subject: BacktestSubject) -> None:
    descriptor = subject.open().descriptor
    revalidated(BacktestProviderDescriptor, descriptor, "descriptor")
    require(descriptor.simulation_only is True, "a backtester is simulation only")
    require(
        descriptor.execution_model == "next_bar_open_participation",
        f"these checks are for next_bar_open_participation, not {descriptor.execution_model}",
    )
    require(
        all(bar.volume is not None for bar in subject.bars),
        "a carry-over subject's bars all carry volume",
    )


def check_carry_over_fills_sum_to_the_target(subject: BacktestSubject) -> None:
    first = subject.bars[0]
    target = _target(first, Decimal(1))
    result = _run(subject.open(), _request(subject, (target,), subject.costs))
    record = _record(result, target)
    require(len(result.fills) >= 2, "the subject's cap must bind: one bar cannot fill the target")
    times = [fill.fill_time for fill in result.fills]
    require(times == sorted(set(times)), "at most one fill per bar, in time order")
    require(times[0] == first.interval_start, "the first fill is at the execution bar")
    require(all(fill.quantity > 0 for fill in result.fills), "every fill is in the target's way")
    with localcontext() as ctx:
        ctx.prec = 120
        total = sum((fill.quantity for fill in result.fills), Decimal(0))
        sized = record.requested_quantity * first.open
    _same(sized, subject.initial_equity, subject, "sized once against the pre-trade equity")
    require(record.ended_by == "filled", f"the subject's bars must fill it, got {record.ended_by}")
    require(total == record.requested_quantity, "the fills sum exactly to the request")
    require(record.remaining_quantity == 0, "nothing remains once filled")
    require(record.ended_at == times[-1], "a filled remainder ends at its last fill")
    require(result.unexecuted_targets == 0, "the target executed")


def check_carry_over_superseded_by_a_later_target(subject: BacktestSubject) -> None:
    first, second = subject.bars[0], subject.bars[1]
    old, new = _target(first, Decimal(1)), _target(second, Decimal(0))
    result = _run(subject.open(), _request(subject, (old, new), subject.costs))
    old_record = _record(result, old)
    require(old_record.ended_by == "superseded", f"got {old_record.ended_by}")
    require(old_record.ended_at == second.interval_start, "superseded at the new execution bar")
    require(old_record.remaining_quantity > 0, "the cancelled remainder is positive")
    bought = [fill for fill in result.fills if fill.decision_time == old.decision_time]
    require(
        [fill.fill_time for fill in bought] == [first.interval_start],
        "the old target fills only before the new target's execution bar",
    )
    new_record = _record(result, new)
    require(
        new_record.requested_quantity == -bought[0].quantity,
        "the new target is sized against the actual holdings",
    )
    sold = [fill for fill in result.fills if fill.decision_time == new.decision_time]
    require(bool(sold) and all(fill.quantity < 0 for fill in sold), "flattening sells")


def check_carry_over_ends_at_the_end_of_data(subject: BacktestSubject) -> None:
    last = subject.bars[-1]
    target = _target(last, Decimal(1))
    result = _run(subject.open(), _request(subject, (target,), subject.costs))
    record = _record(result, target)
    require(record.ended_by == "end_of_data", f"got {record.ended_by}")
    require(record.ended_at == last.interval_start, "end_of_data ends at the last bar")
    require(record.remaining_quantity > 0, "the cap binds, so something remains")


def check_carry_over_no_look_ahead(subject: BacktestSubject) -> None:
    targets = (
        _target(subject.bars[0], Decimal(1)),
        _target(subject.bars[len(subject.bars) // 2], Decimal(-1)),
    )
    base = _run(subject.open(), _request(subject, targets, subject.costs))
    cut = len(subject.bars) // 2 + 1
    changed = subject.bars[:cut] + tuple(
        bar.model_copy(
            update={
                "open": bar.open * 2,
                "high": bar.high * 2,
                "low": bar.low * 2,
                "close": bar.close * 2,
                "volume": None if bar.volume is None else bar.volume * 7,
            }
        )
        for bar in subject.bars[cut:]
    )
    moved_subject = BacktestSubject(
        subject.open, changed, subject.costs, subject.initial_equity, subject.tolerance
    )
    moved = _run(subject.open(), _request(moved_subject, targets, subject.costs))
    before = subject.bars[cut].interval_start
    horizon = subject.bars[cut - 1].interval_end
    require(
        [p for p in base.equity_curve if p.time <= horizon]
        == [p for p in moved.equity_curve if p.time <= horizon],
        "equity up to t changed when only later bars changed",
    )
    require(
        [f for f in base.fills if f.fill_time < before]
        == [f for f in moved.fills if f.fill_time < before],
        "fills before t changed when only later bars changed",
    )
    require(
        [r for r in base.remainders if r.ended_at < before]
        == [r for r in moved.remainders if r.ended_at < before],
        "remainders ended before t changed when only later bars changed",
    )
    require(
        all(fill.fill_time >= fill.decision_time for fill in base.fills),
        "no fill may precede its decision",
    )


CARRY_OVER_CHECKS: tuple[BacktestCheck, ...] = (
    check_carry_over_descriptor,
    check_determinism,
    check_zero_positions_zero_pnl,
    check_no_look_ahead,
    check_carry_over_fills_sum_to_the_target,
    check_carry_over_superseded_by_a_later_target,
    check_carry_over_ends_at_the_end_of_data,
    check_carry_over_no_look_ahead,
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


class CarryOverBacktestProviderContract:
    """pytest entry point for a ``next_bar_open_participation`` provider (ADR-0054): subclass as
    ``Test*`` and provide ``backtest_subject`` (bars with volume, a binding cap)."""

    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        raise NotImplementedError("subclasses provide backtest_subject")

    @pytest.mark.parametrize("check", CARRY_OVER_CHECKS, ids=lambda check: check.__name__)
    def test_carry_over_contract(
        self, backtest_subject: BacktestSubject, check: BacktestCheck
    ) -> None:
        check(backtest_subject)
