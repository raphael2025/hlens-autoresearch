"""P7 ``temporal`` operator execution Provider (ADR-0082 3rd acceptance, ADR-0088, ADR-0100 #1).

``P7TemporalSequenceProvider`` (``p7_temporal_sequence@1.0.0``) serves exactly the ``EventSpec``
values the pure P7 lowering emits for definition ``p7.temporal.sequence_within_bars@1.0.0``: the
second event occurs 1..``window_bars`` bars after the first, i.e. in the left-open, right-closed
interval ``(first, first + window_bars * bar_duration]``.

- **The window is measured on occurrence times** for a hash-bound (plan format "1.3.0") spec
  (ADR-0100 revision 1 §2): an upstream ``event_time`` is its observable time (occurrence +
  ``observable_lag``), so each side's occurrence time is ``event_time - upstream.observable_lag``
  (its own spec's lag), and the second's occurrence must fall in
  ``(first_occurrence, first_occurrence + window_bars * bar_duration]``; ``gap_seconds`` is the
  occurrence gap. A spec of an older plan format keeps comparing the upstream ``event_time``
  values directly (its meaning is unchanged).

- **Bar duration is explicit.** ``bar_spec`` is a bare ``representation`` Ref; its duration is not
  readable from the contract. The caller supplies ``bar_durations`` (``str(bar_spec)`` ->
  positive ``timedelta``); a spec whose ``bar_spec`` has no entry is refused at construction. No
  duration is ever guessed from the representation's name.
- **Upstream specs are explicit.** The caller supplies the two input ``EventSpec`` values
  (``str(ref)`` -> spec). Their refs must be the spec's ``lineage`` / trigger refs and must name
  two different EventSpecs (ADR-0100 revision 1 §3, ``temporal_same_input``), both must
  declare the spec's ``bar_spec``, and the first's lag must not exceed the second's (the
  lowering's ``temporal_visibility_unprovable`` rule, re-checked). The spec's ``observable_lag``
  must be 0 for a hash-bound (plan format "1.3.0") spec (ADR-0100 revision 1 §1) and equal the
  second's otherwise (older formats, unchanged). Upstream events of any other definition or spec
  hash are refused.
- **Event time = observable time** (ADR-0036 §2, enforced by ``EventResult.check_answers``): each
  output event links the second event with the latest first event in its window, and its
  ``event_time`` is the second event's ``event_time`` plus the spec's ``observable_lag``. For a
  hash-bound spec that lag is 0, so the output's ``event_time`` is exactly the second event's
  observable time (its ``event_time``); an older-format spec keeps adding the second's lag. A first
  event is always visible no later than the second, so the table as of any time never changes
  retroactively. No partner -> no event (``missing = no_event``).
- ADR-0061's microsecond ``seq`` window is not reused; ``time_unit`` is bars of ``bar_spec`` only.

**Upstream hash binding** (ADR-0100 revision 1 §4). A spec lowered from a plan of format "1.3.0"
carries ``first_event_hash`` / ``second_event_hash`` in its trigger (both or neither); each must
equal the supplied upstream spec's ``content_hash()``, otherwise the spec is refused. That is the
``<name>`` / ``<name>_hash`` pair ``infrastructure.event.upstream.verify_interaction`` reads, so the
runner accepts these specs. A spec lowered from an older plan format binds only the upstream refs;
the runner refuses it (fail closed) and the Provider enforces the binding only against the
caller's upstream table (which the P7 compiler builds from the hash-verified direct-reference
resolution).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any, ClassVar, Final

from core.contracts.event import Event, EventInputError, EventInputPoint
from core.domain.base import Kind
from core.domain.specs import EventSpec
from plugins.events._base import EventProviderBase, parse_ref, trigger_of

__all__ = ["P7TemporalSequenceProvider"]

_MICROSECOND: Final = timedelta(microseconds=1)
_DEFINITION: Final = "p7.temporal.sequence_within_bars@1.0.0"
#: The trigger fields with a fixed value in every lowered temporal spec (besides ``operator``).
_FIXED: Final[dict[str, str]] = {
    "definition": _DEFINITION,
    "provider": "p7_temporal_sequence@1.0.0",
    "semantic_version": "1.0.0",
    "interval": "left_open_right_closed",
    "event_time": "second_event_time",
    "visibility": "second_event_observable_time",
    "missing": "no_event",
}
_VARIABLE: Final = frozenset({"first_event", "second_event", "bar_spec", "window_bars"})
#: Optional upstream hash binding (plan format "1.3.0", ADR-0100 revision 1 §4): both or neither.
_HASH_BINDING: Final[dict[str, str]] = {
    "first_event_hash": "first_event",
    "second_event_hash": "second_event",
}


def _hash_bound(params: Mapping[str, Any]) -> bool:
    """Whether the trigger binds the upstream spec hashes (plan format "1.3.0" lowering)."""
    return all(field in params for field in _HASH_BINDING)


class P7TemporalSequenceProvider(EventProviderBase):
    """``second`` within 1..``window_bars`` bars after ``first`` (module docstring)."""

    NAME = "p7_temporal_sequence"
    OPERATOR: ClassVar[str] = "temporal_sequence"
    DEFINITION: ClassVar[str] = _DEFINITION

    def __init__(
        self,
        specs: Iterable[EventSpec],
        *,
        upstream: Mapping[str, EventSpec],
        bar_durations: Mapping[str, timedelta],
    ) -> None:
        table: dict[str, EventSpec] = {}
        for key, spec in upstream.items():
            if not isinstance(spec, EventSpec) or key != str(spec.ref):
                raise ValueError(f"upstream entry {key!r} must be the EventSpec it names")
            table[key] = spec
        durations: dict[str, timedelta] = {}
        for key, duration in bar_durations.items():
            if not isinstance(duration, timedelta) or duration <= timedelta(0):
                raise ValueError(f"bar duration of {key!r} must be a positive timedelta")
            if duration % _MICROSECOND:
                raise ValueError(f"bar duration of {key!r} must be whole microseconds")
            durations[key] = duration
        # Set before the base constructor, which validates every spec through ``canonical``.
        self._upstream_specs = table
        self._bar_durations = durations
        super().__init__(specs)

    @classmethod
    def plugin_key(cls) -> str:
        return f"{cls.NAME}@{cls.VERSION}"

    def canonical(self, spec: EventSpec) -> EventSpec:
        params = self.params(spec)
        fixed = {key: params.get(key) for key in _FIXED}
        base = set(_FIXED) | _VARIABLE
        if fixed != _FIXED or set(params) not in (base, base | set(_HASH_BINDING)):
            raise ValueError("the trigger is not a lowered p7 temporal declaration")
        window = params["window_bars"]
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            raise ValueError("window_bars must be a positive int")
        first_ref = parse_ref(params["first_event"], Kind.EVENT)
        second_ref = parse_ref(params["second_event"], Kind.EVENT)
        if first_ref.target_identity() == second_ref.target_identity():
            raise ValueError(
                "temporal_same_input: first_event and second_event must be different EventSpecs"
            )
        bar_spec = parse_ref(params["bar_spec"], Kind.REPRESENTATION)
        if spec.bar_spec is None or str(spec.bar_spec) != str(bar_spec):
            raise ValueError("bar_spec must equal the trigger's bar_spec")
        if str(bar_spec) not in self._bar_durations:
            raise ValueError(f"no explicit bar duration was supplied for {bar_spec}")
        if tuple(spec.lineage) != (first_ref, second_ref):
            raise ValueError("lineage must be (first_event, second_event)")
        first = self._upstream_specs.get(str(first_ref))
        second = self._upstream_specs.get(str(second_ref))
        if first is None or second is None:
            raise ValueError("both upstream EventSpecs must be supplied")
        for upstream in (first, second):
            if upstream.bar_spec is None or str(upstream.bar_spec) != str(bar_spec):
                raise ValueError(f"{upstream.ref} does not declare bar_spec {bar_spec}")
        if _hash_bound(params):
            for field, upstream in (("first_event_hash", first), ("second_event_hash", second)):
                bound = params[field]
                if not isinstance(bound, str) or bound != upstream.content_hash():
                    raise ValueError(
                        f"{field} does not bind the supplied upstream spec {upstream.ref}"
                    )
        if _hash_bound(params):
            # ADR-0100 revision 1 §1: the upstream event_time already is the observable time.
            if spec.observable_lag != timedelta(0):
                raise ValueError("a hash-bound (plan format 1.3.0) spec has observable_lag 0")
        elif spec.observable_lag != second.observable_lag:
            raise ValueError("observable_lag must equal the second event's")
        if first.observable_lag > second.observable_lag:
            raise ValueError("temporal_visibility_unprovable: first lag exceeds the second's")
        if trigger_of(self.OPERATOR, params) != spec.trigger:
            raise ValueError("the trigger is not in canonical form")
        return spec

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        if points:
            raise EventInputError(f"{spec.ref} takes only upstream events")
        first_spec = self._upstream_specs[params["first_event"]]
        second_spec = self._upstream_specs[params["second_event"]]
        sides = {
            "first": (first_spec.ref, first_spec.content_hash()),
            "second": (second_spec.ref, second_spec.content_hash()),
        }
        split: dict[str, list[Event]] = {"first": [], "second": []}
        for item in upstream:
            for side, (ref, spec_hash) in sides.items():
                if item.event == ref and item.spec_hash == spec_hash:
                    split[side].append(item)
                    break
            else:
                raise EventInputError(f"{spec.ref} does not take upstream events of {item.event}")
        if spec.bar_spec is None:  # pragma: no cover - refused in ``canonical``
            raise EventInputError(f"{spec.ref} declares no bar_spec")
        span = int(params["window_bars"]) * self._bar_durations[str(spec.bar_spec)]
        if _hash_bound(params):
            # ADR-0100 revision 1 §2: occurrence time = event_time - the upstream's own lag.
            first_lag, second_lag = first_spec.observable_lag, second_spec.observable_lag
        else:  # older plan format: unchanged, compares the upstream event_time values
            first_lag = second_lag = timedelta(0)
        out: list[Event] = []
        for second in split["second"]:
            occurred = second.event_time - second_lag
            candidates = [
                item
                for item in split["first"]
                if item.event_time - first_lag < occurred <= item.event_time - first_lag + span
            ]
            if not candidates:
                continue
            first = max(candidates, key=lambda item: (item.event_time, item.event_id))
            gap = occurred - (first.event_time - first_lag)
            out.append(
                Event.build(
                    event=spec.ref,
                    spec_hash=spec.content_hash(),
                    # Hash-bound (1.3.0): lag 0, so exactly the second's observable time.
                    event_time=second.event_time + spec.observable_lag,
                    attributes={"gap_seconds": Decimal(gap // _MICROSECOND).scaleb(-6)},
                    upstream=(first, second),
                )
            )
        return out
