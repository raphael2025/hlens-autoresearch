"""Reference backtester (Phase 14 migration target; ADR-0106). Simulation only.

``ReferenceBacktester`` is the **second, independent** ``BacktestProvider`` implementation: the
migration drill's target (``bar.BarBacktester`` is the source). It is written from the contract and
the documented ``next_bar_open`` semantics — it imports nothing from ``plugins.backtest.bar`` (no
shared simulation function, no shared constant) — and is **not** the production default; it never
replaces ``BarBacktester`` (ADR-0106 decision 5).

Scope (ADR-0106 decision 1): only ``next_bar_open``. Everything else is refused explicitly with
``ReferenceScopeError`` (a ``BacktestInputError``): an ``ExecutionModel`` / carry-over, a risk loop
(``run_with_risk``) and the execution report (``run_with_report``). There is no silent fallback.

Semantics (the same observable behaviour a migration must reproduce bit-exactly), one timestamp at a
time, instruments of one timestamp in ascending order:

1. a target is *admitted* once ``decision_time <= timestamp`` and is held per instrument; a later
   admitted target supersedes one that has not executed (it is never counted as executed);
2. marks move to the bars' opens; ``equity_open = cash + Σ quantity × open`` (the sum is accumulated
   from zero in the order the instruments first traded, then added to cash);
3. each instrument with a bar and a held target executes: ``desired = weight × equity_open / open −
   quantity`` (``weight`` is zero when ``equity_open <= 0``); a non-zero trade fills at
   ``open × (1 ± slippage)`` (buy ``+``), pays ``|trade| × fill × fee_rate`` and reports
   ``|trade| × |fill − open|``; cash becomes ``cash − trade × fill − fee``; a zero trade executes
   without a fill;
4. marks move to the closes; the ``EquityPoint`` is stamped with the latest ``interval_end`` of the
   timestamp.

Arithmetic: 50 significant digits, half-even, in exactly the operation order above (``Decimal``
addition and multiplication are not associative at finite precision, so the order is part of the
specification). Cash, equity, exposure, fees and slippage are quantized to ``MONEY_QUANTUM``;
quantities and prices keep working precision. ``unexecuted_targets`` counts targets that never
executed (superseded, or decided after the last bar of their instrument).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
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

__all__ = ["REFERENCE_MONEY_QUANTUM", "ReferenceBacktester", "ReferenceScopeError"]

#: Monetary outputs are quantized to this step (the same value as ``BarBacktester``'s, but defined
#: here independently).
REFERENCE_MONEY_QUANTUM: Final = Decimal("1e-18")
_PRECISION: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_NAME: Final = "hlens_reference_backtest"
_VERSION: Final = "1.0.0"
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


class ReferenceScopeError(BacktestInputError):
    """The request or construction needs something the reference engine does not implement."""


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(REFERENCE_MONEY_QUANTUM)


@dataclass
class _Book:
    """Cash, signed quantities (insertion order = first-trade order) and last marks."""

    cash: Decimal
    quantity: dict[str, Decimal] = field(default_factory=dict)
    mark: dict[str, Decimal] = field(default_factory=dict)

    def value(self) -> Decimal:
        """``Σ quantity × mark`` accumulated from zero in first-trade order."""
        total = _ZERO
        for name, held in self.quantity.items():
            total = total + held * self.mark[name]
        return total

    def exposure(self) -> Decimal:
        total = _ZERO
        for name, held in self.quantity.items():
            total = total + abs(held * self.mark[name])
        return total


def _timeline(bars: Sequence[PriceBar]) -> list[list[PriceBar]]:
    """Bars grouped by ``interval_start`` in time order (the request already sorts them)."""
    slots: dict[datetime, list[PriceBar]] = {}
    for bar in bars:
        slots.setdefault(bar.interval_start, []).append(bar)
    return [slots[start] for start in sorted(slots)]


class ReferenceBacktester:
    """``BacktestProvider`` for ``next_bar_open`` only; see the module docstring."""

    def __init__(self, *, execution: object | None = None) -> None:
        if execution is not None:
            raise ReferenceScopeError(
                "ReferenceBacktester has no execution model (participation cap, impact, funding, "
                "carry-over): ADR-0106 scopes it to next_bar_open"
            )
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
        with localcontext(_PRECISION):
            return self._simulate(request)

    def run_with_report(self, request: BacktestRequest) -> object:
        raise ReferenceScopeError("ReferenceBacktester has no execution report (ADR-0106)")

    def run_with_risk(self, *args: object, **kwargs: object) -> object:
        raise ReferenceScopeError("ReferenceBacktester has no risk loop (ADR-0106)")

    def _simulate(self, request: BacktestRequest) -> BacktestResult:
        fee_rate = request.cost_model.fee_rate
        slippage = request.cost_model.slippage_rate
        book = _Book(cash=request.initial_equity)
        queue: Sequence[TargetPosition] = request.targets  # (decision_time, instrument) ascending
        waiting: dict[str, TargetPosition] = {}
        cursor = 0
        executed = 0
        fills: list[Fill] = []
        curve: list[EquityPoint] = []

        for group in _timeline(request.bars):
            start = group[0].interval_start
            while cursor < len(queue) and queue[cursor].decision_time <= start:
                waiting[queue[cursor].instrument] = queue[cursor]
                cursor += 1

            for bar in group:
                book.mark[bar.instrument] = bar.open
            equity_open = book.cash + book.value()

            for bar in group:
                target = waiting.pop(bar.instrument, None)
                if target is None:
                    continue
                executed += 1
                held = book.quantity.get(bar.instrument, _ZERO)
                weight = target.target_weight if equity_open > 0 else _ZERO
                trade = weight * equity_open / bar.open - held
                if trade == 0:
                    continue
                price = bar.open * (_ONE + slippage if trade > 0 else _ONE - slippage)
                fee = _quantize(abs(trade) * price * fee_rate)
                slip = _quantize(abs(trade) * abs(price - bar.open))
                book.cash = _quantize(book.cash - trade * price - fee)
                book.quantity[bar.instrument] = held + trade
                fills.append(
                    Fill(
                        instrument=bar.instrument,
                        decision_time=target.decision_time,
                        fill_time=bar.interval_start,
                        reference_price=bar.open,
                        fill_price=price,
                        quantity=trade,
                        fee=fee,
                        slippage_cost=slip,
                    )
                )

            for bar in group:
                book.mark[bar.instrument] = bar.close
            stamp = max(bar.interval_end for bar in group)
            if curve and stamp <= curve[-1].time:
                raise BacktestInputError(
                    f"bars starting at {start.isoformat()} end no later than the previous group; "
                    "the reference engine needs one aligned bar grid"
                )
            curve.append(
                EquityPoint(
                    time=stamp,
                    cash=book.cash,
                    equity=_quantize(book.cash + book.value()),
                    gross_exposure=_quantize(book.exposure()),
                )
            )

        return BacktestResult.build(
            request,
            self._descriptor,
            fills=fills,
            equity_curve=curve,
            unexecuted_targets=len(queue) - executed,
        )
