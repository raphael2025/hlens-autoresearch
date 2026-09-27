"""`StateProvider` 的 provider-agnostic contract suite（core/contracts/state.py；ADR-0035 §4）。

实现方提供 `StateSubject`：

- `open`：每次调用返回一个**新** Provider 实例（模拟重启）；
- `spec`：被测的 `StateSpec`，Provider 必须在 descriptor 中声明支持它；
- `inputs` / `evaluation_times`：一份请求夹具。它必须让检查不落空：至少一个评估时刻之后还有输入、
  至少一个评估时刻给出非空状态；规格带 `training_window` 时，至少一个评估时刻在窗口之外还有
  更早的输入；
- `perturb`：返回同 feature、同时刻、值不同的输入；把它施加于全部输入必须改变至少一个状态
  （注意：分位分桶对单调变换不变，扰动应改变排序，例如取负）。

检查只看可观察行为：descriptor、`compute` 的结果、以及对请求的扰动 / 截断 / 变体下结果如何变化。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.state import (
    StateInput,
    StateProvider,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
    UnsupportedState,
)
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import StateSpec
from tests.contract_suites._support import (
    ContractSuiteFailure,
    call_ok,
    expect_error,
    require,
    revalidated,
)

__all__ = [
    "STATE_CHECKS",
    "StateCheck",
    "StateProviderContract",
    "StateSubject",
]


@dataclass(frozen=True)
class StateSubject:
    """被测 `StateProvider` 的接入点（见模块文档）。"""

    open: Callable[[], StateProvider]
    spec: StateSpec
    inputs: tuple[StateInput, ...]
    evaluation_times: tuple[datetime, ...]
    perturb: Callable[[StateInput], StateInput]


type StateCheck = Callable[[StateSubject], None]


# ======================================================================================
# 工具
# ======================================================================================


def _request(
    subject: StateSubject,
    *,
    inputs: Sequence[StateInput] | None = None,
    evaluation_times: Sequence[datetime] | None = None,
    **overrides: Any,
) -> StateRequest:
    fields: dict[str, Any] = {
        "state": subject.spec.ref,
        "spec_hash": subject.spec.content_hash(),
        "evaluation_times": tuple(
            subject.evaluation_times if evaluation_times is None else evaluation_times
        ),
        "inputs": tuple(subject.inputs if inputs is None else inputs),
    }
    fields.update(overrides)
    return call_ok("构造 StateRequest（夹具）", lambda: StateRequest(**fields))


def _descriptor(provider: StateProvider) -> StateProviderDescriptor:
    raw = call_ok("descriptor", lambda: provider.descriptor)
    return revalidated(StateProviderDescriptor, raw, "descriptor")


def _compute(subject: StateSubject, provider: StateProvider, request: StateRequest) -> StateResult:
    """执行一次 compute；结果必须恰好是合规的 `StateResult` 并如实回答该请求。"""
    raw = call_ok("compute", partial(provider.compute, request))
    result = revalidated(StateResult, raw, "compute 的结果")
    try:
        result.check_answers(request, _descriptor(provider), subject.spec)
    except ValueError as exc:
        raise ContractSuiteFailure(f"compute 的结果没有如实回答请求：{exc}") from exc
    return result


def _value_at(result: StateResult, at: datetime) -> StateValue:
    [value] = [item for item in result.values if item.evaluation_time == at]
    return value


def _perturbed(
    subject: StateSubject, which: Callable[[StateInput], bool]
) -> tuple[StateInput, ...]:
    out = []
    for item in subject.inputs:
        if which(item):
            changed = subject.perturb(item)
            require(
                (changed.feature, changed.evaluation_time) == (item.feature, item.evaluation_time),
                "subject.perturb 只能改变输入的值，不能改变 feature 或时刻",
            )
            out.append(changed)
        else:
            out.append(item)
    return tuple(out)


# ======================================================================================
# 检查
# ======================================================================================


def check_descriptor_declares_the_spec(subject: StateSubject) -> None:
    """descriptor 声明确定性并绑定被测规格的内容哈希；实例生命周期内与跨实例都不变。"""
    provider = subject.open()
    descriptor = _descriptor(provider)
    require(descriptor.deterministic is True, "StateProvider 必须声明 deterministic=True")
    require(
        descriptor.supports(subject.spec.ref, subject.spec.content_hash()),
        f"descriptor 必须声明支持 {subject.spec.ref} 及其 spec hash",
    )
    _compute(subject, provider, _request(subject))
    require(_descriptor(provider) == descriptor, "descriptor 在实例生命周期内不得改变")
    require(_descriptor(subject.open()) == descriptor, "同一实现的新实例 descriptor 必须相同")


def check_trained_spec_fixes_window_and_seed(subject: StateSubject) -> None:
    """训练型规格（`training_window` 非空）必须固定 `seed`（roadmap Phase 2 验收）。"""
    spec = subject.spec
    if spec.training_window is not None:
        require(spec.seed is not None, f"{spec.ref} 是训练型规格却没有固定 seed")


def check_values_are_one_to_one_with_evaluation_times(subject: StateSubject) -> None:
    """每个评估时刻恰好一个状态，且与请求中其它评估时刻无关（单独请求得到同一状态）。"""
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
            f"{at.isoformat()} 的状态不得依赖请求中的其它评估时刻",
        )


def check_labels_belong_to_the_state_space(subject: StateSubject) -> None:
    """非空状态都属于 `state_space`；夹具至少给出一个非空状态（否则检查没有对象）。"""
    result = _compute(subject, subject.open(), _request(subject))
    labels = [item.state for item in result.values if item.state is not None]
    require(bool(labels), "夹具必须至少有一个评估时刻给出非空状态")
    stray = sorted(set(labels) - set(subject.spec.state_space))
    require(not stray, f"状态 {stray} 不属于 state_space")


def check_determinism(subject: StateSubject) -> None:
    """同一请求（同一实例重复、重建实例、输入顺序不同）→ 同一结果与同一 result_hash。"""
    request = _request(subject)
    provider = subject.open()
    first = _compute(subject, provider, request)
    shuffled = _request(subject, inputs=tuple(reversed(subject.inputs)))
    require(shuffled == request, "StateRequest 必须把输入规范排序（夹具自检）")
    for label, again in (
        ("同一实例重复", _compute(subject, provider, request)),
        ("重建实例", _compute(subject, subject.open(), request)),
        ("输入顺序不同", _compute(subject, subject.open(), shuffled)),
    ):
        require(again.result_hash == first.result_hash, f"{label}：result_hash 必须相同")
        require(again == first, f"{label}：结果必须相同")


def check_causal_perturbation(subject: StateSubject) -> None:
    """改变 / 删去 t 之后的输入，t 的状态不变（防御执行器之外的直接调用）。"""
    provider = subject.open()
    base = _compute(subject, provider, _request(subject))
    everything = _compute(
        subject, provider, _request(subject, inputs=_perturbed(subject, lambda _: True))
    )
    require(
        everything.values != base.values,
        "subject.perturb 施加于全部输入必须改变至少一个状态（否则扰动检查没有牙齿）",
    )
    exercised = False
    for at in subject.evaluation_times:
        if not any(item.evaluation_time > at for item in subject.inputs):
            continue
        exercised = True

        def after_t(item: StateInput, at: datetime = at) -> bool:
            return item.evaluation_time > at

        changed = _compute(
            subject, provider, _request(subject, inputs=_perturbed(subject, after_t))
        )
        require(
            _value_at(changed, at) == _value_at(base, at),
            f"{at.isoformat()} 的状态受到了之后的输入影响（扰动）",
        )
        past = [item for item in subject.inputs if item.evaluation_time <= at]
        cut = _compute(subject, provider, _request(subject, inputs=past))
        require(
            _value_at(cut, at) == _value_at(base, at),
            f"{at.isoformat()} 的状态受到了之后的输入影响（截断）",
        )
    require(exercised, "夹具必须至少有一个评估时刻之后还有输入")


def check_training_window_is_respected(subject: StateSubject) -> None:
    """训练型规格：`evaluation_time <= t - training_window` 的输入不得影响 t 的状态。"""
    window = subject.spec.training_window
    if window is None:
        return
    provider = subject.open()
    base = _compute(subject, provider, _request(subject))
    exercised = False
    for at in subject.evaluation_times:

        def too_old(item: StateInput, at: datetime = at) -> bool:
            return item.evaluation_time <= at - window

        if not any(too_old(item) for item in subject.inputs):
            continue
        exercised = True
        changed = _compute(
            subject, provider, _request(subject, inputs=_perturbed(subject, too_old))
        )
        require(
            _value_at(changed, at) == _value_at(base, at),
            f"{at.isoformat()} 的状态受到了训练窗口之外的输入影响（扰动）",
        )
        kept = [item for item in subject.inputs if not too_old(item)]
        cut = _compute(subject, provider, _request(subject, inputs=kept))
        require(
            _value_at(cut, at) == _value_at(base, at),
            f"{at.isoformat()} 的状态受到了训练窗口之外的输入影响（截断）",
        )
    require(exercised, "夹具必须至少有一个评估时刻在训练窗口之外还有更早的输入")


def check_outcome_inputs_are_refused_at_construction(subject: StateSubject) -> None:
    """非 Feature 引用（尤其是 Outcome）不能成为 StateInput；非 state 引用不能成为请求。"""
    sample = subject.inputs[0]
    for kind in (Kind.OUTCOME, Kind.STATE, Kind.EVENT):
        payload = sample.model_dump()
        payload["feature"] = Ref(kind=kind, name="leak", version="1.0.0")
        expect_error(
            ValidationError,
            f"{kind.value} 作为 StateInput",
            partial(StateInput.model_validate, payload),
        )
    expect_error(
        ValidationError,
        "非 state 引用的请求",
        lambda: StateRequest(
            state=Ref(kind=Kind.OUTCOME, name="leak", version="1.0.0"),
            spec_hash=subject.spec.content_hash(),
            evaluation_times=subject.evaluation_times,
            inputs=subject.inputs,
        ),
    )


def check_not_computable_is_explicit_none(subject: StateSubject) -> None:
    """没有任何可见输入时状态显式为 None（不填补），且不报告任何输入。"""
    result = _compute(subject, subject.open(), _request(subject, inputs=()))
    for item in result.values:
        require(item.state is None, f"无输入时状态必须显式为 None，实际 {item.state!r}")
        require(item.inputs_used == 0, "无输入时 inputs_used 必须为 0")


def check_hash_sensitivity(subject: StateSubject) -> None:
    """请求的任一输入变化 → request_hash 与 result_hash 都变化（同一实例，防缓存旧结果）。"""
    provider = subject.open()
    base = _compute(subject, provider, _request(subject))
    last = subject.evaluation_times[-1]
    target = subject.inputs[len(subject.inputs) // 2]
    variants: dict[str, StateRequest] = {
        "少一个评估时刻": _request(subject, evaluation_times=subject.evaluation_times[:-1]),
        "多一个评估时刻": _request(
            subject, evaluation_times=(*subject.evaluation_times, last + timedelta(minutes=1))
        ),
        "一个输入值变化": _request(
            subject, inputs=_perturbed(subject, lambda item: item == target)
        ),
        "少一个输入": _request(subject, inputs=[item for item in subject.inputs if item != target]),
        "输入来源变化": _request(
            subject,
            inputs=[
                item.model_copy(
                    update={"source_result_hash": content_hash({"other": item.source_result_hash})}
                )
                if item == target
                else item
                for item in subject.inputs
            ],
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


def check_unsupported_state_is_refused(subject: StateSubject) -> None:
    """未声明的 state 版本或 spec hash → `UnsupportedState`，不得"尽量计算"。"""
    provider = subject.open()
    descriptor = _descriptor(provider)
    spec = subject.spec
    other_hash = content_hash({"not": spec.content_hash()})
    major = 999_999
    while descriptor.supports(
        Ref(kind=Kind.STATE, name=spec.name, version=f"{major}.0.0"), spec.content_hash()
    ):
        major += 1
    for label, request in (
        ("未声明的 spec hash", _request(subject, spec_hash=other_hash)),
        (
            "未声明的版本",
            _request(subject, state=Ref(kind=Kind.STATE, name=spec.name, version=f"{major}.0.0")),
        ),
    ):
        expect_error(UnsupportedState, f"compute({label})", partial(provider.compute, request))


STATE_CHECKS: tuple[StateCheck, ...] = (
    check_descriptor_declares_the_spec,
    check_trained_spec_fixes_window_and_seed,
    check_values_are_one_to_one_with_evaluation_times,
    check_labels_belong_to_the_state_space,
    check_determinism,
    check_causal_perturbation,
    check_training_window_is_respected,
    check_outcome_inputs_are_refused_at_construction,
    check_not_computable_is_explicit_none,
    check_hash_sensitivity,
    check_unsupported_state_is_refused,
)


class StateProviderContract:
    """pytest 复用入口：子类以 `Test*` 命名并提供 `state_subject` fixture。"""

    @pytest.fixture
    def state_subject(self) -> StateSubject:
        raise NotImplementedError("子类必须提供 state_subject fixture")

    @pytest.mark.parametrize("check", STATE_CHECKS, ids=lambda check: check.__name__)
    def test_state_contract(self, state_subject: StateSubject, check: StateCheck) -> None:
        check(state_subject)
