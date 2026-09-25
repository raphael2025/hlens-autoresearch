"""Upstream results → strategy signals (Phase 5 wiring; ADR-0038)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.contracts.event import Event
from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateProviderDescriptor, StateRequest, StateResult, StateValue
from core.contracts.strategy import SignalObservation, StrategyRequest
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import FeatureSpec
from infrastructure.feature.runner import run_feature
from infrastructure.strategy import (
    SignalAdapterError,
    signals_from_events,
    signals_from_features,
    signals_from_states,
)
from tests.fake_events import X, x_point
from tests.fake_features import (
    CUTOFF,
    EVALUATION_TIMES,
    MANIFEST,
    OBSERVATIONS,
    LatestValueProvider,
    fake_spec,
)
from tests.fake_states import VOL_FEATURE, at, value_input

INSTRUMENT = "BTCUSDT"
STATE = Ref(kind=Kind.STATE, name="regime", version="1.0.0")
EVENT = Ref(kind=Kind.EVENT, name="cross", version="1.0.0")
STRATEGY = Ref(kind=Kind.STRATEGY, name="s", version="1.0.0")
OUTCOME = Ref(kind=Kind.OUTCOME, name="y", version="1.0.0")


def _feature_result() -> tuple[FeatureSpec, FeatureResult]:
    spec = fake_spec("latest_x")
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=EVALUATION_TIMES,
        observations=OBSERVATIONS,
    )
    return spec, run_feature(LatestValueProvider([spec]), spec, request)


def _state_result() -> StateResult:
    spec_hash = content_hash({"spec": "regime"})
    descriptor = StateProviderDescriptor(
        name="fixture",
        version="1.0.0",
        deterministic=True,
        supported_states=FrozenMapping({str(STATE): spec_hash}),
    )
    request = StateRequest(
        state=STATE,
        spec_hash=spec_hash,
        evaluation_times=(at(1), at(2), at(3)),
        inputs=tuple(value_input(VOL_FEATURE, i, Decimal(i)) for i in range(4)),
    )
    values = [
        StateValue(evaluation_time=at(1), state="calm", inputs_used=1, latest_input_time=at(1)),
        StateValue(evaluation_time=at(2), state=None, inputs_used=0),
        StateValue(evaluation_time=at(3), state="wild", inputs_used=1, latest_input_time=at(3)),
    ]
    return StateResult.build(request, descriptor, values)


def _event(minute: int, **attributes: object) -> Event:
    point = x_point(minute, minute)
    return Event.build(
        event=EVENT,
        spec_hash=content_hash({"spec": "cross"}),
        event_time=point.available_time,
        attributes=dict(attributes),
        inputs=(point,),
    )


def test_feature_values_become_signals_available_at_their_evaluation_time() -> None:
    spec, result = _feature_result()
    signals = signals_from_features(
        result, feature=spec.ref, instrument=INSTRUMENT, knowledge_time=CUTOFF
    )
    assert len(signals) == len(result.values)
    for signal, value in zip(signals, result.values, strict=True):
        assert signal.signal == spec.ref
        assert signal.event_time == signal.available_time == value.evaluation_time
        assert signal.knowledge_time == CUTOFF
        assert signal.value == value.value
    assert any(v.value is None for v in result.values), "fixture must include an explicit None"


def test_state_labels_keep_explicit_none() -> None:
    signals = signals_from_states(
        _state_result(), state=STATE, instrument=INSTRUMENT, knowledge_time=at(3)
    )
    assert [s.value for s in signals] == ["calm", None, "wild"]
    assert all(s.signal.kind is Kind.STATE for s in signals)


def test_events_are_available_when_observable() -> None:
    events = (_event(2, direction="up"), _event(3, direction="down"))
    flags = signals_from_events(events, instrument=INSTRUMENT, knowledge_time=at(9))
    assert [s.value for s in flags] == [True, True]
    assert [s.available_time for s in flags] == [e.event_time for e in events]
    directions = signals_from_events(
        events, instrument=INSTRUMENT, knowledge_time=at(9), value_attribute="direction"
    )
    assert [s.value for s in directions] == ["up", "down"]
    with pytest.raises(SignalAdapterError, match="没有属性"):
        signals_from_events(
            events, instrument=INSTRUMENT, knowledge_time=at(9), value_attribute="missing"
        )


def test_wrong_kinds_and_outcomes_are_refused() -> None:
    spec, result = _feature_result()
    with pytest.raises(SignalAdapterError, match="kind=feature"):
        signals_from_features(result, feature=OUTCOME, instrument=INSTRUMENT, knowledge_time=CUTOFF)
    with pytest.raises(SignalAdapterError, match="kind=state"):
        signals_from_states(_state_result(), state=X, instrument=INSTRUMENT, knowledge_time=at(3))
    with pytest.raises(ValidationError, match="C-L2"):
        SignalObservation(
            signal=OUTCOME,
            instrument=INSTRUMENT,
            event_time=at(1),
            available_time=at(1),
            knowledge_time=at(1),
            value=Decimal(1),
        )


def test_a_knowledge_time_before_the_results_is_refused() -> None:
    with pytest.raises(SignalAdapterError, match="knowledge_time"):
        signals_from_states(
            _state_result(), state=STATE, instrument=INSTRUMENT, knowledge_time=at(2)
        )
    with pytest.raises(SignalAdapterError, match="knowledge_time"):
        signals_from_events((_event(5),), instrument=INSTRUMENT, knowledge_time=at(1))


def test_signals_feed_a_strategy_request_and_stay_causal() -> None:
    signals = signals_from_states(
        _state_result(), state=STATE, instrument=INSTRUMENT, knowledge_time=at(3)
    )
    request = StrategyRequest(
        strategy=STRATEGY,
        spec_hash=content_hash({"spec": "s"}),
        instruments=(INSTRUMENT,),
        knowledge_cutoff=at(3),
        decision_times=(at(2), at(3)),
        signals=signals,
    )
    assert [s.value for s in request.visible_at(at(2))] == ["calm", None]
    assert all(s.available_time <= at(2) for s in request.visible_at(at(2)))
    assert at(3) - at(2) == timedelta(minutes=1)
