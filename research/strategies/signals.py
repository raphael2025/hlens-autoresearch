"""Bar-derived signal observations for research strategies (Phase 5; ADR-0038).

Exploration helper: it turns ``PriceBar`` rows into ``SignalObservation`` rows for the two feature
refs the first strategies consume. The production path computes these through the
``FeatureProvider`` runner (``infrastructure/feature``, ADR-0030) over PIT-selected canonical bars;
this helper mirrors the same definitions so a research run needs no data plane. Both signals are
available when the bar that completes them is available (``bar.available_time``), never earlier.

- ``bar_log_return@1.0.0``: ``ln(close[k] / close[k-1])`` of two contiguous bars;
- ``bar_realized_vol_<n>@1.0.0``: ``sqrt(sum r_i^2)`` over the last ``n`` contiguous one-bar log
  returns.

A gap (``end != next start``) breaks the run: no value is interpolated. Logs and roots run at
50 significant digits and are quantized to 18 places, half-even (as ``plugins/features/bars``);
the realized volatility squares the unquantized log returns, so both values equal the provider's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.strategy import PriceBar, SignalObservation
from core.domain.base import Kind, Ref

__all__ = [
    "LOG_RETURN_SIGNAL",
    "bar_signals",
    "realized_vol_signal",
]

LOG_RETURN_SIGNAL: Final = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_SCALE: Final = Decimal("1e-18")


def realized_vol_signal(window: int) -> Ref:
    """The ``bar_realized_vol_<window>@1.0.0`` feature ref."""
    if window < 1:
        raise ValueError("window must be positive")
    return Ref(kind=Kind.FEATURE, name=f"bar_realized_vol_{window}", version="1.0.0")


def _by_instrument(bars: Iterable[PriceBar]) -> dict[str, list[PriceBar]]:
    grouped: dict[str, list[PriceBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.instrument, item.interval_start)):
        grouped.setdefault(bar.instrument, []).append(bar)
    return grouped


def _observation(
    signal: Ref, bar: PriceBar, value: Decimal | None, knowledge_time: datetime | None
) -> SignalObservation:
    return SignalObservation(
        signal=signal,
        instrument=bar.instrument,
        event_time=bar.interval_end,
        available_time=bar.available_time,
        knowledge_time=knowledge_time or bar.available_time,
        value=value,
    )


def bar_signals(
    bars: Sequence[PriceBar],
    *,
    vol_windows: Sequence[int] = (),
    knowledge_time: datetime | None = None,
) -> tuple[SignalObservation, ...]:
    """Log-return and realized-volatility observations for every bar that completes one.

    ``knowledge_time`` defaults to each bar's ``available_time`` (a replayed archive would pass the
    time the rows were learned). Bars too early to complete a value yield an explicit ``None``.
    """
    out: list[SignalObservation] = []
    with localcontext(_CONTEXT):
        for series in _by_instrument(bars).values():
            # Unquantized 50-digit log returns: the realized volatility squares these (as the
            # provider does), and only the published log-return value is quantized.
            returns: list[Decimal | None] = [None]
            for previous, bar in zip(series, series[1:], strict=False):
                if previous.interval_end != bar.interval_start:
                    returns.append(None)
                else:
                    returns.append((bar.close / previous.close).ln())
            for index, bar in enumerate(series):
                if index > 0:
                    log_return = returns[index]
                    quantized = log_return.quantize(_SCALE) if log_return is not None else None
                    out.append(_observation(LOG_RETURN_SIGNAL, bar, quantized, knowledge_time))
                for window in vol_windows:
                    tail = returns[max(0, index - window + 1) : index + 1]
                    value: Decimal | None = None
                    if len(tail) == window and all(item is not None for item in tail):
                        total = sum((item**2 for item in tail if item is not None), Decimal(0))
                        value = total.sqrt().quantize(_SCALE)
                    out.append(
                        _observation(realized_vol_signal(window), bar, value, knowledge_time)
                    )
    return tuple(out)
