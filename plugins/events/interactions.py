"""Interaction operators as EventProviders (Phase 3; ADR-0036 §5).

Their inputs are the events of two upstream event definitions (``EventRequest.upstream_events``);
every output event cites the pair it links in ``upstream_event_ids``, so it traces back to the
upstream events and, through them, to their feature / state inputs.

- ``EventSequenceProvider`` (``event_sequence``): "A then B within ``window``" — for each B event,
  the latest A event with ``A.event_time < B.event_time <= A.event_time + window``;
- ``EventCoOccurrenceProvider`` (``event_co_occurrence``): "A and B within ``window`` of each
  other" — for each event of either side, the latest event of the other side at or before it and
  at most ``window`` earlier (a tie is one co-occurrence).

``event_time`` is the later event's ``event_time`` plus ``observable_lag``: the pair is observable
only once both are. Every earlier partner is visible whenever the later event is, so the table as
of any time never changes retroactively.

``EventSpec`` can only depend on features and states (ADR-0012), so an interaction spec declares
the union of the upstream specs' features / states as its (transitive) inputs, records the upstream
event refs in ``lineage``, and binds the upstream refs, their spec hashes and the window in its
trigger. Upstream events of any other definition or spec hash are refused (fail closed).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any, ClassVar

from core.contracts.event import Event, EventInputError, EventInputPoint
from core.domain.base import Kind, Ref
from core.domain.specs import EventSpec
from plugins.events._base import EventProviderBase, parse_ref, trigger_of

__all__ = ["EventCoOccurrenceProvider", "EventSequenceProvider"]

_MICROSECOND = timedelta(microseconds=1)


def _window(value: object) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"window_us must be a positive int, got {value!r}")
    return value * _MICROSECOND


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a spec content hash")
    return value


def _union(*specs: EventSpec) -> tuple[tuple[Ref, ...], tuple[Ref, ...]]:
    features = {str(ref): ref for spec in specs for ref in spec.features}
    states = {str(ref): ref for spec in specs for ref in spec.states}
    return (
        tuple(features[key] for key in sorted(features)),
        tuple(states[key] for key in sorted(states)),
    )


def _gap_seconds(gap: timedelta) -> Decimal:
    return Decimal(gap // _MICROSECOND).scaleb(-6)


def _split(
    spec: EventSpec, upstream: Sequence[Event], sides: dict[str, tuple[Ref, str]]
) -> dict[str, list[Event]]:
    """Upstream events by side; any event of another definition / hash is refused."""
    out: dict[str, list[Event]] = {side: [] for side in sides}
    for item in upstream:
        for side, (ref, spec_hash) in sides.items():
            if item.event == ref and item.spec_hash == spec_hash:
                out[side].append(item)
                break
        else:
            raise EventInputError(f"{spec.ref} does not take upstream events of {item.event}")
    return out


class _PairProvider(EventProviderBase):
    """Two upstream event definitions + a window."""

    FIRST: ClassVar[str]
    SECOND: ClassVar[str]

    @classmethod
    def _build(
        cls,
        first: EventSpec,
        second: EventSpec,
        window: timedelta,
        *,
        name: str,
        version: str,
        observable_lag: timedelta,
    ) -> EventSpec:
        if first.ref == second.ref:
            raise ValueError("the two upstream event definitions must differ")
        if window <= timedelta(0) or window % _MICROSECOND:
            raise ValueError("window must be a positive whole number of microseconds")
        features, states = _union(first, second)
        return EventSpec(
            name=name,
            version=version,
            trigger=trigger_of(
                cls.OPERATOR,
                {
                    cls.FIRST: str(first.ref),
                    f"{cls.FIRST}_hash": first.content_hash(),
                    cls.SECOND: str(second.ref),
                    f"{cls.SECOND}_hash": second.content_hash(),
                    "window_us": window // _MICROSECOND,
                },
            ),
            features=features,
            states=states,
            observable_lag=observable_lag,
            lineage=(first.ref, second.ref),
        )

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        sides = self._sides(params)
        refs = tuple(ref for ref, _ in sides.values())
        if spec.lineage != refs:
            raise ValueError("lineage must be the two upstream event refs, in trigger order")
        _window(params["window_us"])
        # The declared inputs are the upstream specs' (not visible here): only their form is fixed.
        for refs_of in (spec.features, spec.states):
            keys = [str(ref) for ref in refs_of]
            if keys != sorted(set(keys)):
                raise ValueError("features / states must be sorted and unique")
        rebuilt_trigger = trigger_of(self.OPERATOR, params)
        if rebuilt_trigger != spec.trigger:
            raise ValueError("the trigger is not in canonical form")
        return spec

    def _sides(self, params: dict[str, Any]) -> dict[str, tuple[Ref, str]]:
        return {
            side: (
                parse_ref(params[side], Kind.EVENT),
                _hash(params[f"{side}_hash"], f"{side}_hash"),
            )
            for side in (self.FIRST, self.SECOND)
        }

    def _event(self, spec: EventSpec, earlier: Event, later: Event, gap: timedelta) -> Event:
        return Event.build(
            event=spec.ref,
            spec_hash=spec.content_hash(),
            event_time=later.event_time + spec.observable_lag,
            attributes={"gap_seconds": _gap_seconds(gap)},
            upstream=(earlier, later),
        )


def _latest(candidates: Sequence[Event]) -> Event | None:
    return max(candidates, key=lambda item: (item.event_time, item.event_id), default=None)


class EventSequenceProvider(_PairProvider):
    """``first`` then ``then`` within ``window`` (strictly later, at most ``window`` apart)."""

    NAME = "event_sequence"
    OPERATOR: ClassVar[str] = "event_sequence"
    FIRST: ClassVar[str] = "first"
    SECOND: ClassVar[str] = "then"

    @classmethod
    def spec(
        cls,
        first: EventSpec,
        then: EventSpec,
        window: timedelta,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
    ) -> EventSpec:
        return cls._build(
            first,
            then,
            window,
            name=name or f"{first.name}_then_{then.name}",
            version=version,
            observable_lag=observable_lag,
        )

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        if points:
            raise EventInputError(f"{spec.ref} takes only upstream events")
        window = _window(params["window_us"])
        split = _split(spec, upstream, self._sides(params))
        firsts = split[self.FIRST]
        out: list[Event] = []
        for then in split[self.SECOND]:
            earlier = _latest(
                [
                    item
                    for item in firsts
                    if item.event_time < then.event_time <= item.event_time + window
                ]
            )
            if earlier is not None:
                out.append(self._event(spec, earlier, then, then.event_time - earlier.event_time))
        return out


class EventCoOccurrenceProvider(_PairProvider):
    """``left`` and ``right`` at most ``window`` apart, in either order."""

    NAME = "event_co_occurrence"
    OPERATOR: ClassVar[str] = "event_co_occurrence"
    FIRST: ClassVar[str] = "left"
    SECOND: ClassVar[str] = "right"

    @classmethod
    def spec(
        cls,
        left: EventSpec,
        right: EventSpec,
        window: timedelta,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
    ) -> EventSpec:
        return cls._build(
            left,
            right,
            window,
            name=name or f"{left.name}_with_{right.name}",
            version=version,
            observable_lag=observable_lag,
        )

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        if points:
            raise EventInputError(f"{spec.ref} takes only upstream events")
        window = _window(params["window_us"])
        split = _split(spec, upstream, self._sides(params))
        out: list[Event] = []
        for mine, other in ((self.FIRST, self.SECOND), (self.SECOND, self.FIRST)):
            for later in split[mine]:
                earlier = _latest(
                    [
                        item
                        for item in split[other]
                        if item.event_time <= later.event_time <= item.event_time + window
                    ]
                )
                if earlier is not None:
                    gap = later.event_time - earlier.event_time
                    out.append(self._event(spec, earlier, later, gap))
        return out
