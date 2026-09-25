"""`FeatureProvider` 的 provider-agnostic contract suite（core/contracts/feature.py；ADR-0030 §4）。

实现方提供 `FeatureSubject`：

- `open`：每次调用返回一个**新** Provider 实例（模拟重启）；
- `spec`：被测的 `FeatureSpec`，Provider 必须在 descriptor 中声明支持它；`available_lag` 必须为正
  （否则 lag 检查没有对象）；
- `observations` / `evaluation_times` / `knowledge_cutoff` / `manifest_content_hash`：一份请求夹具。
  它必须让检查不落空：至少一个评估时刻之后还有观察变为可用、至少一个评估时刻的 lag 窗口
  `(t - available_lag, t]` 内有观察变为可用，且第一个评估时刻早于任何观察可见；
- `perturb`：返回同键、同时间、内容不同的观察（例如价格与成交量翻倍）；把它施加于全部观察必须
  改变至少一个值，否则扰动检查没有牙齿。

检查只看可观察行为：descriptor、`compute` 的结果、以及对请求的扰动 / 截断 / 变体下结果如何变化。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.feature import (
    FeatureObservation,
    FeatureProvider,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
    UnsupportedFeature,
)
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import FeatureSpec
from tests.contract_suites._support import (
    ContractSuiteFailure,
    call_ok,
    expect_error,
    require,
    revalidated,
)

__all__ = [
    "FEATURE_CHECKS",
    "FeatureCheck",
    "FeatureProviderContract",
    "FeatureSubject",
]

_TICK = timedelta(microseconds=1)


@dataclass(frozen=True)
class FeatureSubject:
    """被测 `FeatureProvider` 的接入点（见模块文档）。"""

    open: Callable[[], FeatureProvider]
    spec: FeatureSpec
    observations: tuple[FeatureObservation, ...]
    evaluation_times: tuple[datetime, ...]
    knowledge_cutoff: datetime
    manifest_content_hash: str
    perturb: Callable[[FeatureObservation], FeatureObservation]


type FeatureCheck = Callable[[FeatureSubject], None]


# ======================================================================================
# 工具
# ======================================================================================


def _lag(subject: FeatureSubject) -> timedelta:
    lag = subject.spec.available_lag
    require(lag > timedelta(0), "subject.spec 的 available_lag 必须为正（lag 检查需要它）")
    return lag


def _request(
    subject: FeatureSubject,
    *,
    observations: Sequence[FeatureObservation] | None = None,
    evaluation_times: Sequence[datetime] | None = None,
    **overrides: Any,
) -> FeatureRequest:
    fields: dict[str, Any] = {
        "feature": subject.spec.ref,
        "spec_hash": subject.spec.content_hash(),
        "manifest_content_hash": subject.manifest_content_hash,
        "knowledge_cutoff": subject.knowledge_cutoff,
        "evaluation_times": tuple(
            subject.evaluation_times if evaluation_times is None else evaluation_times
        ),
        "observations": tuple(subject.observations if observations is None else observations),
    }
    fields.update(overrides)
    return call_ok("构造 FeatureRequest（夹具）", lambda: FeatureRequest(**fields))


def _descriptor(provider: FeatureProvider) -> ProviderDescriptor:
    raw = call_ok("descriptor", lambda: provider.descriptor)
    return revalidated(ProviderDescriptor, raw, "descriptor")


def _compute(
    subject: FeatureSubject, provider: FeatureProvider, request: FeatureRequest
) -> FeatureResult:
    """执行一次 compute；结果必须恰好是合规的 `FeatureResult` 并如实回答该请求。"""
    raw = call_ok("compute", partial(provider.compute, request))
    result = revalidated(FeatureResult, raw, "compute 的结果")
    try:
        result.check_answers(request, _descriptor(provider), subject.spec.available_lag)
    except ValueError as exc:
        raise ContractSuiteFailure(f"compute 的结果没有如实回答请求：{exc}") from exc
    return result


def _value_at(result: FeatureResult, at: datetime) -> FeatureValue:
    [value] = [item for item in result.values if item.evaluation_time == at]
    return value


def _perturbed(
    subject: FeatureSubject, which: Callable[[FeatureObservation], bool]
) -> tuple[FeatureObservation, ...]:
    out = []
    for item in subject.observations:
        if which(item):
            changed = subject.perturb(item)
            require(
                (changed.observation_key, changed.available_time, changed.knowledge_time)
                == (item.observation_key, item.available_time, item.knowledge_time),
                "subject.perturb 只能改变观察内容，不能改变键或时间",
            )
            out.append(changed)
        else:
            out.append(item)
    return tuple(out)


def _require_perturbation_has_teeth(subject: FeatureSubject, base: FeatureResult) -> None:
    provider = subject.open()
    everything = _compute(
        subject, provider, _request(subject, observations=_perturbed(subject, lambda _: True))
    )
    require(
        everything.values != base.values,
        "subject.perturb 施加于全部观察必须改变至少一个值（否则扰动检查没有牙齿）",
    )


# ======================================================================================
# 检查
# ======================================================================================


def check_descriptor_declares_the_spec(subject: FeatureSubject) -> None:
    """descriptor 声明确定性并绑定被测规格的内容哈希；实例生命周期内与跨实例都不变。"""
    provider = subject.open()
    descriptor = _descriptor(provider)
    require(descriptor.deterministic is True, "FeatureProvider 必须声明 deterministic=True")
    require(
        descriptor.supports(subject.spec.ref, subject.spec.content_hash()),
        f"descriptor 必须声明支持 {subject.spec.ref} 及其 spec hash",
    )
    _compute(subject, provider, _request(subject))
    require(_descriptor(provider) == descriptor, "descriptor 在实例生命周期内不得改变")
    require(_descriptor(subject.open()) == descriptor, "同一实现的新实例 descriptor 必须相同")


def check_values_are_one_to_one_with_evaluation_times(subject: FeatureSubject) -> None:
    """每个评估时刻恰好一个值，且该值与请求中其它评估时刻无关（单独请求得到同一值）。"""
    provider = subject.open()
    full = _compute(subject, provider, _request(subject))
    require(
        tuple(item.evaluation_time for item in full.values) == subject.evaluation_times,
        "values 必须按顺序与 evaluation_times 一一对应",
    )
    for at in subject.evaluation_times:
        alone = _compute(subject, provider, _request(subject, evaluation_times=(at,)))
        require(
            alone.values == (_value_at(full, at),),
            f"{at.isoformat()} 的值不得依赖请求中的其它评估时刻",
        )


def check_determinism(subject: FeatureSubject) -> None:
    """同一请求（同一实例重复、重建实例、观察输入顺序不同）→ 同一结果与同一 result_hash。"""
    request = _request(subject)
    provider = subject.open()
    first = _compute(subject, provider, request)
    shuffled = _request(subject, observations=tuple(reversed(subject.observations)))
    require(shuffled == request, "FeatureRequest 必须把观察规范排序（夹具自检）")
    for label, again in (
        ("同一实例重复", _compute(subject, provider, request)),
        ("重建实例", _compute(subject, subject.open(), request)),
        ("观察输入顺序不同", _compute(subject, subject.open(), shuffled)),
    ):
        require(again.result_hash == first.result_hash, f"{label}：result_hash 必须相同")
        require(again == first, f"{label}：结果必须相同")


def check_causal_perturbation(subject: FeatureSubject) -> None:
    """改变 / 删去 t 之后才可用的观察，t 的值不变（防御执行器之外的直接调用）。"""
    provider = subject.open()
    base = _compute(subject, provider, _request(subject))
    _require_perturbation_has_teeth(subject, base)
    exercised = False
    for at in subject.evaluation_times:
        future = [item for item in subject.observations if item.available_time > at]
        if not future:
            continue
        exercised = True

        def after_t(item: FeatureObservation, at: datetime = at) -> bool:
            return item.available_time > at

        changed = _perturbed(subject, after_t)
        after = _compute(subject, provider, _request(subject, observations=changed))
        require(
            _value_at(after, at) == _value_at(base, at),
            f"{at.isoformat()} 的值受到了之后才可用的观察影响（扰动）",
        )
        past = [item for item in subject.observations if item.available_time <= at]
        cut = _compute(subject, provider, _request(subject, observations=past))
        require(
            _value_at(cut, at) == _value_at(base, at),
            f"{at.isoformat()} 的值受到了之后才可用的观察影响（截断）",
        )
    require(exercised, "夹具必须至少有一个评估时刻之后还有观察变为可用")


def check_lag_is_respected(subject: FeatureSubject) -> None:
    """`available_time + available_lag > t` 的观察不得影响 t 的值。"""
    lag = _lag(subject)
    provider = subject.open()
    base = _compute(subject, provider, _request(subject))
    exercised = False
    for at in subject.evaluation_times:

        def in_band(item: FeatureObservation, at: datetime = at) -> bool:
            return at - lag < item.available_time <= at

        if not any(in_band(item) for item in subject.observations):
            continue
        exercised = True
        after = _compute(
            subject, provider, _request(subject, observations=_perturbed(subject, in_band))
        )
        require(
            _value_at(after, at) == _value_at(base, at),
            f"{at.isoformat()} 的值受到了 available_time + available_lag > t 的观察影响（扰动）",
        )
        request = _request(subject)
        visible = request.visible_at(at, lag)
        cut = _compute(subject, provider, _request(subject, observations=visible))
        require(
            _value_at(cut, at) == _value_at(base, at),
            f"{at.isoformat()} 的值不只取决于可见集合（截断到可见集合后改变）",
        )
    require(exercised, "夹具必须至少有一个评估时刻的 lag 窗口内有观察变为可用")


def check_cutoff_is_refused_at_construction(subject: FeatureSubject) -> None:
    """knowledge_time 晚于 knowledge_cutoff 的观察在请求构造时即被拒绝，到不了 Provider。"""
    latest = max(item.knowledge_time for item in subject.observations)
    fields: dict[str, Any] = {
        "feature": subject.spec.ref,
        "spec_hash": subject.spec.content_hash(),
        "manifest_content_hash": subject.manifest_content_hash,
        "knowledge_cutoff": latest - _TICK,
        "evaluation_times": subject.evaluation_times,
        "observations": subject.observations,
    }
    expect_error(
        ValidationError, "构造越过 knowledge_cutoff 的请求", lambda: FeatureRequest(**fields)
    )
    # 恰在截止处（含等号）是合法的，且 Provider 能回答。
    _compute(subject, subject.open(), _request(subject, knowledge_cutoff=latest))


def check_not_computable_is_explicit_none(subject: FeatureSubject) -> None:
    """无可见输入时值显式为 None（不填补），且不报告任何输入。"""
    provider = subject.open()
    lag = subject.spec.available_lag
    first_visible = min(item.available_time for item in subject.observations) + lag
    early = subject.evaluation_times[0]
    require(early < first_visible, "夹具的第一个评估时刻必须早于任何观察可见")
    for label, request in (
        ("第一个评估时刻", _request(subject, evaluation_times=(early,))),
        ("没有任何观察", _request(subject, observations=())),
    ):
        result = _compute(subject, provider, request)
        for item in result.values:
            if request.visible_at(item.evaluation_time, lag):
                continue
            require(
                item.value is None, f"{label}：无可见输入时值必须显式为 None，实际 {item.value!r}"
            )
            require(item.inputs_used == 0, f"{label}：无可见输入时 inputs_used 必须为 0")


def check_non_finite_numbers_are_refused(subject: FeatureSubject) -> None:
    """NaN / ±Infinity / 二进制浮点既不能进入观察，也不能作为结果值（ADR-0013）。"""
    sample = subject.observations[0]
    name = next(iter(sample.values))
    bad_values: tuple[Any, ...] = (
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        float("nan"),
        1.5,
    )
    for bad in bad_values:
        payload = sample.model_dump()
        payload["values"] = {**payload["values"], name: bad}
        expect_error(
            ValidationError, f"观察值 {bad!r}", partial(FeatureObservation.model_validate, payload)
        )
        expect_error(
            ValidationError,
            f"特征值 {bad!r}",
            partial(
                FeatureValue,
                evaluation_time=subject.evaluation_times[0],
                value=bad,
                inputs_used=0,
            ),
        )
    result = _compute(subject, subject.open(), _request(subject))
    for item in result.values:
        if isinstance(item.value, Decimal):
            require(item.value.is_finite(), f"{item.evaluation_time.isoformat()} 的值不是有限数")


def check_hash_sensitivity(subject: FeatureSubject) -> None:
    """请求的任一输入变化 → request_hash 与 result_hash 都变化（同一实例，防缓存旧结果）。"""
    provider = subject.open()
    base = _compute(subject, provider, _request(subject))
    last = subject.evaluation_times[-1]
    visible = [
        item.observation_key
        for item in _request(subject).visible_at(last, subject.spec.available_lag)
    ]
    require(bool(visible), "夹具的最后一个评估时刻必须有可见观察")
    target = visible[-1]
    later_cutoff = subject.knowledge_cutoff + timedelta(days=1)
    variants: dict[str, FeatureRequest] = {
        "少一个评估时刻": _request(subject, evaluation_times=subject.evaluation_times[:-1]),
        "多一个评估时刻": _request(
            subject, evaluation_times=(*subject.evaluation_times, last + timedelta(minutes=1))
        ),
        "一条观察内容变化": _request(
            subject,
            observations=_perturbed(subject, lambda item: item.observation_key == target),
        ),
        "少一条观察": _request(
            subject,
            observations=[item for item in subject.observations if item.observation_key != target],
        ),
        "knowledge_cutoff 变化": _request(subject, knowledge_cutoff=later_cutoff),
        "manifest 变化": _request(
            subject, manifest_content_hash=content_hash({"other": subject.manifest_content_hash})
        ),
    }
    seen = {base.result_hash}
    for label, request in variants.items():
        result = _compute(subject, provider, request)
        require(result.result_hash not in seen, f"{label}：result_hash 必须随输入变化")
        seen.add(result.result_hash)
    require(
        _compute(subject, provider, _request(subject)).result_hash == base.result_hash,
        "变体之后重算原请求必须回到原 result_hash",
    )


def check_unsupported_feature_is_refused(subject: FeatureSubject) -> None:
    """未声明的 feature 版本或 spec hash → `UnsupportedFeature`，不得"尽量计算"。"""
    provider = subject.open()
    descriptor = _descriptor(provider)
    spec = subject.spec
    other_hash = content_hash({"not": spec.content_hash()})
    major = 999_999
    while descriptor.supports(
        Ref(kind=Kind.FEATURE, name=spec.name, version=f"{major}.0.0"), spec.content_hash()
    ):
        major += 1
    for label, request in (
        ("未声明的 spec hash", _request(subject, spec_hash=other_hash)),
        (
            "未声明的版本",
            _request(
                subject, feature=Ref(kind=Kind.FEATURE, name=spec.name, version=f"{major}.0.0")
            ),
        ),
    ):
        expect_error(UnsupportedFeature, f"compute({label})", partial(provider.compute, request))


FEATURE_CHECKS: tuple[FeatureCheck, ...] = (
    check_descriptor_declares_the_spec,
    check_values_are_one_to_one_with_evaluation_times,
    check_determinism,
    check_causal_perturbation,
    check_lag_is_respected,
    check_cutoff_is_refused_at_construction,
    check_not_computable_is_explicit_none,
    check_non_finite_numbers_are_refused,
    check_hash_sensitivity,
    check_unsupported_feature_is_refused,
)


class FeatureProviderContract:
    """pytest 复用入口：子类以 `Test*` 命名并提供 `feature_subject` fixture。"""

    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        raise NotImplementedError("子类必须提供 feature_subject fixture")

    @pytest.mark.parametrize("check", FEATURE_CHECKS, ids=lambda check: check.__name__)
    def test_feature_contract(self, feature_subject: FeatureSubject, check: FeatureCheck) -> None:
        check(feature_subject)
