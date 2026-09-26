"""Window interaction operators for the interaction DSL (Phase 3; ADR-0061).

Like ``plugins.events.interactions`` their inputs are upstream events only, every output cites the
upstream events it links (``upstream_event_ids``), the spec's ``lineage`` is exactly the upstream
event refs and its trigger binds every upstream ref to its spec hash with the ``<name>`` /
``<name>_hash`` pair (ADR-0036 §4-§5), so ``infrastructure.event.upstream`` verifies every hop.

- ``EventWindowEndProvider`` (``event_window_end``): one event at the **end** of the window that
  opens at each upstream event: ``event_time = A.event_time + window``. The spec's
  ``observable_lag`` *is* the window (checked), so the runner only shows an ``A`` event once its
  window has ended and the event time is the observable time of its only input (ADR-0036 §2).
- ``EventAbsenceProvider`` (``event_absence``): for each ``anchor`` event, "no ``absent`` event in
  ``[anchor.event_time - window, anchor.event_time]``" (both ends inclusive). It cites the anchor
  only; every ``absent`` event that could refute it is visible whenever the anchor is (it is not
  later), so the table as of any time never changes retroactively.
- ``EventCountProvider`` (``event_count``): at each upstream event ``e``, "at least ``at_least``
  upstream events in ``[e.event_time - window, e.event_time]``" (inclusive); it cites every event
  in that window and records the ``count``.

The DSL operator ``not(a, b, within)`` ("A occurs and no B within ``within``") is the two-hop chain
``event_absence(anchor=event_window_end(a, within), absent=b, window=within)``: its event time is
the end of A's window (ADR-0061 §4) — before then nobody can know that B did not happen — and at
that time every B of the window ``[A, A + within]`` is visible to the runner. A single spec cannot
do this: the runner truncates every upstream event with the spec's one ``observable_lag``
(``EventRequest.visible_at``), so a spec that dated A's event at the window end (lag = window)
would not see the Bs inside the window when it answers (ADR-0061 implementation note).

Deterministic, exact; only ``core`` (and the shared interaction helpers) is imported.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, ClassVar

from core.contracts.event import Event, EventInputError, EventInputPoint
from core.domain.base import Kind, Ref
from core.domain.specs import EventSpec
from plugins.events._base import EventProviderBase, parse_ref, trigger_of
from plugins.events.interactions import _MICROSECOND, _gap_seconds, _hash, _split, _union, _window

__all__ = ["EventAbsenceProvider", "EventCountProvider", "EventWindowEndProvider"]


def _whole_window(window: timedelta) -> int:
    if window <= timedelta(0) or window % _MICROSECOND:
        raise ValueError("window must be a positive whole number of microseconds")
    return window // _MICROSECOND


def _at_least(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"at_least must be a positive int, got {value!r}")
    return value


class _LinkedProvider(EventProviderBase):
    """Upstream event definitions named by ``SIDES``, each bound by ``<side>`` / ``<side>_hash``."""

    SIDES: ClassVar[tuple[str, ...]]

    @classmethod
    def _make(
        cls,
        upstream: Sequence[EventSpec],
        extra: dict[str, Any],
        *,
        name: str,
        version: str,
        observable_lag: timedelta,
    ) -> EventSpec:
        refs = [str(item.ref) for item in upstream]
        if len(set(refs)) != len(refs):
            raise ValueError("the upstream event definitions must differ")
        features, states = _union(*upstream)
        params: dict[str, Any] = dict(extra)
        for side, item in zip(cls.SIDES, upstream, strict=True):
            params[side] = str(item.ref)
            params[f"{side}_hash"] = item.content_hash()
        return EventSpec(
            name=name,
            version=version,
            trigger=trigger_of(cls.OPERATOR, params),
            features=features,
            states=states,
            observable_lag=observable_lag,
            lineage=tuple(item.ref for item in upstream),
        )

    def _sides(self, params: dict[str, Any]) -> dict[str, tuple[Ref, str]]:
        return {
            side: (
                parse_ref(params[side], Kind.EVENT),
                _hash(params[f"{side}_hash"], f"{side}_hash"),
            )
            for side in self.SIDES
        }

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        expected = {"window_us", *self.SIDES, *(f"{side}_hash" for side in self.SIDES)}
        expected |= self._extra_fields()
        if set(params) != expected:
            raise ValueError(f"the trigger fields must be exactly {sorted(expected)}")
        sides = self._sides(params)
        refs = tuple(ref for ref, _ in sides.values())
        if spec.lineage != refs:
            raise ValueError("lineage must be the upstream event refs, in trigger order")
        if len(set(map(str, refs))) != len(refs):
            raise ValueError("the upstream event definitions must differ")
        _window(params["window_us"])
        self._check(spec, params)
        # The declared inputs are the upstream specs' union (not visible here): only their form is
        # fixed; ``infrastructure.event.run_events`` checks them against the supplied upstream.
        for refs_of in (spec.features, spec.states):
            keys = [str(ref) for ref in refs_of]
            if keys != sorted(set(keys)):
                raise ValueError("features / states must be sorted and unique")
        if trigger_of(self.OPERATOR, params) != spec.trigger:
            raise ValueError("the trigger is not in canonical form")
        return spec

    def _extra_fields(self) -> set[str]:
        return set()

    def _check(self, spec: EventSpec, params: dict[str, Any]) -> None:
        """Operator-specific parameter checks."""


class EventWindowEndProvider(_LinkedProvider):
    """One event at ``A.event_time + window`` for each ``of`` event (lag = the window)."""

    NAME = "event_window_end"
    OPERATOR: ClassVar[str] = "event_window_end"
    SIDES: ClassVar[tuple[str, ...]] = ("of",)

    @classmethod
    def spec(
        cls, of: EventSpec, window: timedelta, *, name: str, version: str = "1.0.0"
    ) -> EventSpec:
        return cls._make(
            (of,),
            {"window_us": _whole_window(window)},
            name=name,
            version=version,
            observable_lag=window,
        )

    def _check(self, spec: EventSpec, params: dict[str, Any]) -> None:
        if spec.observable_lag != _window(params["window_us"]):
            raise ValueError("observable_lag must be exactly the window (the event is its end)")

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
        return [
            Event.build(
                event=spec.ref,
                spec_hash=spec.content_hash(),
                event_time=item.event_time + spec.observable_lag,
                attributes={"window_seconds": _gap_seconds(window)},
                upstream=(item,),
            )
            for item in split["of"]
        ]


class EventAbsenceProvider(_LinkedProvider):
    """``anchor`` with no ``absent`` event in ``[anchor - window, anchor]`` (inclusive)."""

    NAME = "event_absence"
    OPERATOR: ClassVar[str] = "event_absence"
    SIDES: ClassVar[tuple[str, ...]] = ("anchor", "absent")

    @classmethod
    def spec(
        cls,
        anchor: EventSpec,
        absent: EventSpec,
        window: timedelta,
        *,
        name: str,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
    ) -> EventSpec:
        return cls._make(
            (anchor, absent),
            {"window_us": _whole_window(window)},
            name=name,
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
        absent_times = [item.event_time for item in split["absent"]]
        out: list[Event] = []
        for anchor in split["anchor"]:
            start, end = anchor.event_time - window, anchor.event_time
            if any(start <= at <= end for at in absent_times):
                continue
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=anchor.event_time + spec.observable_lag,
                    attributes={"window_seconds": _gap_seconds(window)},
                    upstream=(anchor,),
                )
            )
        return out


class EventCountProvider(_LinkedProvider):
    """At each ``of`` event: at least ``at_least`` ``of`` events in ``[e - window, e]``."""

    NAME = "event_count"
    OPERATOR: ClassVar[str] = "event_count"
    SIDES: ClassVar[tuple[str, ...]] = ("of",)

    @classmethod
    def spec(
        cls,
        of: EventSpec,
        at_least: int,
        window: timedelta,
        *,
        name: str,
        version: str = "1.0.0",
        observable_lag: timedelta = timedelta(0),
    ) -> EventSpec:
        return cls._make(
            (of,),
            {"at_least": _at_least(at_least), "window_us": _whole_window(window)},
            name=name,
            version=version,
            observable_lag=observable_lag,
        )

    def _extra_fields(self) -> set[str]:
        return {"at_least"}

    def _check(self, spec: EventSpec, params: dict[str, Any]) -> None:
        _at_least(params["at_least"])

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
        at_least = _at_least(params["at_least"])
        events = _split(spec, upstream, self._sides(params))["of"]
        out: list[Event] = []
        for item in events:
            linked = [
                other
                for other in events
                if item.event_time - window <= other.event_time <= item.event_time
            ]
            if len(linked) < at_least:
                continue
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    event_time=item.event_time + spec.observable_lag,
                    attributes={"count": len(linked)},
                    upstream=linked,
                )
            )
        return out
