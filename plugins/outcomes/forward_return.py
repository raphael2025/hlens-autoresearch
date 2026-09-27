"""Forward-return outcome (ADR-0037): ``exit_close / entry_open - 1`` over the horizon.

Entry at the open of the first bar at/after the event; exit at the close of the bar that ends
exactly at ``entry_time + horizon``. An incomplete or gapped window is ``None`` (not computable).
The label is direction free: the validation layer multiplies it by the signal side.
"""

from __future__ import annotations

from core.contracts.outcome import OutcomeEvent, OutcomeLabel, OutcomeMethod, OutcomeRequest
from plugins.outcomes._window import LabelProviderBase, label_window, quantize_return, unknown

__all__ = ["ForwardReturnOutcome"]


class ForwardReturnOutcome(LabelProviderBase):
    METHOD = OutcomeMethod.FORWARD_RETURN
    NAME = "hlens_forward_return"

    def _label(self, request: OutcomeRequest, event: OutcomeEvent) -> OutcomeLabel:
        window = label_window(request.bars, event.event_time, request.label_spec.horizon)
        if window is None or not window.complete:
            return unknown(event)
        first, last = window.bars[0], window.bars[-1]
        return OutcomeLabel(
            event_key=event.event_key,
            event_time=event.event_time,
            value=quantize_return(last.close, first.open),
            entry_time=window.entry_time,
            exit_time=last.interval_end,
            entry_price=first.open,
            exit_price=last.close,
            available_time=max(bar.available_time for bar in window.bars),
        )
