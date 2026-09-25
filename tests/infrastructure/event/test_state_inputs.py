"""Phase 2 -> Phase 3 wiring: ``StateResult`` -> ``EventInputPoint`` (ADR-0035 §1, ADR-0036 §1).

``state_series_from_state_run`` / ``state_value_lineage`` (``infrastructure/event/inputs.py``) turn
one Phase 2 state run into the event engine's local ``StateSeriesPoint`` shape. Smoke level: rerun
gives identical points, a future perturbation never changes a past point's identity (and therefore
never changes a past event's id downstream, through ``StateSwitchProvider``), and a ``None`` state
(not computable) is passed through, never filled.
"""

from __future__ import annotations

from datetime import datetime

from core.contracts.event import EventInputPoint, EventRequest, EventResult
from core.contracts.state import StateInput, StateRequest, StateResult
from infrastructure.event.inputs import (
    inputs_from_state_series,
    state_series_from_state_run,
)
from infrastructure.event.runner import run_events
from infrastructure.state import run_state, state_request
from plugins.events import StateSwitchProvider
from plugins.states import TrendRangeProvider
from tests.fake_states import (
    RETURN_FEATURE,
    TEST_TREND_THRESHOLD,
    TEST_TREND_WINDOW,
    at,
    negate,
    return_inputs,
)

SPEC = TrendRangeProvider.spec(
    RETURN_FEATURE, window=TEST_TREND_WINDOW, threshold=TEST_TREND_THRESHOLD
)
TIMES = tuple(at(i) for i in range(31))
SWITCH = StateSwitchProvider.spec(SPEC.ref)


def _run(inputs: tuple[StateInput, ...]) -> tuple[StateRequest, StateResult]:
    request = state_request(SPEC, TIMES, inputs)
    return request, run_state(TrendRangeProvider((SPEC,)), SPEC, request)


def _events(inputs: tuple[EventInputPoint, ...]) -> EventResult:
    request = EventRequest(
        event=SWITCH.ref, spec_hash=SWITCH.content_hash(), as_of=at(40), inputs=inputs
    )
    return run_events(StateSwitchProvider((SWITCH,)), SWITCH, request)


def test_state_value_none_is_kept_not_filled() -> None:
    request, result = _run(return_inputs())
    points = state_series_from_state_run(request, result)
    none_points = [p for p in points if p.label is None]
    assert none_points, "the fixture must exercise the not-computable case"

    event_inputs = inputs_from_state_series(SPEC.ref, points)
    by_time = {item.evaluation_time: item for item in event_inputs}
    for point in none_points:
        assert by_time[point.evaluation_time].value is None  # explicit None, never filled


def test_rerun_is_identical() -> None:
    request, result = _run(return_inputs())
    first = state_series_from_state_run(request, result)
    again = state_series_from_state_run(request, result)
    assert first == again

    request2, result2 = _run(return_inputs())
    assert result2.result_hash == result.result_hash
    assert state_series_from_state_run(request2, result2) == first


def test_future_perturbation_leaves_past_points_and_event_ids_unchanged() -> None:
    cut: datetime = at(18)
    base_inputs = return_inputs()
    changed_inputs = tuple(
        negate(item) if item.evaluation_time > cut else item for item in base_inputs
    )

    base_request, base_result = _run(base_inputs)
    changed_request, changed_result = _run(changed_inputs)

    base_points = state_series_from_state_run(base_request, base_result)
    changed_points = state_series_from_state_run(changed_request, changed_result)
    past_base = [p for p in base_points if p.evaluation_time <= cut]
    past_changed = [p for p in changed_points if p.evaluation_time <= cut]
    assert past_base == past_changed  # same label and the same lineage_hash, point by point
    assert base_points != changed_points  # the perturbation has teeth somewhere

    base_events = _events(inputs_from_state_series(SPEC.ref, base_points))
    changed_events = _events(inputs_from_state_series(SPEC.ref, changed_points))
    assert base_events.restricted_to(cut) == changed_events.restricted_to(cut)
    assert base_events.events != changed_events.events  # the perturbation reaches events too
