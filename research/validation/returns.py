"""Per-period return series for the G4 robustness checks (Phase 8, ADR-0041).

``PeriodReturns`` holds, per period ``(times[i-1], times[i]]``, the **gross** return (before costs)
and the **cost** (fees + slippage at cost multiplier 1), both as fractions of the equity at the
start of the period. The net return at a Profile cost multiplier ``m`` is ``gross - m * cost``,
so cost stress never needs a re-run and never skips the cost model (roadmap Phase 4).

``from_backtest`` derives the series from a ``BacktestResult`` (Phase 5, ADR-0038): each fill's
fee and slippage cost belong to the first equity point strictly after its ``fill_time``; the
gross change is the equity change plus those costs (the fill price already contains the slippage,
the fee is paid from cash), so ``net(1)`` reproduces the backtest's own equity curve.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Final

from core.contracts.strategy import BacktestResult

__all__ = ["ParamPoint", "PeriodReturns", "TrialReturns", "aligned", "from_backtest", "param_key"]

#: One point of a declared parameter space (the same scalar types as ``StrategySpec.params``).
ParamPoint = Mapping[str, str | int | float | bool]

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class PeriodReturns:
    times: tuple[datetime, ...]
    gross: tuple[Decimal, ...]
    cost: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if not (len(self.times) == len(self.gross) == len(self.cost)):
            raise ValueError("times, gross and cost must have the same length")
        if any(t.tzinfo is None for t in self.times):
            raise ValueError("period times must be timezone-aware UTC")
        if any(later <= earlier for earlier, later in pairwise(self.times)):
            raise ValueError("period times must be strictly ascending")
        if any(item < 0 for item in self.cost):
            raise ValueError("a period cost cannot be negative")

    def __len__(self) -> int:
        return len(self.times)

    def net(self, multiplier: Decimal = Decimal(1)) -> tuple[Decimal, ...]:
        with localcontext(_CONTEXT):
            return tuple(g - multiplier * c for g, c in zip(self.gross, self.cost, strict=True))

    def net_floats(self, multiplier: Decimal = Decimal(1)) -> list[float]:
        return [float(item) for item in self.net(multiplier)]

    def total_gross(self) -> Decimal:
        with localcontext(_CONTEXT):
            return sum(self.gross, Decimal(0))

    def total_cost(self) -> Decimal:
        with localcontext(_CONTEXT):
            return sum(self.cost, Decimal(0))

    def breakeven_cost_multiple(self) -> Decimal | None:
        """How many times its own cost the gross edge covers; ``None`` without any cost."""
        cost = self.total_cost()
        if cost == 0:
            return None
        with localcontext(_CONTEXT):
            return self.total_gross() / cost

    def window(self, start: datetime, end: datetime) -> PeriodReturns:
        """Periods whose end time lies in ``[start, end)``."""
        picked = [i for i, t in enumerate(self.times) if start <= t < end]
        return PeriodReturns(
            times=tuple(self.times[i] for i in picked),
            gross=tuple(self.gross[i] for i in picked),
            cost=tuple(self.cost[i] for i in picked),
        )


@dataclass(frozen=True)
class TrialReturns:
    """The period returns of one declared parameter point of a family."""

    params: ParamPoint
    returns: PeriodReturns

    def key(self) -> tuple[tuple[str, str], ...]:
        return param_key(self.params)


def param_key(params: ParamPoint) -> tuple[tuple[str, str], ...]:
    """A hashable, order-free identity of a parameter point (``repr`` keeps types apart)."""
    return tuple(sorted((name, repr(value)) for name, value in params.items()))


def from_backtest(result: BacktestResult) -> PeriodReturns:
    """Gross and cost per equity point of a backtest (see module docs)."""
    points = result.equity_curve
    costs = [Decimal(0)] * len(points)
    for fill in result.fills:
        index = next((i for i, p in enumerate(points) if p.time > fill.fill_time), None)
        if index is None:
            raise ValueError("a fill after the last equity point cannot be attributed")
        costs[index] += fill.fee + fill.slippage_cost
    gross: list[Decimal] = []
    cost: list[Decimal] = []
    previous = result.initial_equity
    with localcontext(_CONTEXT):
        for point, spent in zip(points, costs, strict=True):
            if previous <= 0:
                raise ValueError("returns are undefined once equity is not positive")
            gross.append((point.equity - previous + spent) / previous)
            cost.append(spent / previous)
            previous = point.equity
    return PeriodReturns(
        times=tuple(point.time for point in points), gross=tuple(gross), cost=tuple(cost)
    )


def aligned(trials: Sequence[TrialReturns]) -> bool:
    """All trials share one period grid (required by CSCV and the neighbourhood comparison)."""
    return all(trial.returns.times == trials[0].returns.times for trial in trials)
