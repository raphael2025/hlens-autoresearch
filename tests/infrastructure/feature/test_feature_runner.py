"""F4 feature runner: structural truncation (ADR-0030 §1, option A)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

import pytest

from core.contracts.feature import (
    FeatureRequest,
    FeatureResult,
    UnsupportedFeature,
)
from core.domain.base import content_hash
from core.domain.specs import FeatureSpec
from infrastructure.feature.runner import FeatureRunnerError, run_feature
from plugins.features import BarRealizedVolatilityProvider, BarVolumeSumProvider
from tests.fake_features import (
    CUTOFF,
    EVALUATION_TIMES,
    MANIFEST,
    OBSERVATIONS,
    DriftingDescriptorProvider,
    ForgedHashProvider,
    IgnoresLagProvider,
    LatestValueProvider,
    PeeksAheadProvider,
    RunningCountProvider,
    fake_spec,
)
from tests.plugins.features.test_bar_features import SUITE_BARS, SUITE_TIMES

SPEC = fake_spec("latest_x")


def _request(spec: FeatureSpec = SPEC, **overrides: Any) -> FeatureRequest:
    fields: dict[str, Any] = {
        "feature": spec.ref,
        "spec_hash": spec.content_hash(),
        "manifest_content_hash": MANIFEST,
        "knowledge_cutoff": CUTOFF,
        "evaluation_times": EVALUATION_TIMES,
        "observations": OBSERVATIONS,
    }
    fields.update(overrides)
    return FeatureRequest(**fields)


class Spy(LatestValueProvider):
    """Records every request the runner hands over."""

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        super().__init__(specs)
        self.seen: list[FeatureRequest] = []

    def compute(self, request: FeatureRequest) -> FeatureResult:
        self.seen.append(request)
        return super().compute(request)


def test_the_provider_only_ever_sees_the_visible_set() -> None:
    spy = Spy((SPEC,))
    request = _request()
    run_feature(spy, SPEC, request)
    assert spy.seen
    answered = [at for sub in spy.seen for at in sub.evaluation_times]
    assert tuple(answered) == request.evaluation_times
    for sub in spy.seen:
        for at in sub.evaluation_times:
            assert sub.observations == request.visible_at(at, SPEC.available_lag)
            for item in sub.observations:
                assert item.available_time + SPEC.available_lag <= at
                assert item.knowledge_time <= request.knowledge_cutoff
        # The superseded k2 revision is never handed over next to its replacement.
        keys = [item.observation_key for item in sub.observations]
        assert len(keys) == len(set(keys))


def test_one_call_per_distinct_visible_set() -> None:
    spy = Spy((SPEC,))
    run_feature(spy, SPEC, _request())
    sets = [
        tuple((item.observation_key, item.available_time) for item in sub.observations)
        for sub in spy.seen
    ]
    assert len(sets) == len(set(sets))  # consecutive times with one visible set share a call


@pytest.mark.parametrize("provider", [LatestValueProvider, RunningCountProvider])
def test_the_run_equals_a_compliant_direct_answer(provider: type[Any]) -> None:
    spec = fake_spec("latest_x" if provider is LatestValueProvider else "running_n")
    request = _request(spec)
    direct = provider((spec,)).compute(request)
    ran = run_feature(provider((spec,)), spec, request)
    assert ran == direct and ran.result_hash == direct.result_hash


@pytest.mark.parametrize("leaky", [PeeksAheadProvider, IgnoresLagProvider])
def test_a_leaky_provider_cannot_leak_through_the_runner(leaky: type[Any]) -> None:
    """Option A: truncation is structural, so the leaky variants give the honest answer."""
    request = _request()
    honest = LatestValueProvider((SPEC,)).compute(request)
    assert leaky((SPEC,)).compute(request).values != honest.values  # it does leak directly
    ran = run_feature(leaky((SPEC,)), SPEC, request)
    assert ran.values == honest.values


def test_real_providers_run_equals_direct() -> None:
    for provider, spec in (
        (BarRealizedVolatilityProvider, BarRealizedVolatilityProvider.spec(2)),
        (BarVolumeSumProvider, BarVolumeSumProvider.spec(3, available_lag=timedelta(minutes=1))),
    ):
        request = _request(spec, evaluation_times=SUITE_TIMES, observations=SUITE_BARS)
        assert run_feature(provider((spec,)), spec, request) == provider((spec,)).compute(request)


def test_the_request_must_be_for_the_spec() -> None:
    other = fake_spec("other_x")
    with pytest.raises(FeatureRunnerError, match="not for"):
        run_feature(LatestValueProvider((SPEC,)), SPEC, _request(other))
    with pytest.raises(FeatureRunnerError, match="not for"):
        run_feature(
            LatestValueProvider((SPEC,)),
            SPEC,
            _request(spec_hash=content_hash({"other": 1})),
        )


def test_an_undeclared_spec_is_refused_before_any_call() -> None:
    spy = Spy((fake_spec("other_x"),))
    with pytest.raises(UnsupportedFeature):
        run_feature(spy, SPEC, _request())
    assert spy.seen == []


def test_a_non_deterministic_spec_is_refused() -> None:
    loose = SPEC.model_copy(update={"deterministic": False})
    with pytest.raises(FeatureRunnerError, match="deterministic"):
        run_feature(LatestValueProvider((loose,)), loose, _request(loose))


@pytest.mark.parametrize(
    ("provider", "match"),
    [
        (ForgedHashProvider, "not compliant"),
        (DriftingDescriptorProvider, "not compliant"),
    ],
)
def test_a_non_compliant_answer_is_refused(provider: type[Any], match: str) -> None:
    spec = fake_spec("running_n", min_inputs=3)
    with pytest.raises(FeatureRunnerError, match=match):
        run_feature(provider((spec,)), spec, _request(spec))


def test_an_answer_for_another_request_is_refused() -> None:
    class Replays(RunningCountProvider):
        def compute(self, request: FeatureRequest) -> FeatureResult:
            full = request.model_copy(update={"observations": OBSERVATIONS})
            return super().compute(full)  # answers with inputs it was not given

    spec = fake_spec("running_n")
    with pytest.raises(FeatureRunnerError, match="request_hash"):
        run_feature(Replays((spec,)), spec, _request(spec))
