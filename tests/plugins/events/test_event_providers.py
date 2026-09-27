"""First EventProviders (Phase 3; ADR-0036): contract suite, hand-checked events, spec binding."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.event import (
    Event,
    EventInputError,
    EventProvider,
    EventResult,
    UnsupportedEvent,
)
from core.domain.specs import EventSpec
from plugins.events import (
    EventCoOccurrenceProvider,
    EventSequenceProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
    VolatilityBreakoutProvider,
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
    perturb_label,
    perturb_number,
    request,
)

CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
CROSS_BOTH = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)
BREAKOUT = VolatilityBreakoutProvider.spec(X, 2, Decimal("1.5"), observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)
TO_WILD = StateSwitchProvider.spec(
    REGIME, to_state="wild", name="regime_to_wild", observable_lag=LAG
)
SEQUENCE = EventSequenceProvider.spec(CROSS_UP, SWITCH, 3 * MINUTE, observable_lag=LAG)
CO_OCCUR = EventCoOccurrenceProvider.spec(CROSS_UP, SWITCH, MINUTE, observable_lag=LAG)


def _events(provider: EventProvider, spec: EventSpec, **fields: Any) -> tuple[Event, ...]:
    result = provider.detect(request(spec, **fields))
    assert isinstance(result, EventResult)
    return result.events


UPSTREAM: tuple[Event, ...] = _events(
    FeatureThresholdCrossProvider((CROSS_UP,)), CROSS_UP, inputs=X_INPUTS
) + _events(StateSwitchProvider((SWITCH,)), SWITCH, inputs=REGIME_INPUTS)


class TestThresholdCrossContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: FeatureThresholdCrossProvider((CROSS_BOTH,)),
            spec=CROSS_BOTH,
            as_of_times=AS_OF_TIMES,
            inputs=X_INPUTS,
            perturb=perturb_number,
        )


class TestVolatilityBreakoutContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: VolatilityBreakoutProvider((BREAKOUT,)),
            spec=BREAKOUT,
            as_of_times=AS_OF_TIMES,
            inputs=X_INPUTS,
            perturb=perturb_number,
        )


class TestStateSwitchContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: StateSwitchProvider((SWITCH, TO_WILD)),
            spec=SWITCH,
            as_of_times=AS_OF_TIMES,
            inputs=REGIME_INPUTS,
            perturb=perturb_label,
        )


class TestEventSequenceContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: EventSequenceProvider((SEQUENCE,)),
            spec=SEQUENCE,
            as_of_times=AS_OF_TIMES,
            upstream_events=UPSTREAM,
        )


class TestEventCoOccurrenceContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        return EventSubject(
            open=lambda: EventCoOccurrenceProvider((CO_OCCUR,)),
            spec=CO_OCCUR,
            as_of_times=AS_OF_TIMES,
            upstream_events=UPSTREAM,
        )


# ======================================================================================
# Hand-checked events
# ======================================================================================


def _minutes(events: tuple[Event, ...]) -> list[int]:
    return [(item.event_time - T0) // MINUTE for item in events]


def test_threshold_cross_events_are_hand_checked() -> None:
    both = _events(FeatureThresholdCrossProvider((CROSS_BOTH,)), CROSS_BOTH, inputs=X_INPUTS)
    # x: 1 2 4 6 3 5 7 2 None 8 9 1 ; minutes 5, 6 arrive at 7; event = arrival + 1 min lag.
    assert sorted(
        (m, str(e.attributes["direction"])) for m, e in zip(_minutes(both), both, strict=True)
    ) == [
        (4, "up"),  # 4 -> 6 at minute 3
        (5, "down"),  # 6 -> 3 at minute 4
        (8, "down"),  # 7 -> 2 at minute 7
        (8, "up"),  # 3 -> 5 at minute 5 (arrives at 7)
        (12, "down"),  # 9 -> 1 at minute 11 (2 -> None -> 8 is not a pair)
    ]
    first = both[0]
    assert first.attributes["previous"] == Decimal(4) and first.attributes["value"] == Decimal(6)
    assert first.input_ids == tuple(sorted((X_INPUTS[2].point_id, X_INPUTS[3].point_id)))


def test_volatility_breakout_is_hand_checked() -> None:
    events = _events(VolatilityBreakoutProvider((BREAKOUT,)), BREAKOUT, inputs=X_INPUTS)
    # window 2, multiplier 1.5: x[i] * 2 > 1.5 * (x[i-1] + x[i-2]) and not at i - 1.
    # i=2: 4*2=8 > 1.5*3 yes, i=1 needs 3 points -> first decision at i=3 (6*2=12 > 9: above, but
    # i=2 was above too -> no event); i=6: 7*2=14 > 1.5*8=12 above, i=5: 10 > 13.5 no -> event.
    assert _minutes(events) == [8]  # minute 6 arrives at 7, + lag
    assert events[0].attributes["baseline"] == Decimal("4.000000000000000000")
    assert len(events[0].input_ids) == 4


def test_state_switch_is_hand_checked() -> None:
    events = _events(StateSwitchProvider((SWITCH,)), SWITCH, inputs=REGIME_INPUTS)
    assert [
        (m, e.attributes["from_state"], e.attributes["to_state"])
        for m, e in zip(_minutes(events), events, strict=True)
    ] == [(3, "calm", "wild"), (8, "wild", "calm"), (9, "calm", "wild"), (11, "wild", "calm")]
    to_wild = _events(StateSwitchProvider((TO_WILD,)), TO_WILD, inputs=REGIME_INPUTS)
    assert _minutes(to_wild) == [3, 9]


def test_interactions_trace_their_upstream_events() -> None:
    by_id = {item.event_id: item for item in UPSTREAM}
    sequence = _events(EventSequenceProvider((SEQUENCE,)), SEQUENCE, upstream_events=UPSTREAM)
    assert sequence, "the fixture has cross-up events followed by switches"
    for item in sequence:
        assert item.input_ids == ()
        first, then = sorted(
            (by_id[event_id] for event_id in item.upstream_event_ids),
            key=lambda event: event.event_time,
        )
        assert (first.event, then.event) == (CROSS_UP.ref, SWITCH.ref)
        assert first.event_time < then.event_time <= first.event_time + 3 * MINUTE
        assert item.event_time == then.event_time + LAG
    co = _events(EventCoOccurrenceProvider((CO_OCCUR,)), CO_OCCUR, upstream_events=UPSTREAM)
    for item in co:
        linked = [by_id[event_id] for event_id in item.upstream_event_ids]
        assert {event.event for event in linked} == {CROSS_UP.ref, SWITCH.ref}
        times = sorted(event.event_time for event in linked)
        assert times[1] - times[0] <= MINUTE
        assert item.event_time == times[1] + LAG


def test_interactions_refuse_foreign_upstream_events() -> None:
    foreign = _events(StateSwitchProvider((TO_WILD,)), TO_WILD, inputs=REGIME_INPUTS)
    with pytest.raises(EventInputError):
        EventSequenceProvider((SEQUENCE,)).detect(
            request(SEQUENCE, upstream_events=UPSTREAM + foreign)
        )


# ======================================================================================
# Spec binding
# ======================================================================================


def test_every_parameter_is_bound_by_the_spec_hash() -> None:
    hashes = {
        spec.content_hash()
        for spec in (
            CROSS_UP,
            CROSS_BOTH,
            FeatureThresholdCrossProvider.spec(X, Decimal("4.6"), "up", observable_lag=LAG),
            FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up"),
            BREAKOUT,
            VolatilityBreakoutProvider.spec(X, 3, Decimal("1.5"), observable_lag=LAG),
            VolatilityBreakoutProvider.spec(X, 2, Decimal("2"), observable_lag=LAG),
            SEQUENCE,
            EventSequenceProvider.spec(CROSS_UP, SWITCH, 2 * MINUTE, observable_lag=LAG),
        )
    }
    assert len(hashes) == 9
    assert SEQUENCE.lineage == (CROSS_UP.ref, SWITCH.ref)
    assert SEQUENCE.features == (X,) and SEQUENCE.states == (REGIME,)


def test_a_spec_the_provider_would_not_build_is_refused() -> None:
    tampered = CROSS_UP.model_copy(update={"trigger": CROSS_UP.trigger.replace(",", ", ")})
    with pytest.raises(ValueError, match="not a feature_threshold_cross spec"):
        FeatureThresholdCrossProvider((tampered,))
    with pytest.raises(ValueError):
        StateSwitchProvider((CROSS_UP,))
    with pytest.raises(ValueError):
        EventSequenceProvider((SEQUENCE.model_copy(update={"lineage": ()}),))


def test_undeclared_spec_is_unsupported() -> None:
    with pytest.raises(UnsupportedEvent):
        FeatureThresholdCrossProvider((CROSS_UP,)).detect(request(CROSS_BOTH, inputs=X_INPUTS))


def test_wrong_input_kinds_fail_closed() -> None:
    with pytest.raises(EventInputError):
        bad = tuple(
            item.model_copy(update={"source": REGIME})
            for item in X_INPUTS
            if item.value is not None
        )
        StateSwitchProvider((SWITCH,)).detect(request(SWITCH, inputs=bad))


def test_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        EventSequenceProvider.spec(CROSS_UP, SWITCH, timedelta(0))
    with pytest.raises(ValueError):
        EventSequenceProvider.spec(CROSS_UP, CROSS_UP, MINUTE)
