"""Runner-side upstream verification (ADR-0036 §5; implementation note of 2026-09-26).

An interaction run is checked against the upstream specs / results it is given (declared refs,
spec hashes, the Feature / State union), and input points against the feature / state runs they
were built from (per-point ``source_lineage_hash``). Every mismatch fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.event import Event, EventInputPoint, EventRequest, EventResult
from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import StateRequest, StateResult
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import EventSpec
from infrastructure.event.inputs import (
    inputs_from_feature_run,
    inputs_from_state_series,
    state_series_from_state_run,
)
from infrastructure.event.runner import (
    EventRunnerError,
    FutureConfirmationError,
    UpstreamVerificationError,
    run_events,
)
from infrastructure.feature.runner import run_feature
from infrastructure.state import run_state, state_inputs, state_request
from plugins.events import (
    EventCoOccurrenceProvider,
    EventSequenceProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
)
from plugins.features import BarLogReturnProvider
from plugins.states import TrendRangeProvider
from tests.fake_events import (
    LAG,
    MINUTE,
    REGIME,
    REGIME_INPUTS,
    X_INPUTS,
    BackdatedProvider,
    ConfirmedTopProvider,
    X,
    request,
)
from tests.fake_states import TEST_TREND_THRESHOLD, TEST_TREND_WINDOW, at, bar

CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)
#: Same ref as SWITCH, different content (hence a different spec hash).
SWITCH_TWIN = StateSwitchProvider.spec(
    REGIME, to_state="wild", name=SWITCH.name, observable_lag=LAG
)
TO_WILD = StateSwitchProvider.spec(
    REGIME, to_state="wild", name="regime_to_wild", observable_lag=LAG
)
SEQUENCE = EventSequenceProvider.spec(CROSS_UP, SWITCH, 3 * MINUTE, observable_lag=LAG)
CO_OCCUR = EventCoOccurrenceProvider.spec(CROSS_UP, SWITCH, MINUTE, observable_lag=LAG)
Y = Ref(kind=Kind.FEATURE, name="y", version="1.0.0")


def _run(provider: Any, spec: EventSpec, req: EventRequest, **verify: Any) -> EventResult:
    return run_events(provider, spec, req, **verify)


UP_RESULT = _run(
    FeatureThresholdCrossProvider((CROSS_UP,)), CROSS_UP, request(CROSS_UP, inputs=X_INPUTS)
)
SWITCH_RESULT = _run(StateSwitchProvider((SWITCH,)), SWITCH, request(SWITCH, inputs=REGIME_INPUTS))
TWIN_RESULT = _run(
    StateSwitchProvider((SWITCH_TWIN,)), SWITCH_TWIN, request(SWITCH_TWIN, inputs=REGIME_INPUTS)
)
WILD_RESULT = _run(StateSwitchProvider((TO_WILD,)), TO_WILD, request(TO_WILD, inputs=REGIME_INPUTS))
UPSTREAM: tuple[Event, ...] = UP_RESULT.events + SWITCH_RESULT.events


def _interaction(
    spec: EventSpec,
    upstream: Sequence[Event] = UPSTREAM,
    provider: Any = None,
    **verify: Any,
) -> EventResult:
    provider = provider or EventSequenceProvider((spec,))
    return _run(provider, spec, request(spec, upstream_events=tuple(upstream)), **verify)


def _variant(spec: EventSpec, **update: Any) -> EventSpec:
    return EventSpec.model_validate({**spec.model_dump(), **update})


# ---------------------------------------------------------------- matching interactions pass


@pytest.mark.parametrize(
    ("spec", "provider_cls"),
    [(SEQUENCE, EventSequenceProvider), (CO_OCCUR, EventCoOccurrenceProvider)],
)
def test_a_matching_interaction_passes(spec: EventSpec, provider_cls: Any) -> None:
    plain = _interaction(spec, provider=provider_cls((spec,)), upstream_specs=(SWITCH, CROSS_UP))
    strict = _interaction(
        spec,
        provider=provider_cls((spec,)),
        upstream_specs=(CROSS_UP, SWITCH),
        upstream_results=(UP_RESULT, SWITCH_RESULT),
    )
    assert plain == strict and plain.events
    assert plain == provider_cls((spec,)).detect(request(spec, upstream_events=UPSTREAM))


# ---------------------------------------------------------------- undeclared upstream


def test_an_interaction_without_its_upstream_specs_is_refused() -> None:
    with pytest.raises(UpstreamVerificationError, match="must be supplied"):
        _interaction(SEQUENCE)


def test_missing_or_extra_upstream_specs_are_refused() -> None:
    with pytest.raises(UpstreamVerificationError, match="no upstream spec supplied"):
        _interaction(SEQUENCE, upstream_specs=(CROSS_UP,))
    with pytest.raises(UpstreamVerificationError, match="does not declare the upstream specs"):
        _interaction(SEQUENCE, upstream_specs=(CROSS_UP, SWITCH, TO_WILD))
    with pytest.raises(UpstreamVerificationError, match="supplied twice"):
        _interaction(SEQUENCE, upstream_specs=(CROSS_UP, SWITCH, SWITCH))


def test_an_undeclared_upstream_event_is_refused() -> None:
    # The provider would refuse too, but only when called: the runner refuses first, by name.
    with pytest.raises(UpstreamVerificationError, match="does not declare the upstream event"):
        _interaction(SEQUENCE, UPSTREAM + WILD_RESULT.events, upstream_specs=(CROSS_UP, SWITCH))


def test_upstream_events_on_a_non_interaction_are_refused() -> None:
    provider = FeatureThresholdCrossProvider((CROSS_UP,))
    with pytest.raises(UpstreamVerificationError, match="declares no upstream events"):
        _run(
            provider,
            CROSS_UP,
            request(CROSS_UP, inputs=X_INPUTS, upstream_events=SWITCH_RESULT.events),
        )
    with pytest.raises(UpstreamVerificationError, match="declares no upstream events"):
        _run(provider, CROSS_UP, request(CROSS_UP, inputs=X_INPUTS), upstream_specs=(SWITCH,))


def test_an_upstream_event_outside_the_supplied_results_is_refused() -> None:
    with pytest.raises(UpstreamVerificationError, match="in none of the supplied"):
        _interaction(SEQUENCE, upstream_specs=(CROSS_UP, SWITCH), upstream_results=(UP_RESULT,))
    with pytest.raises(UpstreamVerificationError, match="does not declare the upstream event"):
        _interaction(
            SEQUENCE,
            upstream_specs=(CROSS_UP, SWITCH),
            upstream_results=(UP_RESULT, SWITCH_RESULT, WILD_RESULT),
        )


# ---------------------------------------------------------------- spec-hash mismatch


def test_an_upstream_spec_the_interaction_does_not_bind_is_refused() -> None:
    assert SWITCH_TWIN.ref == SWITCH.ref and SWITCH_TWIN.content_hash() != SWITCH.content_hash()
    with pytest.raises(UpstreamVerificationError, match="does not bind"):
        _interaction(
            SEQUENCE, UP_RESULT.events + TWIN_RESULT.events, upstream_specs=(CROSS_UP, SWITCH_TWIN)
        )


def test_an_upstream_event_with_another_spec_hash_is_refused() -> None:
    with pytest.raises(UpstreamVerificationError, match="spec hash"):
        _interaction(
            SEQUENCE, UP_RESULT.events + TWIN_RESULT.events, upstream_specs=(CROSS_UP, SWITCH)
        )


# ---------------------------------------------------------------- input union mismatch


@pytest.mark.parametrize(
    "update",
    [
        {"features": (X, Y)},  # a superset: a declared input no computation uses
        {"features": (), "states": (REGIME,)},  # a subset: hides a transitive input
        {"features": (Y,)},  # a different set
    ],
    ids=["superset", "subset", "different"],
)
def test_an_input_union_mismatch_is_refused(update: dict[str, Any]) -> None:
    spec = _variant(SEQUENCE, **update)
    # The provider accepts it (it checks only the form): the gap the runner now closes.
    provider = EventSequenceProvider((spec,))
    with pytest.raises(UpstreamVerificationError, match="exactly their union"):
        _interaction(spec, provider=provider, upstream_specs=(CROSS_UP, SWITCH))


# ---------------------------------------------------------------- per-point lineage


LOG_RETURN = BarLogReturnProvider.spec()
TREND = TrendRangeProvider.spec(
    LOG_RETURN.ref, window=TEST_TREND_WINDOW, threshold=TEST_TREND_THRESHOLD
)
TIMES = tuple(at(minute) for minute in range(1, 31))
#: Trend (rising), then range (alternating), then trend again: the state switches.
CLOSES = (
    [100 + i for i in range(12)] + [111 + i % 2 for i in range(10)] + [112 + i for i in range(8)]
)
RETURN_CROSS = FeatureThresholdCrossProvider.spec(LOG_RETURN.ref, Decimal(0), "up")
TREND_SWITCH = StateSwitchProvider.spec(TREND.ref)


def _feature_run() -> tuple[FeatureRequest, FeatureResult]:
    feature_request = FeatureRequest(
        feature=LOG_RETURN.ref,
        spec_hash=LOG_RETURN.content_hash(),
        manifest_content_hash=content_hash({"manifest": "lineage-test"}),
        knowledge_cutoff=at(60),
        evaluation_times=TIMES,
        observations=tuple(bar(minute, str(close)) for minute, close in enumerate(CLOSES)),
    )
    return feature_request, run_feature(
        BarLogReturnProvider((LOG_RETURN,)), LOG_RETURN, feature_request
    )


FEATURE_RUN = _feature_run()


def _state_run() -> tuple[StateRequest, StateResult]:
    state_req = state_request(TREND, TIMES, state_inputs([FEATURE_RUN]))
    return state_req, run_state(TrendRangeProvider((TREND,)), TREND, state_req)


STATE_RUN = _state_run()
FEATURE_POINTS = inputs_from_feature_run(*FEATURE_RUN)
STATE_POINTS = inputs_from_state_series(TREND.ref, state_series_from_state_run(*STATE_RUN))
INPUTS = FEATURE_POINTS + STATE_POINTS


def _lineage_run(spec: EventSpec, inputs: Sequence[EventInputPoint], **verify: Any) -> EventResult:
    provider: Any = (
        FeatureThresholdCrossProvider((spec,))
        if spec is RETURN_CROSS
        else StateSwitchProvider((spec,))
    )
    req = EventRequest(
        event=spec.ref, spec_hash=spec.content_hash(), as_of=at(40), inputs=tuple(inputs)
    )
    return run_events(provider, spec, req, **verify)


def _forged(
    points: tuple[EventInputPoint, ...], index: int, **update: Any
) -> list[EventInputPoint]:
    out = list(points)
    out[index] = out[index].model_copy(update=update)
    return out


RUNS = {"feature_runs": (FEATURE_RUN,), "state_runs": (STATE_RUN,)}


@pytest.mark.parametrize("spec", [RETURN_CROSS, TREND_SWITCH], ids=["feature", "state"])
def test_inputs_recomputed_from_their_runs_pass(spec: EventSpec) -> None:
    verified = _lineage_run(spec, INPUTS, **RUNS)
    assert verified == _lineage_run(spec, INPUTS)
    assert verified.events, "the fixture must produce events"


@pytest.mark.parametrize(
    ("points", "spec"),
    [(FEATURE_POINTS, RETURN_CROSS), (STATE_POINTS, TREND_SWITCH)],
    ids=["feature", "state"],
)
def test_a_forged_lineage_hash_is_refused(
    points: tuple[EventInputPoint, ...], spec: EventSpec
) -> None:
    forged = _forged(points, 7, source_lineage_hash=content_hash({"forged": 7}))
    _lineage_run(spec, forged)  # unchecked without the runs: the old honesty boundary
    with pytest.raises(UpstreamVerificationError, match="source_lineage_hash"):
        _lineage_run(spec, forged, **RUNS)


def test_a_forged_value_under_the_genuine_lineage_is_refused() -> None:
    index = next(i for i, item in enumerate(STATE_POINTS) if item.value is not None)
    flipped = "range" if STATE_POINTS[index].value != "range" else "trend_up"
    forged = _forged(STATE_POINTS, index, value=flipped)
    with pytest.raises(UpstreamVerificationError, match="differ from the supplied upstream run"):
        _lineage_run(TREND_SWITCH, forged, **RUNS)


def test_points_without_a_matching_run_are_refused() -> None:
    with pytest.raises(UpstreamVerificationError, match="no upstream run was supplied"):
        _lineage_run(TREND_SWITCH, INPUTS, state_runs=(STATE_RUN,))
    late = STATE_POINTS[-1].model_copy(update={"evaluation_time": at(35), "available_time": at(35)})
    with pytest.raises(UpstreamVerificationError, match="has no such point"):
        _lineage_run(TREND_SWITCH, (*STATE_POINTS, late), state_runs=(STATE_RUN,))


def test_unusable_or_ambiguous_runs_are_refused() -> None:
    with pytest.raises(UpstreamVerificationError, match="two upstream runs"):
        _lineage_run(TREND_SWITCH, STATE_POINTS, state_runs=(STATE_RUN, STATE_RUN))
    other_request = STATE_RUN[0].model_copy(update={"evaluation_times": TIMES[:-1]})
    with pytest.raises(UpstreamVerificationError, match="not usable"):
        _lineage_run(TREND_SWITCH, STATE_POINTS, state_runs=((other_request, STATE_RUN[1]),))


# ---------------------------------------------------------------- faulty providers still caught


class _BackdatedSequenceProvider(EventSequenceProvider):
    """FAULT: every interaction event is dated one minute before its observable time."""

    def events(
        self,
        spec: EventSpec,
        params: dict[str, Any],
        points: Sequence[EventInputPoint],
        upstream: Sequence[Event],
    ) -> list[Event]:
        by_id = {item.event_id: item for item in upstream}
        return [
            Event.build(
                event=item.event,
                spec_hash=item.spec_hash,
                event_time=item.event_time - timedelta(minutes=1),
                attributes=dict(item.attributes),
                upstream=[by_id[event_id] for event_id in item.upstream_event_ids],
            )
            for item in super().events(spec, params, points, upstream)
        ]


def test_faulty_providers_are_still_caught() -> None:
    with pytest.raises(FutureConfirmationError, match="back-dated"):
        _run(ConfirmedTopProvider((CROSS_UP,)), CROSS_UP, request(CROSS_UP, inputs=X_INPUTS))
    with pytest.raises(EventRunnerError, match="not compliant"):
        _run(BackdatedProvider((CROSS_UP,)), CROSS_UP, request(CROSS_UP, inputs=X_INPUTS))
    with pytest.raises(EventRunnerError, match="not compliant"):
        _interaction(
            SEQUENCE,
            provider=_BackdatedSequenceProvider((SEQUENCE,)),
            upstream_specs=(CROSS_UP, SWITCH),
            upstream_results=(UP_RESULT, SWITCH_RESULT),
        )
