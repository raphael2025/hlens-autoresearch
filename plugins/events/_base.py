"""Shared plumbing of the first EventProviders (Phase 3; ADR-0036).

An ``EventSpec`` has no ``params`` field, so every definition parameter lives in its ``trigger``:
the canonical JSON (``core.domain.base.canonical_json``) of ``{"operator": ..., <params>}``. The
trigger is part of the spec content, so the spec hash in the descriptor binds every parameter; a
provider only serves specs that it would build itself from their own trigger (a spec whose trigger,
inputs or lineage differ from that canonical form is refused at construction).

At ``detect`` a provider reads only the visible set ``request.visible_at(request.as_of, lag)`` and
returns every event it derives from it (the table as of ``as_of``); it never looks at anything the
runner would not hand it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from core.contracts.event import (
    Event,
    EventInputError,
    EventInputPoint,
    EventProviderDescriptor,
    EventRequest,
    EventResult,
    UnsupportedEvent,
)
from core.domain.base import FrozenMapping, Kind, Ref, canonical_json
from core.domain.specs import EventSpec

__all__ = [
    "EventProviderBase",
    "numeric",
    "parse_decimal",
    "parse_ref",
    "series",
    "trigger_of",
]


def trigger_of(operator: str, params: Mapping[str, Any]) -> str:
    """The canonical trigger text of an operator and its parameters."""
    return canonical_json({"operator": operator, **params})


def parse_ref(value: object, kind: Kind) -> Ref:
    if not isinstance(value, str):
        raise ValueError(f"expected a {kind.value} reference, got {value!r}")
    ref = Ref.parse(value)
    if ref.kind is not kind:
        raise ValueError(f"expected a {kind.value} reference, got {ref}")
    return ref


def parse_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string, got {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} must be a decimal string, got {value!r}") from None
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def series(points: Iterable[EventInputPoint], source: Ref) -> list[EventInputPoint]:
    """The visible points of one source, in ``evaluation_time`` order."""
    return sorted(
        (item for item in points if item.source == source), key=lambda item: item.evaluation_time
    )


def numeric(item: EventInputPoint) -> Decimal | None:
    """The point's value as a number; ``None`` stays ``None`` (not computable)."""
    value = item.value
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise EventInputError(f"{item.source} @ {item.evaluation_time.isoformat()} is not numeric")
    return Decimal(value)


class EventProviderBase:
    """Declared specs, descriptor, visible-set ``detect``."""

    NAME: ClassVar[str]
    VERSION: ClassVar[str] = "1.0.0"
    OPERATOR: ClassVar[str]

    def __init__(self, specs: Iterable[EventSpec]) -> None:
        by_ref: dict[str, EventSpec] = {}
        for spec in specs:
            if not isinstance(spec, EventSpec):
                raise TypeError("specs must be EventSpec instances")
            try:
                canonical = self.canonical(spec)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec: {exc}") from None
            if canonical.content_hash() != spec.content_hash():
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec (trigger or inputs)")
            if str(spec.ref) in by_ref:
                raise ValueError(f"{spec.ref} is declared twice")
            by_ref[str(spec.ref)] = spec
        if not by_ref:
            raise ValueError("a provider must serve at least one spec")
        self._specs = by_ref
        self._descriptor = EventProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_events=FrozenMapping(
                {key: spec.content_hash() for key, spec in by_ref.items()}
            ),
        )

    @property
    def descriptor(self) -> EventProviderDescriptor:
        return self._descriptor

    def detect(self, request: EventRequest) -> EventResult:
        spec = self._specs.get(str(request.event))
        if spec is None or spec.content_hash() != request.spec_hash:
            raise UnsupportedEvent(f"{self.NAME} does not serve {request.event} with this hash")
        points, upstream = request.visible_at(request.as_of, spec.observable_lag)
        params = self.params(spec)
        events = self.events(spec, params, points, upstream)
        return EventResult.build(request, self._descriptor, events)

    @classmethod
    def params(cls, spec: EventSpec) -> dict[str, Any]:
        """The trigger's parameters (without ``operator``)."""
        raw = json.loads(spec.trigger)
        if not isinstance(raw, dict) or raw.get("operator") != cls.OPERATOR:
            raise ValueError(f"the trigger is not a {cls.OPERATOR} trigger")
        return {key: value for key, value in raw.items() if key != "operator"}

    # ------------------------------------------------------------------ per provider

    def canonical(self, spec: EventSpec) -> EventSpec:
        """The spec this provider would build from ``spec``'s own trigger."""
        raise NotImplementedError

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        raise NotImplementedError
