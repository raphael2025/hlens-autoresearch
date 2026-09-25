"""Price sources for outcome requests (Phase 4, ADR-0037).

Only the synthetic source exists in this batch: ``bars_from_synthetic`` turns the bars of a
``SyntheticMarket`` (ADR-0042) into ``OutcomePriceBar`` with ``available_time = interval_end``.
Canonical ``bars_1m`` rows are read by the Data Plane; wiring that reader is a later batch.
"""

from __future__ import annotations

from core.contracts.outcome import OutcomePriceBar
from core.contracts.synthetic import SyntheticMarket

__all__ = ["bars_from_synthetic"]


def bars_from_synthetic(market: SyntheticMarket) -> tuple[OutcomePriceBar, ...]:
    return tuple(
        OutcomePriceBar(
            interval_start=bar.interval_start,
            interval_end=bar.interval_end,
            available_time=bar.interval_end,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in market.bars
    )
