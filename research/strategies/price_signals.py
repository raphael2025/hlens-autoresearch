"""Bar price-level signal observations for research strategies (ADR-0085). Research code (H5).

Exploration helper, companion of ``signals.py``: ``donchian_breakout`` and ``zscore_reversion``
read price levels, which ``bar_log_return`` / ``bar_realized_vol_<n>`` do not carry. It turns
``PriceBar`` rows into ``SignalObservation`` rows for three feature refs:

- ``bar_close@1.0.0``: the bar's ``close``;
- ``bar_high@1.0.0``: the bar's ``high``;
- ``bar_low@1.0.0``: the bar's ``low``.

Each value is the bar's own ``Decimal`` exactly (nothing is rounded, derived or filled), described
at ``interval_end`` and available when the bar is (``bar.available_time``), never earlier — as in
``signals.py``. There is no production ``FeatureProvider`` for these refs yet; the production path
would compute them through the ``FeatureProvider`` runner (``infrastructure/feature``, ADR-0030)
over PIT-selected canonical bars, with these same definitions.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from core.contracts.strategy import PriceBar, SignalObservation
from core.domain.base import Kind, Ref

__all__ = [
    "BAR_CLOSE_SIGNAL",
    "BAR_HIGH_SIGNAL",
    "BAR_LOW_SIGNAL",
    "bar_price_signals",
]

BAR_CLOSE_SIGNAL: Final = Ref(kind=Kind.FEATURE, name="bar_close", version="1.0.0")
BAR_HIGH_SIGNAL: Final = Ref(kind=Kind.FEATURE, name="bar_high", version="1.0.0")
BAR_LOW_SIGNAL: Final = Ref(kind=Kind.FEATURE, name="bar_low", version="1.0.0")

_FIELDS: Final = {
    BAR_CLOSE_SIGNAL: "close",
    BAR_HIGH_SIGNAL: "high",
    BAR_LOW_SIGNAL: "low",
}


def bar_price_signals(
    bars: Sequence[PriceBar],
    signals: Sequence[Ref],
    *,
    knowledge_time: datetime | None = None,
) -> tuple[SignalObservation, ...]:
    """One observation per bar and requested price signal (``BAR_CLOSE/HIGH/LOW_SIGNAL``).

    ``knowledge_time`` defaults to each bar's ``available_time`` (a replayed archive would pass the
    time the rows were learned). An unknown signal ref is refused (``ValueError``).
    """
    unknown = [ref for ref in signals if ref not in _FIELDS]
    if unknown:
        raise ValueError(f"not a bar price signal: {unknown[0]}")
    if len(set(signals)) != len(signals):
        raise ValueError("price signals must not repeat")
    out: list[SignalObservation] = []
    for bar in sorted(bars, key=lambda item: (item.instrument, item.interval_start)):
        for ref in signals:
            out.append(
                SignalObservation(
                    signal=ref,
                    instrument=bar.instrument,
                    event_time=bar.interval_end,
                    available_time=bar.available_time,
                    knowledge_time=knowledge_time or bar.available_time,
                    value=getattr(bar, _FIELDS[ref]),
                )
            )
    return tuple(out)
