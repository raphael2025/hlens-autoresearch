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

**Opt-in execution realism** (``BarBacktester(execution=ExecutionModel(...))``, see
``plugins.backtest.execution``): a participation cap on bar volume (remainder cancelled at the
execution bar and reported), square-root impact on the fill price (the G4 capacity check's law) and
per-bar funding of shorts / negative cash (step 3b: after the open's trades, debited from cash).
The variant's descriptor version is ``1.1.0+exec.<fingerprint>``, so every parameter and every
volume is bound into ``provider_hash``; the default constructor keeps version ``1.0.0`` and gives
byte-identical results to v1. ``run_with_report`` also returns the ``ExecutionReport``
(remainders, per-fill participation and impact, funding charges).
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
from plugins.backtest.execution import (
    ExecutionModel,
    ExecutionReport,
    FillExecution,
    FundingCharge,
    UnfilledRemainder,
)

__all__ = ["EXECUTION_VERSION", "MONEY_QUANTUM", "BarBacktester"]

#: Monetary outputs (cash, equity, exposure, fee, slippage) are quantized to this step.
MONEY_QUANTUM: Final = Decimal("1e-18")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_NAME: Final = "hlens_bar_backtest"
_VERSION: Final = "1.0.0"
#: Version core of the opt-in execution variant; its build metadata is ``exec.<fingerprint>``.
EXECUTION_VERSION: Final = "1.1.0"


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _groups(bars: Sequence[PriceBar]) -> Iterator[tuple[datetime, list[PriceBar]]]:
    for start, group in groupby(bars, key=lambda bar: bar.interval_start):
        yield start, list(group)


def _bar_volume(execution: ExecutionModel, bar: PriceBar) -> Decimal:
    volume = execution.volume(bar.instrument, bar.interval_start)
    if volume is None:
        raise BacktestInputError(
            f"no bar volume for {bar.instrument} @ {bar.interval_start.isoformat()}; "
            "the participation cap / impact model never fills a missing volume in"
        )
    return volume


def _capped(execution: ExecutionModel, desired: Decimal, volume: Decimal | None) -> Decimal:
    rate = execution.max_participation_rate
    if rate is None or volume is None:
        return desired
    limit = rate * volume
    if abs(desired) <= limit:
        return desired
    return limit if desired > 0 else -limit


def _impact(
    execution: ExecutionModel, trade: Decimal, volume: Decimal | None, bar: PriceBar
) -> tuple[Decimal, Decimal | None]:
    """``(impact fraction, participation)`` with ``coefficient × sqrt(|trade| / volume)``.

    The square-root law of ``research.validation.robustness.capacity_check`` (G4, C-R5):
    ``impact_coefficient * sqrt(participation)`` per unit traded.
    """
    if volume is None:
        return Decimal(0), None
    coefficient = execution.impact_coefficient
    if volume == 0:  # only reachable without a cap (a cap on zero volume fills nothing)
        raise BacktestInputError(
            f"{bar.instrument} @ {bar.interval_start.isoformat()} trades against zero bar volume; "
            "the square-root impact is unbounded"
        )
    participation = abs(trade) / volume
    if coefficient is None:
        return Decimal(0), participation
    return coefficient * participation.sqrt(), participation


class BarBacktester:
    """Next-bar-open, cost-aware, deterministic portfolio simulation (``BacktestProvider``).

    ``execution=None`` (the default) is the v1 model, byte-identical to earlier releases. An
    explicit ``ExecutionModel`` switches on the opt-in participation cap, impact and funding and
    changes the descriptor version to ``1.1.0+exec.<fingerprint>``.
    """

    def __init__(self, *, execution: ExecutionModel | None = None) -> None:
        if execution is not None and not isinstance(execution, ExecutionModel):
            raise TypeError("execution must be an ExecutionModel or None")
        self._execution = execution
        version = (
            _VERSION if execution is None else f"{EXECUTION_VERSION}+exec.{execution.fingerprint}"
        )
        self._descriptor = BacktestProviderDescriptor(
            name=_NAME,
            version=version,
            deterministic=True,
            simulation_only=True,
            execution_model="next_bar_open",
        )

    @property
    def descriptor(self) -> BacktestProviderDescriptor:
        return self._descriptor

    @property
    def execution(self) -> ExecutionModel | None:
        return self._execution

    def run(self, request: BacktestRequest) -> BacktestResult:
        return self.run_with_report(request)[0]

    def run_with_report(self, request: BacktestRequest) -> tuple[BacktestResult, ExecutionReport]:
        """The result plus what it cannot carry (remainders, per-fill impact, funding)."""
        if not isinstance(request, BacktestRequest):
            raise BacktestInputError("run needs a BacktestRequest")
        with localcontext(_CONTEXT):
            return self._simulate(request)

    def _simulate(self, request: BacktestRequest) -> tuple[BacktestResult, ExecutionReport]:
        execution = self._execution
        fee_rate = request.cost_model.fee_rate
        slippage = request.cost_model.slippage_rate
        targets: Sequence[TargetPosition] = request.targets
        pending: dict[str, TargetPosition] = {}
        quantities: dict[str, Decimal] = {}
        marks: dict[str, Decimal] = {}
        cash = request.initial_equity
        fills: list[Fill] = []
        curve: list[EquityPoint] = []
        details: list[FillExecution] = []
        remainders: list[UnfilledRemainder] = []
        funding: list[FundingCharge] = []
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
                held = quantities.get(bar.instrument, Decimal(0))
                weight = target.target_weight if equity_open > 0 else Decimal(0)
                desired = weight * equity_open / bar.open - held
                if execution is None:  # v1: unchanged arithmetic, byte-identical results
                    executed += 1
                    trade = desired
                    if trade == 0:
                        continue
                    factor = Decimal(1) + slippage if trade > 0 else Decimal(1) - slippage
                else:
                    if desired == 0:
                        executed += 1
                        continue
                    volume = _bar_volume(execution, bar) if execution.needs_volume else None
                    trade = _capped(execution, desired, volume)
                    if trade != desired and volume is not None:
                        remainders.append(
                            UnfilledRemainder(
                                instrument=bar.instrument,
                                decision_time=target.decision_time,
                                bar_time=bar.interval_start,
                                bar_volume=volume,
                                desired_quantity=desired,
                                filled_quantity=trade,
                                cancelled_quantity=desired - trade,
                            )
                        )
                    if trade == 0:  # nothing fits: the target is unexecuted (and reported)
                        continue
                    executed += 1
                    impact, participation = _impact(execution, trade, volume, bar)
                    if trade > 0:
                        factor = Decimal(1) + slippage + impact
                    else:
                        factor = Decimal(1) - slippage - impact
                    if factor <= 0:
                        raise BacktestInputError(
                            f"{bar.instrument} @ {bar.interval_start.isoformat()}: slippage plus "
                            "impact would push the sell price to zero or below"
                        )
                    details.append(
                        FillExecution(
                            instrument=bar.instrument,
                            fill_time=bar.interval_start,
                            bar_volume=volume,
                            participation=participation,
                            impact_cost=_money(abs(trade) * bar.open * impact),
                        )
                    )
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

            if execution is not None and execution.charges_funding:
                charge = _funding(execution, start, cash, quantities, marks)
                if charge is not None:
                    funding.append(charge)
                    cash = _money(cash - charge.cost)

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

        result = BacktestResult.build(
            request,
            self._descriptor,
            fills=fills,
            equity_curve=curve,
            unexecuted_targets=len(targets) - executed,
        )
        report = ExecutionReport(
            result_hash=result.result_hash,
            execution_fingerprint=None if execution is None else execution.fingerprint,
            fills=tuple(details),
            remainders=tuple(remainders),
            funding=tuple(funding),
            total_impact=sum((item.impact_cost for item in details), Decimal(0)),
            total_funding=sum((item.cost for item in funding), Decimal(0)),
        )
        return result, report


def _funding(
    execution: ExecutionModel,
    start: datetime,
    cash: Decimal,
    quantities: dict[str, Decimal],
    marks: dict[str, Decimal],
) -> FundingCharge | None:
    """Per-bar-step funding after the open's trades, at the open marks (nothing later is used)."""
    short = sum((-qty * marks[name] for name, qty in quantities.items() if qty < 0), Decimal(0))
    borrowed = -cash if cash < 0 else Decimal(0)
    cost = Decimal(0)
    if execution.short_borrow_rate is not None:
        cost += execution.short_borrow_rate * short
    if execution.cash_borrow_rate is not None:
        cost += execution.cash_borrow_rate * borrowed
    cost = _money(cost)
    if cost == 0:
        return None
    return FundingCharge(
        time=start, short_notional=_money(short), borrowed_cash=borrowed, cost=cost
    )
