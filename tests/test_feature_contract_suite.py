"""证明 FeatureProvider contract suite 有效（Phase 1 F4；ADR-0030 §4，roadmap 验收 #19）。

1. 两个刻意不同的合规替身（`tests/fake_features.py`）以未来实现相同的方式接入（继承
   `FeatureProviderContract` 并提供 subject fixture），通过**全部**检查；
2. 每个只带一处故障的变体都被指定的检查以 `ContractSuiteFailure` 判为不合规，且它在基础检查上
   仍然合规——suite 杀死它依靠的是针对性的行为检查，而不是变体整体坏掉。

`check_cutoff_is_refused_at_construction` 与 `check_non_finite_numbers_are_refused` 的输入一侧由契约
构造本身执行，没有哪个 Provider 能让它们失败，因此不在故障表中；结果一侧的 NaN 由故障表覆盖。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from core.contracts.feature import FeatureProvider
from tests.contract_suites import feature as feature_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.feature import FeatureProviderContract, FeatureSubject
from tests.fake_features import (
    CUTOFF,
    EVALUATION_TIMES,
    MANIFEST,
    OBSERVATIONS,
    AcceptsAnySpecProvider,
    CachingProvider,
    ContextDependentProvider,
    DriftingDescriptorProvider,
    ForgedHashProvider,
    IgnoresLagProvider,
    LatestValueProvider,
    NanValueProvider,
    NondeterministicProvider,
    PeeksAheadProvider,
    RunningCountProvider,
    ZeroFillProvider,
    fake_spec,
    perturb,
)

LATEST_SPEC = fake_spec("latest_x")
COUNT_SPEC = fake_spec("running_n", min_inputs=3)
#: A second declared spec, so "accepts any spec" has something else to (wrongly) compute.
OTHER_SPEC = fake_spec("other_n", min_inputs=1)


def subject(make: Callable[[], FeatureProvider], spec: object = LATEST_SPEC) -> FeatureSubject:
    return FeatureSubject(
        open=make,
        spec=spec,  # type: ignore[arg-type]
        observations=OBSERVATIONS,
        evaluation_times=EVALUATION_TIMES,
        knowledge_cutoff=CUTOFF,
        manifest_content_hash=MANIFEST,
        perturb=perturb,
    )


def latest_subject(cls: type[LatestValueProvider] = LatestValueProvider) -> FeatureSubject:
    return subject(lambda: cls((LATEST_SPEC,)), LATEST_SPEC)


def count_subject(cls: type[RunningCountProvider] = RunningCountProvider) -> FeatureSubject:
    return subject(lambda: cls((OTHER_SPEC, COUNT_SPEC)), COUNT_SPEC)


class TestLatestValueProviderContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return latest_subject()


class TestRunningCountProviderContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return count_subject()


FEATURE_KILLS: tuple[tuple[str, Callable[[], FeatureSubject], feature_suite.FeatureCheck], ...] = (
    (
        "nondeterministic",
        lambda: latest_subject(NondeterministicProvider),
        feature_suite.check_determinism,
    ),
    (
        "peeks-ahead",
        lambda: latest_subject(PeeksAheadProvider),
        feature_suite.check_causal_perturbation,
    ),
    (
        "ignores-lag",
        lambda: latest_subject(IgnoresLagProvider),
        feature_suite.check_lag_is_respected,
    ),
    (
        "zero-fill",
        lambda: latest_subject(ZeroFillProvider),
        feature_suite.check_not_computable_is_explicit_none,
    ),
    (
        "depends-on-other-evaluation-times",
        lambda: latest_subject(ContextDependentProvider),
        feature_suite.check_values_are_one_to_one_with_evaluation_times,
    ),
    (
        "caches-stale-results",
        lambda: count_subject(CachingProvider),
        feature_suite.check_hash_sensitivity,
    ),
    (
        "forged-result-hash",
        lambda: count_subject(ForgedHashProvider),
        feature_suite.check_determinism,
    ),
    (
        "nan-value",
        lambda: latest_subject(NanValueProvider),
        feature_suite.check_non_finite_numbers_are_refused,
    ),
    (
        "drifting-descriptor",
        lambda: count_subject(DriftingDescriptorProvider),
        feature_suite.check_descriptor_declares_the_spec,
    ),
    (
        "accepts-any-spec",
        lambda: count_subject(AcceptsAnySpecProvider),
        feature_suite.check_unsupported_feature_is_refused,
    ),
)


@pytest.mark.parametrize(
    ("make_subject", "check"),
    [pytest.param(make, check, id=name) for name, make, check in FEATURE_KILLS],
)
def test_feature_suite_kills_faulty_implementation(
    make_subject: Callable[[], FeatureSubject], check: feature_suite.FeatureCheck
) -> None:
    with pytest.raises(ContractSuiteFailure):
        check(make_subject())


def test_every_kill_uses_a_published_suite_check() -> None:
    assert {check for _, _, check in FEATURE_KILLS} <= set(feature_suite.FEATURE_CHECKS)


#: Variants whose single fault shows in every compute, so no basic check can pass them.
#: zero-fill joined them with F4-R1: a value computed from zero inputs is now refused by
#: ``FeatureValue`` itself, so such a provider cannot even produce a result.
_BROKEN_EVERYWHERE = {"forged-result-hash", "nan-value", "drifting-descriptor", "zero-fill"}


def test_a_value_from_zero_inputs_cannot_be_built() -> None:
    """F4-R1 (cursor review 1/2): filling is refused by the contract, not only by the suite."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from pydantic import ValidationError

    from core.contracts.feature import FeatureValue

    at = datetime(2024, 3, 1, 12, tzinfo=UTC)
    with pytest.raises(ValidationError, match="value 必须为 None"):
        FeatureValue(evaluation_time=at, value=Decimal(0), inputs_used=0)
    assert FeatureValue(evaluation_time=at, value=None, inputs_used=0).value is None


@pytest.mark.parametrize(
    "make_subject",
    [
        pytest.param(make, id=name)
        for name, make, _ in FEATURE_KILLS
        if name not in _BROKEN_EVERYWHERE
    ],
)
def test_faulty_variants_are_otherwise_plausible(
    make_subject: Callable[[], FeatureSubject],
) -> None:
    """其余变体都通过 descriptor 检查：只错在被杀死的那一处。"""
    feature_suite.check_descriptor_declares_the_spec(make_subject())


def test_the_two_compliant_doubles_really_differ() -> None:
    """两个替身的值类型与算法都不同：suite 不依赖某一种实现形状。"""
    from core.contracts.feature import FeatureRequest

    latest = LatestValueProvider((LATEST_SPEC,))
    count = RunningCountProvider((COUNT_SPEC,))
    request = FeatureRequest(
        feature=LATEST_SPEC.ref,
        spec_hash=LATEST_SPEC.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=EVALUATION_TIMES,
        observations=OBSERVATIONS,
    )
    other = request.model_copy(
        update={"feature": COUNT_SPEC.ref, "spec_hash": COUNT_SPEC.content_hash()}
    )
    latest_values = {type(v.value) for v in latest.compute(request).values} - {type(None)}
    count_values = {type(v.value) for v in count.compute(other).values} - {type(None)}
    assert latest_values and count_values and latest_values.isdisjoint(count_values)


def test_the_doubles_are_statically_substitutable() -> None:
    providers: list[FeatureProvider] = [
        LatestValueProvider((LATEST_SPEC,)),
        RunningCountProvider((COUNT_SPEC,)),
    ]
    assert len({provider.descriptor.name for provider in providers}) == 2
