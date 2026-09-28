"""Pure source adapters for Phase 4 Outcomes (ADR-0037 / W2-P4)."""

from __future__ import annotations

from core.contracts.event import Event, EventProviderDescriptor, EventRequest, EventResult
from core.contracts.outcome import OutcomeEvent
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from research.outcomes import outcome_events_from_event_result
from tests.fake_events import MINUTE, T0, X_INPUTS

EVENT = Ref(kind=Kind.EVENT, name="outcome_source", version="1.0.0")
SPEC_HASH = content_hash({"spec": "outcome_source"})


def _result(*, empty: bool = False) -> EventResult:
    request = EventRequest(
        event=EVENT,
        spec_hash=SPEC_HASH,
        as_of=T0 + 10 * MINUTE,
        inputs=X_INPUTS,
    )
    descriptor = EventProviderDescriptor(
        name="outcome_source_test",
        version="1.0.0",
        deterministic=True,
        supported_events=FrozenMapping({str(EVENT): SPEC_HASH}),
    )
    events: tuple[Event, ...] = ()
    if not empty:
        events = tuple(
            Event.build(
                event=EVENT,
                spec_hash=SPEC_HASH,
                event_time=T0 + minute * MINUTE,
                inputs=(X_INPUTS[0],),
            )
            for minute in (2, 4)
        )
    return EventResult.build(request, descriptor, events)


def test_event_result_converter_preserves_identity_time_and_order() -> None:
    result = _result()

    converted = outcome_events_from_event_result(result)

    assert converted == tuple(
        OutcomeEvent(event_key=event.event_id, event_time=event.event_time)
        for event in result.events
    )
    assert tuple(event.event_time for event in converted) == tuple(
        sorted(event.event_time for event in converted)
    )


def test_event_result_converter_maps_empty_result_to_empty_tuple() -> None:
    assert outcome_events_from_event_result(_result(empty=True)) == ()
