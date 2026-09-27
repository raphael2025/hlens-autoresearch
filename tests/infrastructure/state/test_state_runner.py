"""State runner (Phase 2; ADR-0035 §1): structural truncation, spec checks, table, bars → states.

Smoke level: no look-ahead (perturbing the future leaves the past unchanged), determinism, fixed
window / seed for trained specs, and an end-to-end bars → features → states → table run.
All spec numbers are test-only fixture parameters (tests/fake_states.py).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from core.contracts.feature import FeatureRequest, FeatureResult
from core.contracts.state import (
    StateInput,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
    UnsupportedState,
)
from core.domain.base import FrozenMapping, content_hash
from core.domain.specs import FeatureSpec, StateSpec
from infrastructure.feature.runner import run_feature
from infrastructure.state import (
    StateRunnerError,
    run_state,
    state_inputs,
    state_request,
    state_table,
)
from plugins.features import BarLogReturnProvider, BarRealizedVolatilityProvider
from plugins.states import TrendRangeProvider, VolatilityRegimeProvider
from tests.fake_states import (
    RETURN_FEATURE,
    TEST_CUTS,
    TEST_MIN_HISTORY,
    TEST_SEED,
    TEST_TREND_THRESHOLD,
    TEST_TREND_WINDOW,
    TEST_WINDOW,
    VOL_FEATURE,
    at,
    bars,
    level_inputs,
    negate,
    return_inputs,
)

MANIFEST = content_hash({"manifest": "state-runner"})
TIMES = tuple(at(i) for i in range(31))


def vol_spec(feature: object = VOL_FEATURE) -> StateSpec:
    return VolatilityRegimeProvider.spec(
        feature,  # type: ignore[arg-type]
        cuts=TEST_CUTS,
        min_history=TEST_MIN_HISTORY,
        training_window=TEST_WINDOW,
        seed=TEST_SEED,
    )


class Recording:
    """Delegates to a real provider and records the inputs of every call."""

    def __init__(self, spec: StateSpec) -> None:
        self._inner = VolatilityRegimeProvider((spec,))
        self.seen: list[tuple[datetime, tuple[StateInput, ...]]] = []

    @property
    def descriptor(self) -> StateProviderDescriptor:
        return self._inner.descriptor

    def compute(self, request: StateRequest) -> StateResult:
        self.seen.append((request.evaluation_times[0], request.inputs))
        return self._inner.compute(request)


class Peeking:
    """Ignores the request's truncation contract: labels every time by the last input it sees."""

    def __init__(self, spec: StateSpec) -> None:
        self._inner = VolatilityRegimeProvider((spec,))
        self._spec = spec

    @property
    def descriptor(self) -> StateProviderDescriptor:
        return self._inner.descriptor

    def compute(self, request: StateRequest) -> StateResult:
        last = request.inputs[-1]
        values = [
            StateValue(
                evaluation_time=t,
                state=self._spec.state_space[0],
                inputs_used=1,
                latest_input_time=last.evaluation_time,
            )
            for t in request.evaluation_times
        ]
        return StateResult.build(request, self.descriptor, values)


def test_the_provider_sees_only_the_windowed_past() -> None:
    spec = vol_spec()
    provider = Recording(spec)
    run_state(provider, spec, state_request(spec, TIMES, level_inputs(VOL_FEATURE)))
    assert len(provider.seen) == len(TIMES)
    window = spec.training_window
    assert window is not None
    for t, inputs in provider.seen:
        assert all(t - window < item.evaluation_time <= t for item in inputs)


def test_perturbing_the_future_leaves_the_past_unchanged() -> None:
    spec = vol_spec()
    inputs = level_inputs(VOL_FEATURE)
    base = run_state(VolatilityRegimeProvider((spec,)), spec, state_request(spec, TIMES, inputs))
    cut = at(18)
    changed = tuple(negate(item) if item.evaluation_time > cut else item for item in inputs)
    after = run_state(VolatilityRegimeProvider((spec,)), spec, state_request(spec, TIMES, changed))
    past = [v for v in base.values if v.evaluation_time <= cut]
    assert past == [v for v in after.values if v.evaluation_time <= cut]
    assert base.values != after.values  # the perturbation has teeth


def test_runs_are_deterministic() -> None:
    spec = TrendRangeProvider.spec(
        RETURN_FEATURE, window=TEST_TREND_WINDOW, threshold=TEST_TREND_THRESHOLD
    )
    request = state_request(spec, TIMES, return_inputs())
    first = run_state(TrendRangeProvider((spec,)), spec, request)
    again = run_state(TrendRangeProvider((spec,)), spec, request)
    assert first.result_hash == again.result_hash
    assert first == TrendRangeProvider((spec,)).compute(request)  # runner == direct, compliant


def test_a_provider_that_peeks_past_the_window_is_caught_or_truncated() -> None:
    """Directly the peeker answers from the future (refused by the contract); via the runner it
    only ever sees the windowed past."""
    spec = vol_spec()
    request = state_request(spec, TIMES, level_inputs(VOL_FEATURE))
    with pytest.raises(ValueError, match="latest_input_time"):
        Peeking(spec).compute(request)
    through = run_state(Peeking(spec), spec, request)
    for value in through.values:
        assert value.latest_input_time is None or value.latest_input_time <= value.evaluation_time


def test_trained_specs_must_fix_the_seed() -> None:
    spec = vol_spec().model_copy(update={"seed": None})
    with pytest.raises(StateRunnerError, match="seed"):
        run_state(
            VolatilityRegimeProvider((vol_spec(),)),
            spec,
            state_request(spec, TIMES, level_inputs(VOL_FEATURE)),
        )


def test_inputs_outside_the_declared_features_are_refused() -> None:
    spec = vol_spec()
    stray = level_inputs(RETURN_FEATURE)
    with pytest.raises(StateRunnerError, match="declared features"):
        run_state(VolatilityRegimeProvider((spec,)), spec, state_request(spec, TIMES, stray))


def test_undeclared_spec_is_unsupported() -> None:
    spec = vol_spec()
    other = TrendRangeProvider.spec(RETURN_FEATURE, window=3, threshold=TEST_TREND_THRESHOLD)
    with pytest.raises(UnsupportedState):
        run_state(TrendRangeProvider((other,)), spec, state_request(spec, TIMES, ()))


def test_state_inputs_bind_feature_results() -> None:
    spec = BarLogReturnProvider.spec()
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=at(100),
        evaluation_times=(at(5), at(6)),
        observations=bars(6),
    )
    result = run_feature(BarLogReturnProvider((spec,)), spec, request)
    inputs = state_inputs([(request, result)])
    assert [item.source_result_hash for item in inputs] == [result.result_hash] * 2
    assert [item.feature for item in inputs] == [spec.ref] * 2
    other = request.model_copy(update={"evaluation_times": (at(5),)})
    with pytest.raises(StateRunnerError):
        state_inputs([(other, result)])


def _feature(
    spec: FeatureSpec, provider: object, times: tuple[datetime, ...], n: int
) -> tuple[FeatureRequest, FeatureResult]:
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=at(10_000),
        evaluation_times=times,
        observations=bars(n),
    )
    return request, run_feature(provider, spec, request)  # type: ignore[arg-type]


def test_bars_to_features_to_states_to_a_table() -> None:
    """End to end on synthetic bars; perturbing future bars leaves earlier states unchanged."""
    n = 40
    times = tuple(at(i) for i in range(1, n + 1))
    vol = BarRealizedVolatilityProvider.spec(5)
    state = VolatilityRegimeProvider.spec(
        vol.ref,
        cuts=TEST_CUTS,
        min_history=TEST_MIN_HISTORY,
        training_window=TEST_WINDOW,
        seed=TEST_SEED,
    )
    provider = VolatilityRegimeProvider((state,))
    inputs = state_inputs([_feature(vol, BarRealizedVolatilityProvider((vol,)), times, n)])
    request = state_request(state, times, inputs)
    result = run_state(provider, state, request)
    labels = [v.state for v in result.values]
    assert labels[0] is None  # not enough bars yet: explicit None, never filled
    assert set(labels) - {None} <= set(state.state_space)
    assert any(label is not None for label in labels)

    table = state_table(state, request, result, provider.descriptor)
    assert table.num_rows == len(times)
    assert table.column("state").null_count == labels.count(None)
    assert set(table.column("result_hash").to_pylist()) == {result.result_hash}

    # Feature values at t use bars closed by t; a changed future bar cannot move an earlier state.
    cut = 25
    changed_bars = tuple(
        b.model_copy(
            update={"values": FrozenMapping({**b.values, "close": Decimal(b.values["close"]) * 3})}
        )
        if b.event_time >= at(cut)
        else b
        for b in bars(n)
    )
    changed_request = FeatureRequest(
        feature=vol.ref,
        spec_hash=vol.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=at(10_000),
        evaluation_times=times,
        observations=changed_bars,
    )
    changed_feature = run_feature(BarRealizedVolatilityProvider((vol,)), vol, changed_request)
    changed_inputs = state_inputs([(changed_request, changed_feature)])
    changed = run_state(provider, state, state_request(state, times, changed_inputs))
    for before, after in zip(result.values, changed.values, strict=True):
        if before.evaluation_time <= at(cut):
            assert before == after, before.evaluation_time
