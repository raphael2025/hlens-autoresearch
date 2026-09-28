"""EventProviders over one feature series (Phase 3; ADR-0036).

- ``FeatureThresholdCrossProvider`` (``feature_threshold_cross``): the feature crosses a level
  between two consecutive series points — ``up``: ``previous <= level < value``; ``down``:
  ``previous >= level > value``; ``both``: either;
- ``FeatureRelativeThresholdCrossProvider`` (``feature_relative_threshold_cross``): the absolute
  value of one feature crosses ``multiplier * level_feature`` on shared evaluation times;
- ``VolatilityBreakoutProvider`` (``volatility_breakout``): a (volatility) feature enters the region
  ``value > multiplier * mean(previous window values)``: above at point ``i`` and not above at
  ``i - 1``. Uses the ``window + 2`` latest points, all computable.

A level, a window and a multiplier are event-definition parameters bound by the spec hash; they
are not validation thresholds. Consecutive means adjacent in the visible series (``evaluation_time``
order); the relative-threshold provider pairs features only at shared evaluation times. A ``None``
(not computable) point breaks a pair / window, nothing is filled.

Each event's ``event_time`` is the ``available_time`` of its latest point plus the spec's
``observable_lag`` — the moment the crossing becomes observable. No event uses later points.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any, ClassVar, Final, Literal

from core.contracts.event import Event, EventInputError, EventInputPoint
from core.domain.base import Kind, Ref
from core.domain.specs import EventSpec
from plugins.events._base import (
    EventProviderBase,
    numeric,
    parse_decimal,
    parse_ref,
    series,
    trigger_of,
)

__all__ = [
    "FeatureRelativeThresholdCrossProvider",
    "FeatureThresholdCrossProvider",
    "VolatilityBreakoutProvider",
]

Direction = Literal["up", "down", "both"]
_DIRECTIONS: Final = ("up", "down", "both")
#: Exact comparisons: an inexact step is an input error, never a silently rounded decision.
_EXACT: Final = Context(prec=80, traps=[Inexact, InvalidOperation, Overflow])
#: The reported baseline (an attribute, not a decision input): 50 digits, 18 places, half-even.
_BASELINE: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_BASELINE_QUANTUM: Final = Decimal(1).scaleb(-18)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


class FeatureThresholdCrossProvider(EventProviderBase):
    """A feature series crosses a fixed level."""

    NAME = "feature_threshold_cross"
    OPERATOR: ClassVar[str] = "feature_threshold_cross"

    @staticmethod
    def spec(
        feature: Ref,
        level: Decimal,
        direction: Direction = "up",
        *,
        name: str | None = None,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
        bar_spec: Ref | None = None,
    ) -> EventSpec:
        if feature.kind is not Kind.FEATURE:
            raise ValueError(f"{feature} is not a feature reference")
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        if not isinstance(level, Decimal) or not level.is_finite():
            raise ValueError("level must be a finite Decimal")
        return EventSpec(
            name=name or f"{feature.name}_cross_{direction}",
            version=version,
            trigger=trigger_of(
                FeatureThresholdCrossProvider.OPERATOR,
                {"feature": str(feature), "level": str(level), "direction": direction},
            ),
            features=(feature,),
            observable_lag=observable_lag,
            bar_spec=bar_spec,
        )

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        direction = params["direction"]
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        return self.spec(
            parse_ref(params["feature"], Kind.FEATURE),
            parse_decimal(params["level"], "level"),
            direction,
            name=spec.name,
            version=spec.version,
            observable_lag=spec.observable_lag,
            bar_spec=spec.bar_spec,
        )

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        if upstream:
            raise EventInputError(f"{spec.ref} takes no upstream events")
        feature = parse_ref(params["feature"], Kind.FEATURE)
        level = parse_decimal(params["level"], "level")
        direction: str = params["direction"]
        out: list[Event] = []
        line = series(points, feature)
        for previous, current in zip(line, line[1:], strict=False):
            before, value = numeric(previous), numeric(current)
            if before is None or value is None:
                continue
            crossed: str | None = None
            if direction in ("up", "both") and before <= level < value:
                crossed = "up"
            elif direction in ("down", "both") and before >= level > value:
                crossed = "down"
            if crossed is None:
                continue
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=current.available_time + spec.observable_lag,
                    attributes={
                        "direction": crossed,
                        "level": level,
                        "previous": before,
                        "value": value,
                    },
                    inputs=(previous, current),
                )
            )
        return out


class FeatureRelativeThresholdCrossProvider(EventProviderBase):
    """Cross ``abs(feature)`` against ``multiplier * level_feature`` exactly."""

    NAME = "feature_relative_threshold_cross"
    OPERATOR: ClassVar[str] = "feature_relative_threshold_cross"

    @staticmethod
    def spec(
        feature: Ref,
        level_feature: Ref,
        multiplier: Decimal,
        direction: Direction,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
        bar_spec: Ref | None = None,
    ) -> EventSpec:
        if feature.kind is not Kind.FEATURE or level_feature.kind is not Kind.FEATURE:
            raise ValueError("feature and level_feature must be feature references")
        if feature == level_feature:
            raise ValueError("feature and level_feature must be distinct")
        if not isinstance(multiplier, Decimal) or not multiplier.is_finite() or multiplier <= 0:
            raise ValueError("multiplier must be a positive finite Decimal")
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        return EventSpec(
            name=name or f"{feature.name}_relative_{level_feature.name}_{direction}",
            version=version,
            trigger=trigger_of(
                FeatureRelativeThresholdCrossProvider.OPERATOR,
                {
                    "feature": str(feature),
                    "level_feature": str(level_feature),
                    "multiplier": str(multiplier),
                    "direction": direction,
                },
            ),
            features=(feature, level_feature),
            observable_lag=observable_lag,
            bar_spec=bar_spec,
        )

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        direction = params["direction"]
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        return self.spec(
            parse_ref(params["feature"], Kind.FEATURE),
            parse_ref(params["level_feature"], Kind.FEATURE),
            parse_decimal(params["multiplier"], "multiplier"),
            direction,
            name=spec.name,
            version=spec.version,
            observable_lag=spec.observable_lag,
            bar_spec=spec.bar_spec,
        )

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        if upstream:
            raise EventInputError(f"{spec.ref} takes no upstream events")
        feature = parse_ref(params["feature"], Kind.FEATURE)
        level_feature = parse_ref(params["level_feature"], Kind.FEATURE)
        multiplier = parse_decimal(params["multiplier"], "multiplier")
        direction: str = params["direction"]
        line = series(points, feature)
        levels = {item.evaluation_time: item for item in series(points, level_feature)}
        stray = {str(item.source) for item in points} - {str(feature), str(level_feature)}
        if stray:
            raise EventInputError(f"inputs outside the spec's features: {sorted(stray)}")

        shared_times = sorted({item.evaluation_time for item in line} & levels.keys())
        by_time = {item.evaluation_time: item for item in line}
        paired = [(by_time[at], levels[at]) for at in shared_times]
        out: list[Event] = []
        for (previous, previous_level), (current, current_level) in zip(
            paired, paired[1:], strict=False
        ):
            before_value, value = numeric(previous), numeric(current)
            before_level, level = numeric(previous_level), numeric(current_level)
            if any(item is None for item in (before_value, value, before_level, level)):
                continue
            assert before_value is not None and value is not None
            assert before_level is not None and level is not None
            try:
                with localcontext(_EXACT):
                    before_threshold = multiplier * before_level
                    threshold = multiplier * level
                    crossed_up = abs(before_value) <= before_threshold and abs(value) > threshold
                    crossed_down = abs(before_value) >= before_threshold and abs(value) < threshold
            except DecimalException:
                raise EventInputError("the relative-threshold comparison is not exact") from None
            crossed = (
                "up"
                if direction in ("up", "both") and crossed_up
                else "down"
                if direction in ("down", "both") and crossed_down
                else None
            )
            if crossed is None:
                continue
            latest = max(current.available_time, current_level.available_time)
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=latest + spec.observable_lag,
                    attributes={
                        "direction": crossed,
                        "feature_value": value,
                        "level_value": level,
                        "multiplier": multiplier,
                        "threshold": threshold,
                    },
                    inputs=(previous, previous_level, current, current_level),
                )
            )
        return out


class VolatilityBreakoutProvider(EventProviderBase):
    """A (volatility) feature enters ``value > multiplier * mean(previous window values)``."""

    NAME = "volatility_breakout"
    OPERATOR: ClassVar[str] = "volatility_breakout"

    @staticmethod
    def spec(
        feature: Ref,
        window: int,
        multiplier: Decimal,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
        bar_spec: Ref | None = None,
    ) -> EventSpec:
        if feature.kind is not Kind.FEATURE:
            raise ValueError(f"{feature} is not a feature reference")
        _positive_int(window, "window")
        if not isinstance(multiplier, Decimal) or not multiplier.is_finite() or multiplier <= 0:
            raise ValueError("multiplier must be a positive finite Decimal")
        return EventSpec(
            name=name or f"{feature.name}_breakout_{window}",
            version=version,
            trigger=trigger_of(
                VolatilityBreakoutProvider.OPERATOR,
                {"feature": str(feature), "window": window, "multiplier": str(multiplier)},
            ),
            features=(feature,),
            observable_lag=observable_lag,
            bar_spec=bar_spec,
        )

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        return self.spec(
            parse_ref(params["feature"], Kind.FEATURE),
            _positive_int(params["window"], "window"),
            parse_decimal(params["multiplier"], "multiplier"),
            name=spec.name,
            version=spec.version,
            observable_lag=spec.observable_lag,
            bar_spec=spec.bar_spec,
        )

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        if upstream:
            raise EventInputError(f"{spec.ref} takes no upstream events")
        feature = parse_ref(params["feature"], Kind.FEATURE)
        window = _positive_int(params["window"], "window")
        multiplier = parse_decimal(params["multiplier"], "multiplier")
        line = series(points, feature)
        values = [numeric(item) for item in line]
        out: list[Event] = []
        for index in range(window + 1, len(line)):
            used = values[index - window - 1 : index + 1]
            if any(value is None for value in used):
                continue
            numbers = [value for value in used if value is not None]
            try:
                with localcontext(_EXACT):
                    now_sum = sum(numbers[1:-1], Decimal(0))
                    before_sum = sum(numbers[:-2], Decimal(0))
                    above_now = numbers[-1] * window > multiplier * now_sum
                    above_before = numbers[-2] * window > multiplier * before_sum
            except DecimalException:
                raise EventInputError("the breakout comparison is not exact") from None
            if not above_now or above_before:
                continue
            with localcontext(_BASELINE):
                baseline = (now_sum / window).quantize(_BASELINE_QUANTUM)
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=line[index].available_time + spec.observable_lag,
                    attributes={
                        "value": numbers[-1],
                        "baseline": baseline,
                        "multiplier": multiplier,
                        "window": window,
                    },
                    inputs=line[index - window - 1 : index + 1],
                )
            )
        return out
