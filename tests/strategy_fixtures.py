"""Deterministic bar fixtures for the Phase 5 strategy / risk / backtest tests (ADR-0038)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.contracts.strategy import BacktestCostModel, PriceBar

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
COSTS = BacktestCostModel(
    name="flat_bps", version="1.0.0", fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.0005")
)


def make_bars(
    instrument: str, closes: Sequence[Decimal], *, start: datetime = T0
) -> tuple[PriceBar, ...]:
    """One bar per close; each bar opens at the previous close (the first opens at its close)."""
    bars: list[PriceBar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        begin = start + index * MINUTE
        bars.append(
            PriceBar(
                instrument=instrument,
                interval_start=begin,
                interval_end=begin + MINUTE,
                available_time=begin + MINUTE,
                open=previous,
                high=max(previous, close),
                low=min(previous, close),
                close=close,
            )
        )
        previous = close
    return tuple(bars)


def wave_closes(count: int, *, base: str = "100", phase: int = 0) -> tuple[Decimal, ...]:
    """A deterministic up-trend / down-trend wave (period 40 bars), exact decimals."""
    out: list[Decimal] = []
    price = Decimal(base)
    for index in range(count):
        step = Decimal("0.4") if ((index + phase) // 20) % 2 == 0 else Decimal("-0.3")
        wobble = Decimal("0.05") if index % 3 == 0 else Decimal("-0.02")
        price = price + step + wobble
        out.append(price)
    return tuple(out)
