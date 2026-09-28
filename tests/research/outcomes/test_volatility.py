"""``research.outcomes.volatility`` (ADR-0088 decision 5 follow-up, outcome-library.md gap O-3):
wiring an answered ``FeatureResult`` into ``VolScaledTripleBarrierOutcome``'s volatility argument.

Covers: point-in-time selection never uses a feature value from after an event's entry time; a
``FeatureRequest.feature`` that does not match ``label_spec.volatility_feature`` is refused; a
``feature_result`` that does not answer the given ``feature_request`` is refused; an event with no
timely entry bar needs no feature lookup and maps straight to ``None``; a feature value that is
itself ``None`` maps to ``None``; a non-``Decimal`` feature value is refused; and the end-to-end
label produced through this wiring matches a hand-computed value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.feature import FeatureRequest, FeatureResult, FeatureValue, ProviderDescriptor
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeRequest,
)
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import OutcomeSpec
from plugins.outcomes import VolScaledTripleBarrierOutcome
from research.outcomes import VolatilityWiringError, select_volatility_for_entry_times

T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
MANIFEST = content_hash({"manifest": "test-vol-wiring"})
VOL_FEATURE = Ref(kind=Kind.FEATURE, name="realized_vol", version="1.0.0")
OTHER_FEATURE = Ref(kind=Kind.FEATURE, name="not_the_vol_feature", version="1.0.0")
SPEC_HASH = content_hash({"spec": "vol_feature"})

BAR0 = OutcomePriceBar(
    interval_start=T0,
    interval_end=T0 + MINUTE,
    available_time=T0 + MINUTE,
    open=Decimal(100),
    high=Decimal(101),
    low=Decimal(99),
    close=Decimal("100.5"),
)
BAR1 = OutcomePriceBar(
    interval_start=T0 + MINUTE,
    interval_end=T0 + 2 * MINUTE,
    available_time=T0 + 2 * MINUTE,
    open=Decimal("100.5"),
    high=Decimal(102),
    low=Decimal(100),
    close=Decimal(101),
)
BARS = (BAR0, BAR1)

#: e0's entry bar is BAR0 (entry_time == T0); e1's is BAR1 (entry_time == T0 + MINUTE).
E0 = OutcomeEvent(event_key="e0", event_time=T0)
E1 = OutcomeEvent(event_key="e1", event_time=T0 + MINUTE)


def _outcome_spec(horizon: timedelta = MINUTE) -> OutcomeSpec:
    return OutcomeSpec(
        name="vst_wiring", version="1.0.0", created_at=T0, horizon=horizon, label_definition="t"
    )


def _label_spec(
    *, horizon: timedelta = MINUTE, feature: Ref = VOL_FEATURE, multiplier: Decimal = Decimal("2")
) -> OutcomeLabelSpec:
    return OutcomeLabelSpec.bind(
        _outcome_spec(horizon),
        OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER,
        volatility_feature=feature,
        barrier_multiplier=multiplier,
    )


def _outcome_request(
    *, label_spec: OutcomeLabelSpec | None = None, events: tuple[OutcomeEvent, ...] = (E0, E1)
) -> OutcomeRequest:
    return OutcomeRequest(
        label_spec=label_spec or _label_spec(),
        manifest_content_hash=MANIFEST,
        price_cutoff=BARS[-1].available_time,
        events=events,
        bars=BARS,
    )


def _descriptor(feature: Ref = VOL_FEATURE, spec_hash: str = SPEC_HASH) -> ProviderDescriptor:
    return ProviderDescriptor(
        name="test_vol_feature_provider",
        version="1.0.0",
        deterministic=True,
        supported_features=FrozenMapping({str(feature): spec_hash}),
    )


def _feature_request(
    *,
    feature: Ref = VOL_FEATURE,
    evaluation_times: tuple[datetime, ...],
    knowledge_cutoff: datetime = T0 + timedelta(hours=1),
) -> FeatureRequest:
    return FeatureRequest(
        feature=feature,
        spec_hash=SPEC_HASH,
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=knowledge_cutoff,
        evaluation_times=evaluation_times,
        observations=(),
    )


def _value(at: datetime, value: Decimal | None) -> FeatureValue:
    if value is None:
        return FeatureValue(evaluation_time=at, value=None, inputs_used=0)
    return FeatureValue(
        evaluation_time=at, value=value, inputs_used=1, latest_input_available_time=at
    )


def _feature_result(request: FeatureRequest, values: list[FeatureValue]) -> FeatureResult:
    return FeatureResult.build(request, _descriptor(request.feature), values)


# ------------------------------------------------------------------ point-in-time selection


def test_pit_selection_never_uses_a_value_from_after_the_entry_time() -> None:
    # e0's entry_time is T0, e1's is T0 + MINUTE. A value timestamped strictly after e1's entry
    # time must never be selected for either event, even though it is the temporally closest one.
    times = (T0, T0 + MINUTE, T0 + 5 * MINUTE)
    request = _feature_request(evaluation_times=times)
    result = _feature_result(
        request,
        [
            _value(T0, Decimal("0.01")),
            _value(T0 + MINUTE, Decimal("0.02")),
            _value(T0 + 5 * MINUTE, Decimal("0.99")),  # after both entry times: must be unused
        ],
    )
    mapping = select_volatility_for_entry_times(_outcome_request(), request, result)
    assert mapping == {"e0": Decimal("0.01"), "e1": Decimal("0.02")}


def test_pit_selection_falls_back_to_the_latest_value_at_or_before_entry_time() -> None:
    # With no value exactly at e1's entry_time (T0 + MINUTE), e1 must still pick e0's value (T0),
    # not the future one (T0 + 5 * MINUTE) — proving the selection looks backward, never forward.
    times = (T0, T0 + 5 * MINUTE)
    request = _feature_request(evaluation_times=times)
    result = _feature_result(
        request, [_value(T0, Decimal("0.01")), _value(T0 + 5 * MINUTE, Decimal("0.99"))]
    )
    mapping = select_volatility_for_entry_times(_outcome_request(), request, result)
    assert mapping == {"e0": Decimal("0.01"), "e1": Decimal("0.01")}


def test_no_value_at_or_before_entry_time_is_an_explicit_none() -> None:
    # Only a future value exists: neither event may see it.
    times = (T0 + 5 * MINUTE,)
    request = _feature_request(evaluation_times=times)
    result = _feature_result(request, [_value(T0 + 5 * MINUTE, Decimal("0.99"))])
    mapping = select_volatility_for_entry_times(_outcome_request(), request, result)
    assert mapping == {"e0": None, "e1": None}


def test_a_feature_value_that_is_itself_none_maps_to_none() -> None:
    times = (T0,)
    request = _feature_request(evaluation_times=times)
    result = _feature_result(request, [_value(T0, None)])
    mapping = select_volatility_for_entry_times(_outcome_request(events=(E0,)), request, result)
    assert mapping == {"e0": None}


def test_an_event_with_no_timely_entry_bar_needs_no_feature_lookup() -> None:
    # event_time far past the last bar: label_window returns None, so the event maps to None
    # without any feature value existing to look up (an empty FeatureResult would be unanswerable
    # otherwise, since FeatureResult.values must be non-empty).
    late_event = OutcomeEvent(event_key="too-late", event_time=T0 + 10 * MINUTE)
    times = (T0,)
    request = _feature_request(evaluation_times=times)
    result = _feature_result(request, [_value(T0, Decimal("0.01"))])
    mapping = select_volatility_for_entry_times(
        _outcome_request(events=(late_event,)), request, result
    )
    assert mapping == {"too-late": None}


# ------------------------------------------------------------------ fail-closed wiring checks


def test_feature_ref_mismatch_is_rejected() -> None:
    request = _feature_request(feature=OTHER_FEATURE, evaluation_times=(T0,))
    result = _feature_result(request, [_value(T0, Decimal("0.01"))])
    with pytest.raises(VolatilityWiringError, match="does not match"):
        select_volatility_for_entry_times(_outcome_request(), request, result)


def test_feature_result_not_answering_the_given_request_is_rejected() -> None:
    request_a = _feature_request(evaluation_times=(T0,))
    request_b = _feature_request(evaluation_times=(T0, T0 + MINUTE))
    mismatched_result = _feature_result(
        request_b, [_value(T0, Decimal("0.01")), _value(T0 + MINUTE, Decimal("0.02"))]
    )
    with pytest.raises(VolatilityWiringError, match="request_hash"):
        select_volatility_for_entry_times(_outcome_request(), request_a, mismatched_result)


def test_label_spec_method_mismatch_is_rejected() -> None:
    forward_spec = OutcomeLabelSpec.bind(_outcome_spec(), OutcomeMethod.FORWARD_RETURN)
    request = _feature_request(evaluation_times=(T0,))
    result = _feature_result(request, [_value(T0, Decimal("0.01"))])
    outcome_request = _outcome_request(label_spec=forward_spec)
    with pytest.raises(VolatilityWiringError, match="method"):
        select_volatility_for_entry_times(outcome_request, request, result)


def test_non_decimal_feature_value_is_rejected() -> None:
    request = _feature_request(evaluation_times=(T0,))
    values = [
        FeatureValue(evaluation_time=T0, value=3, inputs_used=1, latest_input_available_time=T0)
    ]
    result = _feature_result(request, values)
    with pytest.raises(VolatilityWiringError, match="Decimal"):
        select_volatility_for_entry_times(_outcome_request(events=(E0,)), request, result)


# ------------------------------------------------------------------ end-to-end


def test_end_to_end_label_matches_the_hand_computed_value() -> None:
    """The full round trip — select volatility through this wiring, feed it to the Provider — must
    match hand-computed entry/exit/value, the same way ``test_vol_scaled_triple_barrier.py``
    checks the Provider directly (here: multiplier(2) * volatility(0.005) == 0.01, barriers
    [99, 101] over BAR0; BAR0.high == 101 touches the upper barrier)."""
    request = _feature_request(evaluation_times=(T0,))
    result = _feature_result(request, [_value(T0, Decimal("0.005"))])
    outcome_request = _outcome_request(
        label_spec=_label_spec(multiplier=Decimal("2")), events=(E0,)
    )

    volatility = select_volatility_for_entry_times(outcome_request, request, result)
    assert volatility == {"e0": Decimal("0.005")}

    provider = VolScaledTripleBarrierOutcome((outcome_request.label_spec,), volatility=volatility)
    label = provider.compute(outcome_request).labels[0]

    # Hand computation: entry = BAR0.open = 100; scale = multiplier(2) * volatility(0.005) = 0.01,
    # so upper = 100 * 1.01 = 101, lower = 100 * 0.99 = 99. BAR0.low (99) touches the lower barrier
    # and BAR0.high (101) touches the upper one in the same bar; triple_barrier's pessimistic rule
    # resolves same-bar double touches to the lower barrier, so exit_price == lower == 99.
    assert label.barrier == -1
    assert label.entry_price == Decimal(100)
    assert label.exit_price == Decimal(99)
    assert label.value == Decimal("-0.010000000000000000")
