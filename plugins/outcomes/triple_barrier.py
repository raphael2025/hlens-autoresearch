"""Triple-barrier outcome (ADR-0037): the first of an upper, a lower and a vertical barrier.

Entry at the open of the first bar at/after the event. Upper price ``entry * (1 + upper)``, lower
price ``entry * (1 - lower)``, vertical barrier ``entry_time + horizon``. Bars are scanned in order:

- a bar whose low reaches the lower price exits at ``-1`` (at the open when the bar gaps through
  it). When one bar reaches **both** prices the order inside the bar is unknown, and the label
  resolves pessimistically to the lower barrier;
- otherwise a bar whose high reaches the upper price exits at ``+1`` (at the open on a gap);
- no touch by a complete window exits at ``0`` at the last close.

A touch is known only when its bar is complete, so ``exit_time`` is that bar's end. A gap before
any touch, or an incomplete window without a touch, is ``None``: an unseen bar could have touched.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabel,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeRequest,
)
from plugins.outcomes._window import LabelProviderBase, label_window, quantize_return, unknown

__all__ = ["TripleBarrierOutcome"]


class TripleBarrierOutcome(LabelProviderBase):
    METHOD = OutcomeMethod.TRIPLE_BARRIER
    NAME = "hlens_triple_barrier"

    def _label(self, request: OutcomeRequest, event: OutcomeEvent) -> OutcomeLabel:
        spec = request.label_spec
        window = label_window(request.bars, event.event_time, spec.horizon)
        if window is None or not window.bars:
            return unknown(event)
        if spec.upper_barrier is None or spec.lower_barrier is None:  # pragma: no cover
            raise ValueError("triple_barrier label spec without barriers")  # contract forbids it
        entry_price = window.bars[0].open
        upper = entry_price * (1 + spec.upper_barrier)
        lower = entry_price * (1 - spec.lower_barrier)
        used: list[OutcomePriceBar] = []
        for bar in window.bars:
            used.append(bar)
            if bar.low <= lower:
                exit_price = bar.open if bar.open <= lower else lower
                return _touch(event, window.entry_time, entry_price, exit_price, -1, used)
            if bar.high >= upper:
                exit_price = bar.open if bar.open >= upper else upper
                return _touch(event, window.entry_time, entry_price, exit_price, 1, used)
        if not window.complete:
            return unknown(event)
        return _touch(event, window.entry_time, entry_price, used[-1].close, 0, used)


def _touch(
    event: OutcomeEvent,
    entry_time: datetime,
    entry_price: Decimal,
    exit_price: Decimal,
    barrier: Literal[-1, 0, 1],
    used: list[OutcomePriceBar],
) -> OutcomeLabel:
    return OutcomeLabel(
        event_key=event.event_key,
        event_time=event.event_time,
        value=quantize_return(exit_price, entry_price),
        barrier=barrier,
        entry_time=entry_time,
        exit_time=used[-1].interval_end,
        entry_price=entry_price,
        exit_price=exit_price,
        available_time=max(bar.available_time for bar in used),
    )
