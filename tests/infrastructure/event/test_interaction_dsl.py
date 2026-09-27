"""Interaction DSL end to end through the runner (ADR-0061; ADR-0036 §5 upstream verification).

Bars → log-return feature run → trend/range state run → three leaf events (return crosses 0 up,
crosses 0 down, trend switch) → a DSL expression with every operator, compiled into interaction
specs and run hop by hop with ``run_events(..., require_full=True)``: every hop gets exactly its
declared upstream specs and results, every leaf its feature / state runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.event import Event, EventRequest, EventResult
from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateRequest, StateResult
from core.domain.base import content_hash
from core.domain.specs import EventSpec
from infrastructure.event.inputs import (
    inputs_from_feature_run,
    inputs_from_state_series,
    state_series_from_state_run,
)
from infrastructure.event.runner import UpstreamVerificationError, run_events
from infrastructure.event.upstream import verify_upstream
from infrastructure.feature.runner import run_feature
from infrastructure.state import run_state, state_inputs, state_request
from plugins.events import FeatureThresholdCrossProvider, StateSwitchProvider
from plugins.events.dsl import Compilation, CompileLimits, Hop, compile_expression, hops
from plugins.features import BarLogReturnProvider
from plugins.states import TrendRangeProvider
from tests.fake_states import T0, TEST_TREND_THRESHOLD, TEST_TREND_WINDOW, at, bar

MINUTE = timedelta(minutes=1)
MIN_US = 60_000_000
AS_OF = at(40)
LOG_RETURN = BarLogReturnProvider.spec()
TREND = TrendRangeProvider.spec(
    LOG_RETURN.ref, window=TEST_TREND_WINDOW, threshold=TEST_TREND_THRESHOLD
)
TIMES = tuple(at(minute) for minute in range(1, 31))
#: Trend (rising), then range (alternating), then trend again.
CLOSES: tuple[int, ...] = tuple(
    [100 + i for i in range(12)] + [111 + i % 2 for i in range(10)] + [112 + i for i in range(8)]
)
UP = FeatureThresholdCrossProvider.spec(LOG_RETURN.ref, Decimal(0), "up")
DOWN = FeatureThresholdCrossProvider.spec(LOG_RETURN.ref, Decimal(0), "down")
SWITCH = StateSwitchProvider.spec(TREND.ref)
REGISTRY = (UP, DOWN, SWITCH)


def ref(spec: EventSpec) -> dict[str, Any]:
    return {"ref": str(spec.ref)}


COUNT = {"op": "count", "a": ref(UP), "at_least": 3, "within_us": 4 * MIN_US}
NOT = {"op": "not", "a": ref(DOWN), "b": ref(SWITCH), "within_us": 2 * MIN_US}
SEQ = {"op": "seq", "a": COUNT, "b": NOT, "within_us": 3 * MIN_US}
EXPRESSION = {"op": "and", "a": SEQ, "b": ref(UP), "within_us": MIN_US}
LIMITS = CompileLimits(max_depth=4, max_nodes=8)
COMPILED = compile_expression(EXPRESSION, REGISTRY, LIMITS)


# ---------------------------------------------------------------- the pipeline


def _feature_run(closes: Sequence[int]) -> tuple[FeatureRequest, FeatureResult]:
    feature_request = FeatureRequest(
        feature=LOG_RETURN.ref,
        spec_hash=LOG_RETURN.content_hash(),
        manifest_content_hash=content_hash({"manifest": "dsl-test"}),
        knowledge_cutoff=at(60),
        evaluation_times=TIMES,
        observations=tuple(bar(minute, str(close)) for minute, close in enumerate(closes)),
    )
    return feature_request, run_feature(
        BarLogReturnProvider((LOG_RETURN,)), LOG_RETURN, feature_request
    )


def _state_run(
    feature_run: tuple[FeatureRequest, FeatureResult],
) -> tuple[StateRequest, StateResult]:
    state_req = state_request(TREND, TIMES, state_inputs([feature_run]))
    return state_req, run_state(TrendRangeProvider((TREND,)), TREND, state_req)


def _hop_request(spec: EventSpec, upstream: Sequence[EventResult], as_of: datetime) -> EventRequest:
    events = {item.event_id: item for result in upstream for item in result.events}
    return EventRequest(
        event=spec.ref,
        spec_hash=spec.content_hash(),
        as_of=as_of,
        upstream_events=tuple(events.values()),
    )


def run_hop(hop: Hop, results: dict[str, EventResult], as_of: datetime = AS_OF) -> EventResult:
    upstream = [results[str(item.ref)] for item in hop.upstream]
    return run_events(
        hop.provider,
        hop.spec,
        _hop_request(hop.spec, upstream, as_of),
        upstream_specs=hop.upstream,
        upstream_results=upstream,
        require_full=True,
    )


def pipeline(
    closes: Sequence[int] = CLOSES, compiled: Compilation = COMPILED
) -> dict[str, EventResult]:
    """Every leaf and every compiled hop, fully verified; results by spec ref."""
    feature_run = _feature_run(closes)
    state_run = _state_run(feature_run)
    feature_points = inputs_from_feature_run(*feature_run)
    state_points = inputs_from_state_series(TREND.ref, state_series_from_state_run(*state_run))
    results: dict[str, EventResult] = {}
    for spec, provider, points in (
        (UP, FeatureThresholdCrossProvider((UP, DOWN)), feature_points),
        (DOWN, FeatureThresholdCrossProvider((UP, DOWN)), feature_points),
        (SWITCH, StateSwitchProvider((SWITCH,)), state_points),
    ):
        req = EventRequest(
            event=spec.ref, spec_hash=spec.content_hash(), as_of=AS_OF, inputs=points
        )
        results[str(spec.ref)] = run_events(
            provider,
            spec,
            req,
            feature_runs=(feature_run,),
            state_runs=(state_run,),
            require_full=True,
        )
    for hop in hops(compiled, REGISTRY):
        results[str(hop.spec.ref)] = run_hop(hop, results)
    return results


RESULTS = pipeline()
HOPS = hops(COMPILED, REGISTRY)


def _hop(operator: str) -> Hop:
    return next(hop for hop in HOPS if hop.provider.descriptor.name == operator)


def _minutes(result: EventResult) -> list[int]:
    return [(item.event_time - T0) // MINUTE for item in result.events]


def _of(spec: EventSpec) -> EventResult:
    return RESULTS[str(spec.ref)]


# ---------------------------------------------------------------- end to end


def test_the_leaf_fixture_is_what_the_hand_checks_assume() -> None:
    assert _minutes(_of(UP)) == [14, 16, 18, 20, 22, 24]
    assert _minutes(_of(DOWN)) == [15, 17, 19, 21]
    assert _minutes(_of(SWITCH)) == [17, 26]


def test_a_multi_hop_expression_is_evaluated_hop_by_hop_and_hand_checked() -> None:
    assert [hop.provider.descriptor.name for hop in HOPS] == [
        "event_count",
        "event_window_end",
        "event_absence",
        "event_sequence",
        "event_co_occurrence",
    ]
    # count(up, 3, 4 min): at each up with >= 3 ups in [t - 4, t].
    count = RESULTS[str(_hop("event_count").spec.ref)]
    assert _minutes(count) == [18, 20, 22, 24]
    # not(down, switch, 2 min): window ends 17, 19, 21, 23; the switch at 17 blocks the first two
    # (17 is in [15, 17] and, inclusive, in [17, 19]).
    assert _minutes(RESULTS[str(_hop("event_window_end").spec.ref)]) == [17, 19, 21, 23]
    assert _minutes(RESULTS[str(_hop("event_absence").spec.ref)]) == [21, 23]
    # seq(count, not, 3 min): not@21 <- count@20, not@23 <- count@22.
    assert _minutes(RESULTS[str(_hop("event_sequence").spec.ref)]) == [21, 23]
    # and(seq, up, 1 min): seq@21 ~ up@20 / up@22, seq@23 ~ up@22 / up@24.
    root = RESULTS[str(COMPILED.root.ref)]
    assert _minutes(root) == [21, 22, 23, 24]


def test_every_hop_is_fully_verified_against_its_declared_upstream() -> None:
    for hop in HOPS:
        upstream = [RESULTS[str(item.ref)] for item in hop.upstream]
        report = verify_upstream(
            hop.spec,
            _hop_request(hop.spec, upstream, AS_OF),
            upstream_specs=hop.upstream,
            upstream_results=upstream,
            require_full=True,
        )
        assert report.full and "upstream_specs" in report.performed
        assert "upstream_results" in report.performed


def _trace(item: Event, by_id: dict[str, Event]) -> set[str]:
    """Every leaf input point an event rests on, through all hops."""
    points = set(item.input_ids)
    for parent in item.upstream_event_ids:
        points |= _trace(by_id[parent], by_id)
    return points


def test_root_events_trace_back_to_leaf_inputs_through_every_hop() -> None:
    by_id = {item.event_id: item for result in RESULTS.values() for item in result.events}
    for item in RESULTS[str(COMPILED.root.ref)].events:
        assert _trace(item, by_id), "every root event rests on leaf input points"


def test_a_hop_given_the_wrong_upstream_is_refused() -> None:
    absence = _hop("event_absence")
    window_end = _hop("event_window_end")
    # The absence spec binds the window-end spec: the raw leaf in its place is refused.
    with pytest.raises(UpstreamVerificationError):
        run_hop(Hop(absence.spec, absence.provider, (DOWN, SWITCH)), RESULTS)
    # A missing upstream result under require_full.
    with pytest.raises(UpstreamVerificationError):
        run_events(
            absence.provider,
            absence.spec,
            _hop_request(
                absence.spec,
                [RESULTS[str(window_end.spec.ref)], RESULTS[str(SWITCH.ref)]],
                AS_OF,
            ),
            upstream_specs=absence.upstream,
            upstream_results=[RESULTS[str(SWITCH.ref)]],
            require_full=True,
        )


# ---------------------------------------------------------------- causality


def test_not_never_fires_before_its_window_ends() -> None:
    absence = _hop("event_absence")
    events = RESULTS[str(absence.spec.ref)].events
    downs = {item.event_id: item for item in _of(DOWN).events}
    ends = {item.event_id: item for item in RESULTS[str(_hop("event_window_end").spec.ref)].events}
    assert events
    for item in events:
        (end_id,) = item.upstream_event_ids
        (down_id,) = ends[end_id].upstream_event_ids
        assert item.event_time == downs[down_id].event_time + 2 * MINUTE
        # As of one microsecond before the window end the event does not exist yet (every hop
        # below it rerun as of that time, fully verified).
        before = item.event_time - timedelta(microseconds=1)
        early = {**RESULTS}
        for hop in HOPS:
            early[str(hop.spec.ref)] = run_hop(hop, early, before)
            if hop.spec == absence.spec:
                break
        assert item not in early[str(absence.spec.ref)].events
        assert early[str(absence.spec.ref)].events == RESULTS[str(absence.spec.ref)].restricted_to(
            before
        )


def _perturbed(from_minute: int) -> tuple[int, ...]:
    return tuple(
        close if minute < from_minute else 120 + (minute * 7) % 5
        for minute, close in enumerate(CLOSES)
    )


@pytest.mark.parametrize("from_minute", [14, 17, 19, 21, 23])
def test_future_market_data_never_changes_past_interaction_events(from_minute: int) -> None:
    """Bars from ``from_minute`` on change; every table (leaf and hop) up to then is unchanged."""
    changed = pipeline(_perturbed(from_minute))
    cut = at(from_minute)  # the first changed bar is available only at from_minute + 1
    for key, result in RESULTS.items():
        assert changed[key].restricted_to(cut) == result.restricted_to(cut), key
    assert any(changed[key].events != result.events for key, result in RESULTS.items())


def test_the_perturbation_has_teeth() -> None:
    changed = pipeline(_perturbed(17))
    root = str(COMPILED.root.ref)
    assert changed[root].events != RESULTS[root].events
