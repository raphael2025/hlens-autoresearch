"""Window interaction providers of the DSL (ADR-0061): contract suite, hand-checked, causality."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.event import Event, EventInputError, EventProvider, EventResult
from core.domain.base import Kind, Ref
from core.domain.specs import EventSpec
from plugins.events import (
    EventAbsenceProvider,
    EventCountProvider,
    EventWindowEndProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
)
from tests.contract_suites.event import EventProviderContract, EventSubject
from tests.fake_events import (
    AS_OF_TIMES,
    LAG,
    MINUTE,
    REGIME,
    REGIME_INPUTS,
    T0,
    X_INPUTS,
    X,
    request,
)

CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)
WINDOW_END = EventWindowEndProvider.spec(CROSS_UP, 2 * MINUTE, name="up_window_end")
ABSENCE = EventAbsenceProvider.spec(
    CROSS_UP, SWITCH, timedelta(seconds=30), name="up_without_switch", observable_lag=LAG
)
COUNT = EventCountProvider.spec(SWITCH, 2, 3 * MINUTE, name="switch_twice", observable_lag=LAG)


def _events(provider: EventProvider, spec: EventSpec, **fields: Any) -> tuple[Event, ...]:
    result = provider.detect(request(spec, **fields))
    assert isinstance(result, EventResult)
    return result.events


#: cross-up events at minutes 4, 8; switch events at minutes 3, 8, 9, 11 (lag included).
UP_EVENTS = _events(FeatureThresholdCrossProvider((CROSS_UP,)), CROSS_UP, inputs=X_INPUTS)
SWITCH_EVENTS = _events(StateSwitchProvider((SWITCH,)), SWITCH, inputs=REGIME_INPUTS)


def _minutes(events: tuple[Event, ...]) -> list[Decimal]:
    return [Decimal((item.event_time - T0) // timedelta(seconds=1)) / 60 for item in events]


def test_the_fixture_is_what_the_hand_checks_assume() -> None:
    assert _minutes(UP_EVENTS) == [4, 8]
    assert _minutes(SWITCH_EVENTS) == [3, 8, 9, 11]


# ---------------------------------------------------------------- contract suite


class TestWindowEndContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: EventWindowEndProvider((WINDOW_END,)),
            spec=WINDOW_END,
            as_of_times=AS_OF_TIMES,
            upstream_events=UP_EVENTS,
        )


class TestAbsenceContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: EventAbsenceProvider((ABSENCE,)),
            spec=ABSENCE,
            as_of_times=AS_OF_TIMES,
            upstream_events=UP_EVENTS + SWITCH_EVENTS,
        )


class TestCountContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: EventCountProvider((COUNT,)),
            spec=COUNT,
            as_of_times=AS_OF_TIMES,
            upstream_events=SWITCH_EVENTS,
        )


# ---------------------------------------------------------------- hand-checked events


def test_window_end_is_dated_at_the_end_of_each_window() -> None:
    events = _events(EventWindowEndProvider((WINDOW_END,)), WINDOW_END, upstream_events=UP_EVENTS)
    assert _minutes(events) == [6, 10]
    for item, source in zip(events, UP_EVENTS, strict=True):
        assert item.upstream_event_ids == (source.event_id,)
        assert item.event_time == source.event_time + 2 * MINUTE
        assert item.attributes["window_seconds"] == Decimal(120)
    assert WINDOW_END.observable_lag == 2 * MINUTE


def test_window_operators_inherit_bar_spec_and_reject_mismatched_upstreams() -> None:
    bar = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")
    other_bar = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_5m", version="1.0.0")
    first = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), bar_spec=bar)
    second = StateSwitchProvider.spec(REGIME, bar_spec=bar)
    mismatch = StateSwitchProvider.spec(REGIME, bar_spec=other_bar)

    window_end = EventWindowEndProvider.spec(first, MINUTE, name="window_end")
    absence = EventAbsenceProvider.spec(first, second, MINUTE, name="absence")
    count = EventCountProvider.spec(second, 2, MINUTE, name="count")
    assert window_end.bar_spec == absence.bar_spec == count.bar_spec == bar
    assert EventWindowEndProvider((window_end,))
    assert EventAbsenceProvider((absence,))
    assert EventCountProvider((count,))

    with pytest.raises(ValueError, match="bar_spec values must agree"):
        EventAbsenceProvider.spec(first, mismatch, MINUTE, name="bad_absence")
    with pytest.raises(ValueError, match="bar_spec must match"):
        EventCountProvider.spec(second, 2, MINUTE, name="bad_count", bar_spec=other_bar)


def test_absence_is_hand_checked() -> None:
    upstream = UP_EVENTS + SWITCH_EVENTS
    events = _events(EventAbsenceProvider((ABSENCE,)), ABSENCE, upstream_events=upstream)
    # up@4: no switch in [3.5, 4] -> event at 4 + lag; up@8: switch@8 (inclusive) -> none.
    assert _minutes(events) == [5]
    assert events[0].upstream_event_ids == (UP_EVENTS[0].event_id,)
    wide = EventAbsenceProvider.spec(CROSS_UP, SWITCH, MINUTE, name="up_without_switch_1m")
    # up@4: switch@3 at exactly window start (inclusive) -> none.
    assert _events(EventAbsenceProvider((wide,)), wide, upstream_events=upstream) == ()


def test_count_is_hand_checked() -> None:
    events = _events(EventCountProvider((COUNT,)), COUNT, upstream_events=SWITCH_EVENTS)
    # switches 3, 8, 9, 11; window 3 min: at 9 -> {8, 9}; at 11 -> {8, 9, 11}; + 1 min lag.
    assert _minutes(events) == [10, 12]
    assert [item.attributes["count"] for item in events] == [2, 3]
    assert set(events[1].upstream_event_ids) == {item.event_id for item in SWITCH_EVENTS[1:]}


def test_absence_and_the_window_end_never_see_the_future() -> None:
    """A B after the window (or a B that only arrives later) never retracts an absence event."""
    provider = EventAbsenceProvider((ABSENCE,))
    base = _events(provider, ABSENCE, upstream_events=UP_EVENTS + SWITCH_EVENTS)
    later = _events(StateSwitchProvider((SWITCH,)), SWITCH, inputs=REGIME_INPUTS)
    # Drop every switch after the first absence event: the past table is unchanged.
    cut = base[0].event_time
    kept = tuple(item for item in later if item.event_time <= cut)
    again = _events(provider, ABSENCE, upstream_events=UP_EVENTS + kept)
    assert tuple(item for item in again if item.event_time <= cut) == base[:1]


def test_foreign_or_point_inputs_are_refused() -> None:
    with pytest.raises(EventInputError):
        EventCountProvider((COUNT,)).detect(request(COUNT, upstream_events=UP_EVENTS))
    with pytest.raises(EventInputError):
        EventWindowEndProvider((WINDOW_END,)).detect(
            request(WINDOW_END, upstream_events=SWITCH_EVENTS)
        )


# ---------------------------------------------------------------- spec binding


def test_specs_bind_every_parameter_and_refuse_non_canonical_forms() -> None:
    assert WINDOW_END.lineage == (CROSS_UP.ref,) and WINDOW_END.features == (X,)
    assert ABSENCE.lineage == (CROSS_UP.ref, SWITCH.ref)
    assert ABSENCE.features == (X,) and ABSENCE.states == (REGIME,)
    hashes = {
        spec.content_hash()
        for spec in (
            COUNT,
            EventCountProvider.spec(SWITCH, 3, 3 * MINUTE, name="switch_twice", observable_lag=LAG),
            EventCountProvider.spec(SWITCH, 2, 2 * MINUTE, name="switch_twice", observable_lag=LAG),
            ABSENCE,
            EventAbsenceProvider.spec(
                CROSS_UP, SWITCH, MINUTE, name="up_without_switch", observable_lag=LAG
            ),
        )
    }
    assert len(hashes) == 5
    with pytest.raises(ValueError, match="observable_lag must be exactly the window"):
        EventWindowEndProvider((WINDOW_END.model_copy(update={"observable_lag": MINUTE}),))
    with pytest.raises(ValueError, match="trigger fields"):
        EventCountProvider(
            (COUNT.model_copy(update={"trigger": COUNT.trigger.replace('"at_least":2,', "")}),)
        )
    with pytest.raises(ValueError):
        EventAbsenceProvider((ABSENCE.model_copy(update={"lineage": (SWITCH.ref, CROSS_UP.ref)}),))
    with pytest.raises(ValueError):
        EventCountProvider.spec(SWITCH, 0, MINUTE, name="bad")
    with pytest.raises(ValueError):
        EventAbsenceProvider.spec(SWITCH, SWITCH, MINUTE, name="bad")
    with pytest.raises(ValueError):
        EventWindowEndProvider.spec(SWITCH, timedelta(0), name="bad")
