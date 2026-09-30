"""Feature-relative event thresholding (ADR-0036; EVT-2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import pytest

from core.contracts.event import Event, EventInputError, EventInputPoint, EventRequest
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import EventSpec
from plugins.events import FeatureRelativeThresholdCrossProvider
from tests.contract_suites.event import EventProviderContract, EventSubject

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
STEP = timedelta(minutes=1)
LAG = timedelta(minutes=1)
RETURN = Ref(kind=Kind.FEATURE, name="test_return", version="1.0.0")
VOL = Ref(kind=Kind.FEATURE, name="test_vol", version="1.0.0")
OTHER = Ref(kind=Kind.FEATURE, name="test_other", version="1.0.0")


def _point(source: Ref, minute: int, value: Decimal | int | None) -> EventInputPoint:
    evaluation_time = T0 + minute * STEP
    return EventInputPoint(
        source=source,
        source_lineage_hash=content_hash(
            {
                "source": str(source),
                "minute": minute,
                "value": None if value is None else str(value),
            }
        ),
        evaluation_time=evaluation_time,
        available_time=evaluation_time + timedelta(seconds=30),
        value=value,
    )


def _inputs(
    feature_values: tuple[Decimal | int | None, ...],
    level_values: tuple[Decimal | int | None, ...],
) -> tuple[EventInputPoint, ...]:
    return tuple(
        [
            *(_point(RETURN, index, value) for index, value in enumerate(feature_values)),
            *(_point(VOL, index, value) for index, value in enumerate(level_values)),
        ]
    )


def _spec(
    direction: Literal["up", "down", "both"] = "both",
    *,
    multiplier: Decimal = Decimal("3"),
    lag: timedelta = LAG,
) -> EventSpec:
    return FeatureRelativeThresholdCrossProvider.spec(
        RETURN, VOL, multiplier, direction, observable_lag=lag
    )


def _detect(
    spec: EventSpec, inputs: tuple[EventInputPoint, ...], as_of: datetime
) -> tuple[Event, ...]:
    request = EventRequest(
        event=spec.ref,
        spec_hash=spec.content_hash(),
        as_of=as_of,
        inputs=inputs,
    )
    return FeatureRelativeThresholdCrossProvider((spec,)).detect(request).events


class TestFeatureRelativeThresholdContract(EventProviderContract):
    @pytest.fixture
    def event_subject(self) -> EventSubject:
        spec = _spec()
        inputs = _inputs(
            tuple(map(Decimal, ("0.01", "0.02", "0.005", "0.03", "0.01", "0.05"))),
            (Decimal("0.005"),) * 6,
        )
        return EventSubject(
            open=lambda: FeatureRelativeThresholdCrossProvider((spec,)),
            spec=spec,
            as_of_times=tuple(T0 + index * STEP + LAG for index in range(1, 8)),
            inputs=inputs,
            perturb=lambda item: item.model_copy(update={"value": cast(Any, item.value) * 2 + 1}),
        )


def test_up_cross_uses_absolute_feature_and_exact_relative_level() -> None:
    inputs = _inputs(
        (Decimal("-0.01"), Decimal("-0.02")),
        (Decimal("0.005"), Decimal("0.005")),
    )
    events = _detect(_spec("up", lag=timedelta(seconds=30)), inputs, T0 + 2 * STEP)
    assert len(events) == 1
    event = events[0]
    assert event.attributes["direction"] == "up"
    assert event.attributes["feature_value"] == Decimal("-0.02")
    assert event.attributes["level_value"] == Decimal("0.005")
    assert event.attributes["threshold"] == Decimal("0.015")
    assert event.event_time == T0 + STEP + timedelta(seconds=60)
    assert len(event.input_ids) == 4


def test_down_cross_uses_strict_current_boundary() -> None:
    spec = _spec("down", lag=timedelta(seconds=30))
    exact_boundary = _inputs(
        (Decimal("0.02"), Decimal("0.015")),
        (Decimal("0.005"), Decimal("0.005")),
    )
    below = _inputs(
        (Decimal("0.02"), Decimal("0.0149")),
        (Decimal("0.005"), Decimal("0.005")),
    )
    assert _detect(spec, exact_boundary, T0 + 2 * STEP) == ()
    (event,) = _detect(spec, below, T0 + 2 * STEP)
    assert event.attributes["direction"] == "down"


def test_dynamic_level_can_cause_a_crossing_with_unchanged_feature() -> None:
    inputs = _inputs(
        (Decimal("0.02"), Decimal("0.02")),
        (Decimal("0.01"), Decimal("0.005")),
    )
    (event,) = _detect(_spec("up", lag=timedelta(seconds=30)), inputs, T0 + 2 * STEP)
    assert event.attributes["threshold"] == Decimal("0.015")


def test_equal_threshold_is_not_an_up_cross() -> None:
    inputs = _inputs(
        (Decimal("0.01"), Decimal("0.015")),
        (Decimal("0.005"), Decimal("0.005")),
    )
    assert _detect(_spec("up"), inputs, T0 + 2 * STEP) == ()


def test_non_shared_times_and_none_values_break_pairs() -> None:
    spec = _spec("both", lag=timedelta(seconds=30))
    unaligned = (
        _point(RETURN, 0, Decimal("0.01")),
        _point(RETURN, 1, Decimal("0.02")),
        _point(VOL, 0, Decimal("0.005")),
        _point(VOL, 2, Decimal("0.005")),
    )
    missing = _inputs(
        (Decimal("0.01"), None, Decimal("0.02")),
        (Decimal("0.005"), Decimal("0.005"), Decimal("0.005")),
    )
    assert _detect(spec, unaligned, T0 + 3 * STEP) == ()
    assert _detect(spec, missing, T0 + 3 * STEP) == ()


def test_no_future_points_affect_an_earlier_as_of() -> None:
    early = _inputs(
        (Decimal("0.01"), Decimal("0.02")),
        (Decimal("0.005"), Decimal("0.005")),
    )
    full = (*early, _point(RETURN, 2, Decimal("999")), _point(VOL, 2, Decimal("0.001")))
    as_of = T0 + 2 * STEP
    assert _detect(_spec(), early, as_of) == _detect(_spec(), full, as_of)


def test_multiplier_is_required_positive_finite_decimal_and_features_are_distinct() -> None:
    with pytest.raises(TypeError):
        FeatureRelativeThresholdCrossProvider.spec(RETURN, VOL)  # type: ignore[call-arg]
    for multiplier in (Decimal("0"), Decimal("-1"), Decimal("NaN")):
        with pytest.raises(ValueError, match="positive finite Decimal"):
            FeatureRelativeThresholdCrossProvider.spec(RETURN, VOL, multiplier, "up")
    with pytest.raises(ValueError, match="distinct"):
        FeatureRelativeThresholdCrossProvider.spec(RETURN, RETURN, Decimal("2"), "up")
    with pytest.raises(ValueError, match="direction"):
        FeatureRelativeThresholdCrossProvider.spec(RETURN, VOL, Decimal("2"), cast(Any, "sideways"))


def test_non_exact_decimal_comparison_fails_closed() -> None:
    large = Decimal("1." + "1" * 90)
    twice_large = Decimal("2." + "2" * 90)
    inputs = _inputs((large, twice_large), (large, large))
    spec = _spec("both", multiplier=Decimal("3"))
    with pytest.raises(EventInputError, match="not exact"):
        _detect(spec, inputs, T0 + 3 * STEP)


def test_inputs_from_another_feature_fail_closed() -> None:
    inputs = (
        _point(RETURN, 0, Decimal("0.01")),
        _point(VOL, 0, Decimal("0.005")),
        _point(OTHER, 0, Decimal("1")),
    )
    with pytest.raises(EventInputError, match="outside the spec's features"):
        _detect(_spec(), inputs, T0 + 3 * STEP)
