"""EventProvider over one state series (Phase 3; ADR-0036).

``StateSwitchProvider`` (``state_switch``): the state label changes between two consecutive
computable points of the series (``evaluation_time`` order). Optional ``from_state`` / ``to_state``
restrict the switches that count; ``None`` means any. A ``None`` (not computable) point breaks the
pair, so a switch across an unknown stretch is not reported.

The input is the state series in the local Phase 3 shape (``infrastructure.event.inputs.
inputs_from_state_series``); labels are non-numeric text. ``event_time`` is the ``available_time``
of the new state's point plus ``observable_lag``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, ClassVar

from core.contracts.event import Event, EventInputError, EventInputPoint
from core.domain.base import Kind, Ref
from core.domain.specs import EventSpec
from plugins.events._base import EventProviderBase, parse_ref, series, trigger_of

__all__ = ["StateSwitchProvider"]


def _label(item: EventInputPoint) -> str | None:
    value = item.value
    if value is None:
        return None
    if not isinstance(value, str):
        raise EventInputError(f"{item.source} @ {item.evaluation_time.isoformat()} is not a label")
    return value


def _optional_label(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty state label or None")
    return value


class StateSwitchProvider(EventProviderBase):
    """The state of a series switches (optionally from / to given states)."""

    NAME = "state_switch"
    OPERATOR: ClassVar[str] = "state_switch"

    @staticmethod
    def spec(
        state: Ref,
        *,
        from_state: str | None = None,
        to_state: str | None = None,
        name: str | None = None,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
    ) -> EventSpec:
        if state.kind is not Kind.STATE:
            raise ValueError(f"{state} is not a state reference")
        return EventSpec(
            name=name or f"{state.name}_switch",
            version=version,
            trigger=trigger_of(
                StateSwitchProvider.OPERATOR,
                {
                    "state": str(state),
                    "from_state": _optional_label(from_state, "from_state"),
                    "to_state": _optional_label(to_state, "to_state"),
                },
            ),
            states=(state,),
            observable_lag=observable_lag,
        )

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        return self.spec(
            parse_ref(params["state"], Kind.STATE),
            from_state=_optional_label(params["from_state"], "from_state"),
            to_state=_optional_label(params["to_state"], "to_state"),
            name=spec.name,
            version=spec.version,
            observable_lag=spec.observable_lag,
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
        state = parse_ref(params["state"], Kind.STATE)
        wanted_from: str | None = params["from_state"]
        wanted_to: str | None = params["to_state"]
        line = series(points, state)
        out: list[Event] = []
        for previous, current in zip(line, line[1:], strict=False):
            before, after = _label(previous), _label(current)
            if before is None or after is None or before == after:
                continue
            if wanted_from is not None and before != wanted_from:
                continue
            if wanted_to is not None and after != wanted_to:
                continue
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=current.available_time + spec.observable_lag,
                    attributes={"from_state": before, "to_state": after},
                    inputs=(previous, current),
                )
            )
        return out
