"""Deterministic bar-level backtester v1 (Phase 5; ADR-0038). Simulation only.

``BarBacktester`` implements ``BacktestProvider`` with the single v1 execution model
``next_bar_open``: a target decided at ``t`` is executed at the open of that instrument's first bar
with ``interval_start >= t``. It never places orders, holds no keys and opens no network
connection — it is arithmetic over the request.

Per bar timestamp (all bars sharing one ``interval_start``, instruments in ascending order):

1. admit every target with ``decision_time <= interval_start``; a later target for the same
   instrument supersedes one that has not executed yet (it is counted as unexecuted);
2. mark the book at the open: ``equity_open = cash + sum(quantity * open)``;
3. for each instrument with a pending target: ``desired = weight * equity_open / open`` (all targets
   of one timestamp are sized against the same pre-trade equity; a book with ``equity_open <= 0``
   is only flattened), ``trade = desired - quantity``; a non-zero trade fills at
   ``open * (1 + slippage_rate)`` (buy) or ``open * (1 - slippage_rate)`` (sell), pays
   ``fee = |trade| * fill_price * fee_rate`` and reports
   ``slippage_cost = |trade| * |fill_price - open|``;
4. mark the book at the close into an ``EquityPoint`` at the latest ``interval_end`` of the group.

Instruments without a bar at a timestamp keep their last mark. Cash may go negative (a target
weight above one, or costs on a fully invested book); v1 charges no financing. Arithmetic runs at
50 significant digits, half-even; cash, equity, exposure, fees and slippage are quantized to
``MONEY_QUANTUM``. Quantities are kept at full working precision. The same request always gives
the same ``result_hash``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import groupby
from typing import Final

from core.contracts.strategy import (
    BacktestInputError,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    Fill,
    PriceBar,
    TargetPosition,
)

__all__ = ["MONEY_QUANTUM", "BarBacktester"]

#: Monetary outputs (cash, equity, exposure, fee, slippage) are quantized to this step.
MONEY_QUANTUM: Final = Decimal("1e-18")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_NAME: Final = "hlens_bar_backtest"
_VERSION: Final = "1.0.0"


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _groups(bars: Sequence[PriceBar]) -> Iterator[tuple[datetime, list[PriceBar]]]:
    for start, group in groupby(bars, key=lambda bar: bar.interval_start):
        yield start, list(group)


class BarBacktester:
    """Next-bar-open, cost-aware, deterministic portfolio simulation (``BacktestProvider``)."""

    def __init__(self) -> None:
        self._descriptor = BacktestProviderDescriptor(
            name=_NAME,
            version=_VERSION,
            deterministic=True,
            simulation_only=True,
            execution_model="next_bar_open",
        )

    @property
    def descriptor(self) -> BacktestProviderDescriptor:
        return self._descriptor

    def run(self, request: BacktestRequest) -> BacktestResult:
        if not isinstance(request, BacktestRequest):
            raise BacktestInputError("run needs a BacktestRequest")
        with localcontext(_CONTEXT):
            return self._simulate(request)

    def _simulate(self, request: BacktestRequest) -> BacktestResult:
        fee_rate = request.cost_model.fee_rate
        slippage = request.cost_model.slippage_rate
        targets: Sequence[TargetPosition] = request.targets
        pending: dict[str, TargetPosition] = {}
        quantities: dict[str, Decimal] = {}
        marks: dict[str, Decimal] = {}
        cash = request.initial_equity
        fills: list[Fill] = []
        curve: list[EquityPoint] = []
        admitted = 0
        executed = 0

        for start, group in _groups(request.bars):
            while admitted < len(targets) and targets[admitted].decision_time <= start:
                target = targets[admitted]
                pending[target.instrument] = target  # supersedes an unexecuted earlier target
                admitted += 1

            for bar in group:
                marks[bar.instrument] = bar.open
            equity_open = cash + sum(
                (qty * marks[name] for name, qty in quantities.items()), Decimal(0)
            )

            for bar in group:
                if bar.instrument not in pending:
                    continue
                target = pending.pop(bar.instrument)
                executed += 1
                held = quantities.get(bar.instrument, Decimal(0))
                weight = target.target_weight if equity_open > 0 else Decimal(0)
                trade = weight * equity_open / bar.open - held
                if trade == 0:
                    continue
                factor = Decimal(1) + slippage if trade > 0 else Decimal(1) - slippage
                fill_price = bar.open * factor
                fee = _money(abs(trade) * fill_price * fee_rate)
                slippage_cost = _money(abs(trade) * abs(fill_price - bar.open))
                cash = _money(cash - trade * fill_price - fee)
                quantities[bar.instrument] = held + trade
                fills.append(
                    Fill(
                        instrument=bar.instrument,
                        decision_time=target.decision_time,
                        fill_time=bar.interval_start,
                        reference_price=bar.open,
                        fill_price=fill_price,
                        quantity=trade,
                        fee=fee,
                        slippage_cost=slippage_cost,
                    )
                )

            for bar in group:
                marks[bar.instrument] = bar.close
            time = max(bar.interval_end for bar in group)
            if curve and time <= curve[-1].time:
                raise BacktestInputError(
                    f"bars starting at {start.isoformat()} end no later than the previous group; "
                    "v1 needs one aligned bar grid"
                )
            exposure = sum((abs(qty * marks[name]) for name, qty in quantities.items()), Decimal(0))
            equity = cash + sum((qty * marks[name] for name, qty in quantities.items()), Decimal(0))
            curve.append(
                EquityPoint(
                    time=time, cash=cash, equity=_money(equity), gross_exposure=_money(exposure)
                )
            )

        return BacktestResult.build(
            request,
            self._descriptor,
            fills=fills,
            equity_curve=curve,
            unexecuted_targets=len(targets) - executed,
        )
