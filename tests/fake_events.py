"""Shared Phase 3 event fixtures and deliberately faulty EventProviders (ADR-0036).

Series (one point per minute from ``T0``; ``available_time = evaluation_time`` except that the
points of minutes 5 and 6 both arrive at minute 7 — a burst, still append-only):

- feature ``x``: 1, 2, 4, 6, 3, 5, 7, 2, None, 8, 9, 1
- state ``regime``: calm, calm, wild, wild, None, wild, calm, calm, wild, wild, calm, calm

The faulty providers each break exactly one rule, so the suite / runner must name that rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.contracts.event import (
    Event,
    EventInputPoint,
    EventRequest,
    EventResult,
)
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import EventSpec
from infrastructure.event.inputs import StateSeriesPoint, inputs_from_state_series
from plugins.events import FeatureThresholdCrossProvider
from plugins.events._base import numeric, series

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LAG = timedelta(minutes=1)
X = Ref(kind=Kind.FEATURE, name="x", version="1.0.0")
REGIME = Ref(kind=Kind.STATE, name="regime", version="1.0.0")
X_RESULT = content_hash({"feature-run": "x"})

X_VALUES: tuple[int | None, ...] = (1, 2, 4, 6, 3, 5, 7, 2, None, 8, 9, 1)
REGIME_LABELS: tuple[str | None, ...] = (
    "calm",
    "calm",
    "wild",
    "wild",
    None,
    "wild",
    "calm",
    "calm",
    "wild",
    "wild",
    "calm",
    "calm",
)
#: Minutes 5 and 6 arrive together at minute 7.
_ARRIVAL = {5: 7, 6: 7}


def _available(minute: int) -> datetime:
    return T0 + _ARRIVAL.get(minute, minute) * MINUTE


def x_point(minute: int, value: int | None) -> EventInputPoint:
    return EventInputPoint(
        source=X,
        source_lineage_hash=X_RESULT,
        evaluation_time=T0 + minute * MINUTE,
        available_time=_available(minute),
        value=None if value is None else Decimal(value),
    )


X_INPUTS: tuple[EventInputPoint, ...] = tuple(
    x_point(minute, value) for minute, value in enumerate(X_VALUES)
)
REGIME_INPUTS: tuple[EventInputPoint, ...] = inputs_from_state_series(
    REGIME,
    (
        StateSeriesPoint(
            evaluation_time=T0 + minute * MINUTE,
            available_time=_available(minute),
            label=label,
            lineage_hash=content_hash({"state-run": "regime", "minute": minute}),
        )
        for minute, label in enumerate(REGIME_LABELS)
    ),
)
#: Every 30 s from T0 to T0 + 16 min.
AS_OF_TIMES: tuple[datetime, ...] = tuple(T0 + k * timedelta(seconds=30) for k in range(33))
END = AS_OF_TIMES[-1]


def perturb_number(item: EventInputPoint) -> EventInputPoint:
    value = item.value
    if isinstance(value, Decimal):
        return item.model_copy(update={"value": value * 2 + 1})
    return item


def perturb_label(item: EventInputPoint) -> EventInputPoint:
    flipped = {"calm": "wild", "wild": "calm"}
    if isinstance(item.value, str):
        return item.model_copy(update={"value": flipped[item.value]})
    return item


def request(spec: EventSpec, *, as_of: datetime = END, **fields: Any) -> EventRequest:
    return EventRequest(event=spec.ref, spec_hash=spec.content_hash(), as_of=as_of, **fields)


# ======================================================================================
# Faulty providers
# ======================================================================================


class ConfirmedTopProvider(FeatureThresholdCrossProvider):
    """FAULT (future confirmation): a local top ``x[i-1] < x[i] > x[i+1]``, dated at ``x[i]``.

    It reads the whole request (not the visible set) and cites only ``x[i-1], x[i]``, so every
    single answer looks well-formed; the top is known only once ``x[i+1]`` arrives.
    """

    NAME = "confirmed_top"

    def detect(self, request: EventRequest) -> EventResult:
        spec = self._specs[str(request.event)]
        line = series(request.inputs, X)
        out: list[Event] = []
        for before, top, after in zip(line, line[1:], line[2:], strict=False):
            a, b, c = numeric(before), numeric(top), numeric(after)
            if a is None or b is None or c is None or not a < b > c:
                continue
            at = top.available_time + spec.observable_lag
            if at > request.as_of:
                continue
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=at,
                    attributes={"top": b},
                    inputs=(before, top),
                )
            )
        return EventResult.build(request, self._descriptor, out)


class BackdatedProvider(FeatureThresholdCrossProvider):
    """FAULT (event time is not the observable time): every event is dated one minute early."""

    NAME = "backdated"

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        honest = super().events(spec, params, points, upstream)
        return [
            Event.build(
                event=item.event,
                spec_hash=item.spec_hash,
                event_time=item.event_time - MINUTE,
                attributes=dict(item.attributes),
                inputs=[point for point in points if point.point_id in item.input_ids],
            )
            for item in honest
        ]
