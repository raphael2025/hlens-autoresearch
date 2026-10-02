"""ADR-0082 / ADR-0088 / ADR-0100 rev. 1: ``P7TemporalSequenceProvider``, hand-checked.

``second`` within 1..``window`` bars after ``first`` means the occurrence gap lies in the
left-open, right-closed interval ``(0, window * bar]``. The upstream event times are observable
times (occurrence + the event's own lag): ``event_a`` lags 1 minute, ``event_b`` 2 minutes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from core.contracts.event import Event, EventInputPoint, EventRequest, UnsupportedEvent
from core.domain.base import Kind, Ref, VersionedSpec, content_hash
from core.domain.specs import EventSpec
from plugins.events.p7_temporal import P7TemporalSequenceProvider
from research.hypotheses.typed_plan import PlanLimits, parse_plan_json
from research.hypotheses.typed_plan_lowering import lower_typed_plan
from research.hypotheses.typed_plan_resolver import resolve_direct_references

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
CREATED = datetime(2026, 10, 1, tzinfo=UTC)
BAR_1M = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")
BAR_5M = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_5m", version="1.0.0")
FEATURE = Ref(kind=Kind.FEATURE, name="ev_feature", version="1.0.0")
DURATIONS = {str(BAR_1M): MINUTE}
LIMITS = PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=4096, max_parameters_per_node=4)

EVENT_A = EventSpec(
    name="event_a",
    version="1.0.0",
    created_at=CREATED,
    trigger="event_a_trigger",
    features=(FEATURE,),
    observable_lag=1 * MINUTE,
    bar_spec=BAR_1M,
)
EVENT_B = EventSpec(
    name="event_b",
    version="1.0.0",
    created_at=CREATED,
    trigger="event_b_trigger",
    features=(FEATURE,),
    observable_lag=2 * MINUTE,
    bar_spec=BAR_1M,
)


class _Resolver:
    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return {str(EVENT_A.ref): EVENT_A, str(EVENT_B.ref): EVENT_B}.get(str(ref))


def lowered(window: int = 3, *, schema_version: str = "1.3.0") -> EventSpec:
    payload = {
        "schema_version": schema_version,
        "root": "seq",
        "nodes": [
            {
                "id": "seq",
                "operator": "temporal",
                "inputs": [
                    {"ref": str(item.ref), "content_hash": item.content_hash()}
                    for item in (EVENT_A, EVENT_B)
                ],
                "parameters": {"window": window, "time_unit": "bar"},
            }
        ],
    }
    plan = parse_plan_json(json.dumps(payload), limits=LIMITS)
    resolution = resolve_direct_references(plan, resolver=_Resolver())
    return cast(EventSpec, lower_typed_plan(plan, resolution=resolution, created_at=CREATED)["seq"])


def provider(
    spec: EventSpec,
    *,
    upstream: dict[str, EventSpec] | None = None,
    durations: dict[str, timedelta] | None = None,
) -> P7TemporalSequenceProvider:
    return P7TemporalSequenceProvider(
        (spec,),
        upstream=upstream
        if upstream is not None
        else {str(EVENT_A.ref): EVENT_A, str(EVENT_B.ref): EVENT_B},
        bar_durations=DURATIONS if durations is None else durations,
    )


def occurred(spec: EventSpec, minute: int) -> Event:
    """An upstream event that occurred at ``T0 + minute`` (event_time = occurrence + own lag)."""
    at = T0 + minute * MINUTE + spec.observable_lag
    point = EventInputPoint(
        source=FEATURE,
        source_lineage_hash=content_hash({"point": spec.name, "minute": minute}),
        evaluation_time=at,
        available_time=at,
        value=Decimal(minute),
    )
    return Event.build(
        event=spec.ref, spec_hash=spec.content_hash(), event_time=at, inputs=(point,)
    )


def detect(
    spec: EventSpec, upstream: tuple[Event, ...], as_of: datetime, **kwargs: Any
) -> tuple[Event, ...]:
    request = EventRequest(
        event=spec.ref, spec_hash=spec.content_hash(), as_of=as_of, upstream_events=upstream
    )
    result = provider(spec, **kwargs).detect(request)
    return result.events


FIRSTS = {minute: occurred(EVENT_A, minute) for minute in (0, 1, 10)}
SECONDS = {minute: occurred(EVENT_B, minute) for minute in (0, 2, 3, 4, 5, 11)}
UPSTREAM = (*FIRSTS.values(), *SECONDS.values())
END = T0 + 30 * MINUTE


def gaps(events: tuple[Event, ...]) -> list[tuple[datetime, Decimal]]:
    return [(item.event_time, cast(Decimal, item.attributes["gap_seconds"])) for item in events]


# --- the lowered declaration ------------------------------------------------------------------


def test_lowered_spec_is_hash_bound_with_zero_observable_lag() -> None:
    spec = lowered()
    trigger = json.loads(spec.trigger)

    assert trigger["definition"] == "p7.temporal.sequence_within_bars@1.0.0"
    assert trigger["provider"] == P7TemporalSequenceProvider.plugin_key()
    assert trigger["first_event_hash"] == EVENT_A.content_hash()
    assert trigger["second_event_hash"] == EVENT_B.content_hash()
    assert spec.observable_lag == timedelta(0)
    assert provider(spec).descriptor.supports(spec.ref, spec.content_hash())


# --- hand-checked sequences -------------------------------------------------------------------


def test_second_within_the_window_pairs_with_the_latest_first() -> None:
    events = detect(lowered(3), UPSTREAM, END)

    # B@2: A@0 (gap 2) and A@1 (gap 1) qualify -> the latest first, A@1, gap 60 s.
    # B@3: A@1 gap 2 min; B@4: A@1 gap 3 min (the window's right end is closed);
    # B@11: only A@10 (A@1 is 10 bars back), gap 1 min.
    # B@0 (not after any first: the left end is open) and B@5 (4 bars after A@1) pair with none.
    assert gaps(events) == [
        (T0 + 4 * MINUTE, Decimal(60)),
        (T0 + 5 * MINUTE, Decimal(120)),
        (T0 + 6 * MINUTE, Decimal(180)),
        (T0 + 13 * MINUTE, Decimal(60)),
    ]
    by_time = {item.event_time: item for item in events}
    first_pair = by_time[T0 + 4 * MINUTE]
    assert first_pair.upstream_event_ids == tuple(sorted((FIRSTS[1].event_id, SECONDS[2].event_id)))
    assert all(item.event == lowered(3).ref for item in events)


def test_a_wider_window_reaches_further_back_and_a_narrower_one_less() -> None:
    narrow = detect(lowered(1), UPSTREAM, END)
    # window 1: gap in (0, 1 min]: only B@2 with A@1 and B@11 with A@10
    assert gaps(narrow) == [(T0 + 4 * MINUTE, Decimal(60)), (T0 + 13 * MINUTE, Decimal(60))]
    wide = detect(lowered(4), UPSTREAM, END)
    assert (T0 + 7 * MINUTE, Decimal(240)) in gaps(wide)  # B@5 now reaches A@1


def test_the_window_is_part_of_the_spec_identity() -> None:
    assert lowered(3).content_hash() != lowered(4).content_hash()
    assert lowered(3).content_hash() == lowered(3).content_hash()


def test_events_are_known_only_at_their_observable_time() -> None:
    """The output is dated at the second event's observable time; nothing earlier shows it."""
    spec = lowered(3)
    before = detect(spec, UPSTREAM, T0 + 3 * MINUTE + 59 * timedelta(seconds=1))
    assert before == ()
    at = detect(spec, UPSTREAM, T0 + 4 * MINUTE)
    assert gaps(at) == [(T0 + 4 * MINUTE, Decimal(60))]
    # a result as of an earlier time is a prefix of the result as of a later time
    later = detect(spec, UPSTREAM, T0 + 5 * MINUTE)
    assert later[: len(at)] == at


def test_upstream_events_after_the_as_of_time_never_change_an_earlier_answer() -> None:
    spec = lowered(3)
    base = detect(spec, UPSTREAM, T0 + 5 * MINUTE)
    extra = (*UPSTREAM, occurred(EVENT_A, 3), occurred(EVENT_B, 12))
    assert detect(spec, extra, T0 + 5 * MINUTE) == base


def test_no_pair_means_no_event_not_a_filled_one() -> None:
    spec = lowered(3)
    only_firsts = tuple(FIRSTS.values())
    assert detect(spec, only_firsts, END) == ()
    assert detect(spec, (), END) == ()


def test_an_older_plan_format_keeps_comparing_the_upstream_event_times() -> None:
    spec = lowered(3, schema_version="1.1.0")
    trigger = json.loads(spec.trigger)
    assert "first_event_hash" not in trigger
    assert spec.observable_lag == EVENT_B.observable_lag
    first_early, first_late = occurred(EVENT_A, 0), occurred(EVENT_A, 1)  # event_time 1, 2
    second = occurred(EVENT_B, 2)  # event_time 4
    events = detect(spec, (first_early, first_late, second), T0 + 10 * MINUTE)
    # direct event_time comparison: A@2 -> 4 is a 2 minute gap; dated 4 + the spec's lag (2)
    assert gaps(events) == [(T0 + 6 * MINUTE, Decimal(120))]


# --- refusals ---------------------------------------------------------------------------------


def test_a_request_for_another_event_or_hash_is_unsupported() -> None:
    spec = lowered(3)
    other = lowered(4)
    served = provider(spec)
    with pytest.raises(UnsupportedEvent):
        served.detect(EventRequest(event=other.ref, spec_hash=other.content_hash(), as_of=END))
    with pytest.raises(UnsupportedEvent):
        served.detect(EventRequest(event=spec.ref, spec_hash="0" * 64, as_of=END))


def test_construction_needs_an_explicit_positive_bar_duration() -> None:
    spec = lowered(3)
    with pytest.raises(ValueError, match="bar duration"):
        provider(spec, durations={})
    with pytest.raises(ValueError, match="positive timedelta"):
        provider(spec, durations={str(BAR_1M): timedelta(0)})
    with pytest.raises(ValueError, match="positive timedelta"):
        provider(spec, durations={str(BAR_1M): "1m"})  # type: ignore[dict-item]


def test_construction_needs_both_upstream_specs() -> None:
    spec = lowered(3)
    with pytest.raises(ValueError, match="upstream"):
        provider(spec, upstream={str(EVENT_A.ref): EVENT_A})
    with pytest.raises(ValueError, match="must be the EventSpec it names"):
        provider(spec, upstream={"event:wrong@1.0.0": EVENT_A})


def test_construction_refuses_an_upstream_that_is_not_the_hash_bound_one() -> None:
    spec = lowered(3)
    # the same ref with other content: the trigger's hash binding no longer matches
    changed_a = EVENT_A.model_copy(update={"trigger": "event_a_other_trigger"})
    with pytest.raises(ValueError, match="first_event_hash"):
        provider(spec, upstream={str(EVENT_A.ref): changed_a, str(EVENT_B.ref): EVENT_B})


def test_construction_refuses_upstream_events_on_another_bar_spec() -> None:
    spec = lowered(3)
    other_bar = EVENT_B.model_copy(update={"bar_spec": BAR_5M})
    with pytest.raises(ValueError):
        provider(spec, upstream={str(EVENT_A.ref): EVENT_A, str(EVENT_B.ref): other_bar})


def test_construction_refuses_a_spec_that_is_not_the_lowered_declaration() -> None:
    spec = lowered(3)
    trigger = json.loads(spec.trigger)
    for change in ({"window_bars": 0}, {"window_bars": True}, {"missing": "fill"}, {"extra": 1}):
        edited = spec.model_copy(update={"trigger": json.dumps({**trigger, **change})})
        with pytest.raises(ValueError, match="not a p7_temporal_sequence spec"):
            provider(edited)
    with pytest.raises(ValueError, match="observable_lag 0"):
        provider(spec.model_copy(update={"observable_lag": MINUTE}))
    with pytest.raises(TypeError, match="EventSpec"):
        P7TemporalSequenceProvider(
            (object(),),  # type: ignore[arg-type]
            upstream={},
            bar_durations=DURATIONS,
        )
    with pytest.raises(ValueError, match="at least one spec"):
        P7TemporalSequenceProvider((), upstream={}, bar_durations=DURATIONS)


def test_the_same_event_as_both_sides_is_refused_at_construction() -> None:
    spec = lowered(3)
    trigger = json.loads(spec.trigger)
    trigger["second_event"] = trigger["first_event"]
    edited = spec.model_copy(update={"trigger": json.dumps(trigger)})
    with pytest.raises(ValueError, match="temporal_same_input"):
        provider(edited)


def test_a_first_event_that_lags_more_than_the_second_is_refused() -> None:
    slow_a = EVENT_A.model_copy(update={"observable_lag": 5 * MINUTE})
    # re-lower against the slower first event so only the lag rule is violated
    resolver_specs = {str(slow_a.ref): slow_a, str(EVENT_B.ref): EVENT_B}
    payload = {
        "schema_version": "1.3.0",
        "root": "seq",
        "nodes": [
            {
                "id": "seq",
                "operator": "temporal",
                "inputs": [
                    {"ref": str(slow_a.ref), "content_hash": slow_a.content_hash()},
                    {"ref": str(EVENT_B.ref), "content_hash": EVENT_B.content_hash()},
                ],
                "parameters": {"window": 3, "time_unit": "bar"},
            }
        ],
    }
    plan = parse_plan_json(json.dumps(payload), limits=LIMITS)

    class _Slow:
        def resolve(self, ref: Ref) -> VersionedSpec | None:
            return resolver_specs.get(str(ref))

    from research.hypotheses.typed_plan_lowering import OperatorLoweringRefused

    resolution = resolve_direct_references(plan, resolver=_Slow())
    with pytest.raises(OperatorLoweringRefused, match="visibility"):
        lower_typed_plan(plan, resolution=resolution, created_at=CREATED)
