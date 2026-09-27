"""Price and event sources for outcome requests (Phase 4, ADR-0037).

``bars_from_synthetic`` turns the bars of a ``SyntheticMarket`` (ADR-0042) into
``OutcomePriceBar`` with ``available_time = interval_end``. Canonical ``bars_1m`` rows are read by
the Data Plane (``infrastructure/bars``).

``outcome_events_from_event_result`` turns the event table of an ``EventResult`` (ADR-0036) into
``OutcomeEvent`` values: ``event_key = Event.event_id`` and ``event_time = Event.event_time`` (the
observable time, ADR-0036 §2), so every label is keyed by the content hash of the event it labels.
"""

from __future__ import annotations

from core.contracts.event import EventResult
from core.contracts.outcome import OutcomeEvent, OutcomePriceBar
from core.contracts.synthetic import SyntheticMarket

__all__ = ["bars_from_synthetic", "outcome_events_from_event_result"]


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


def outcome_events_from_event_result(result: EventResult) -> tuple[OutcomeEvent, ...]:
    """One ``OutcomeEvent`` per event of ``result``, in the result's order; nothing dropped.

    ``EventResult`` already guarantees unique ``event_id`` values in strictly ascending
    ``(event_time, event_id)`` order, which is exactly ``OutcomeRequest``'s canonical
    ``(event_time, event_key)`` order, so the output is unique, deterministic and unchanged by the
    request's canonicalisation. An empty result gives ``()``; ``OutcomeRequest`` itself rejects an
    empty ``events`` tuple. ``Event.subject`` is not carried over (``OutcomeEvent`` has no subject;
    it is already part of ``event_id``): pairing the events with bars of the same subject stays the
    caller's responsibility.
    """
    return tuple(
        OutcomeEvent(event_key=item.event_id, event_time=item.event_time) for item in result.events
    )
