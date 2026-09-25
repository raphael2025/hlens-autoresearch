"""`StateProvider`：确定性市场状态识别的 Protocol 与 DTO（ADR-0035；Phase 2）。

对应 docs/architecture/05-plugin.md §3（StateProvider：Features → State series；训练型需固定种子与
训练窗口）。本模块只定义可执行 Protocol、可序列化 DTO 与它们在契约层可证明的不变量；**不含任何
Provider 实现或执行器**（执行器在 `infrastructure/state/`，首批 Provider 在 `plugins/states/`）。

| 成员 | 输入 → 输出 |
|---|---|
| `descriptor` | → `StateProviderDescriptor`（`name@version`、确定性、支持的 state 与 spec hash） |
| `compute` | `StateRequest` → `StateResult` |

**输入只能是 Feature 值**：`StateInput.feature` 必须是 `kind=feature` 的引用（与
`StateSpec.features` 的白名单一致，ADR-0012 §D-23.2）；Outcome 在构造时即被拒绝，永远到不了
Provider（Constitution C-L2）。
每个 `StateInput` 是某个 `FeatureResult` 在一个评估时刻给出的值，`source_result_hash` 绑定该结果。

**可见集合**（ADR-0035 §1）：评估时刻 `t` 的状态只能是 `StateRequest.visible_at(t, training_window)`
的函数——`evaluation_time <= t` 的 Feature 值（Feature 值在其评估时刻已由 Feature 执行器保证只用
`available_time + available_lag <= evaluation_time` 的观察）；`StateSpec.training_window` 非空时，
再只保留 `evaluation_time > t - training_window` 的值（固定的尾随训练窗口；不可能全样本拟合）。
执行器对每个评估时刻**结构性截断**，只把可见集合交给 Provider（泄漏不依赖 Provider 自律）。

**不可计算**：历史不足（或 Provider 规格定义的其它"无法给出状态"的情形）时 `state` 显式为 `None`，
不得填补为某个默认状态；输入不合规（值类型不符、feature 不属于规格）则以 `StateInputError`
fail closed。

**参数**：`StateSpec` 没有 `params` 字段（已发布契约不改，ADR-0035 §3）。状态模型的参数（分位切点、
最少历史、阈值……）以规范形式编码进 `StateSpec.method`：`<method_name>:<canonical JSON params>`，
由 `state_method` / `parse_state_method` 读写，因此被 spec hash 绑定。参数是规格参数，不是验证阈值。

**诚实边界**：DTO 只证明形状与请求 / 结果之间可局部检查的关系（一一对应、`request_hash`、
`result_hash` 自洽、标签属于 `state_space`、`latest_input_time` 是某个可见输入的评估时刻）。值是否只
依赖可见集合、确定性，由 `StateResult.check_answers`（执行器与 contract suite 共用）与
`tests/contract_suites/state.py` 对具体实现检查。`source_result_hash` 是否真的对应一份已运行的
`FeatureResult`，由构造输入的执行器（`infrastructure.state.state_inputs`）检查，契约层不能证明。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from core.contracts.feature import FeatureScalar
from core.domain.base import (
    NAME_PATTERN,
    PLUGIN_KEY_PATTERN,
    SEMVER_PATTERN,
    ContentHash,
    Contract,
    FrozenMapping,
    Kind,
    PluginKey,
    Ref,
    UtcDatetime,
    canonical_json,
    content_hash,
)
from core.domain.specs import StateSpec

__all__ = [
    "STATE_REF_KEY_PATTERN",
    "MethodParam",
    "StateInput",
    "StateInputError",
    "StateProvider",
    "StateProviderDescriptor",
    "StateProviderError",
    "StateRequest",
    "StateResult",
    "StateValue",
    "UnsupportedState",
    "parse_state_method",
    "state_method",
]

#: `StateProviderDescriptor.supported_states` 的键：`state:name@semver`（与 `Ref` 规范串同一语法）。
STATE_REF_KEY_PATTERN = rf"^state:{PLUGIN_KEY_PATTERN.removeprefix('^')}"

StateRefKey = Annotated[str, Field(pattern=STATE_REF_KEY_PATTERN)]
StateLabel = Annotated[str, Field(min_length=1)]
MethodParam = str | int | bool


class StateProviderError(Exception):
    """StateProvider 契约错误的基类。"""


class UnsupportedState(StateProviderError):
    """请求的 state `name@version` 或 `spec_hash` 不由该 Provider 声明支持。"""


class StateInputError(StateProviderError):
    """输入 Feature 值不符合该状态模型的输入约定（类型不符、feature 不属于规格等）；fail closed。"""


# ======================================================================================
# method 编码（ADR-0035 §3）
# ======================================================================================


def state_method(name: str, params: Mapping[str, MethodParam]) -> str:
    """`<name>:<canonical JSON params>`；参数值只接受 `str` / `int` / `bool`（小数用字符串）。"""
    if not isinstance(name, str) or not name or ":" in name:
        raise ValueError(f"method 名必须是不含 ':' 的非空字符串：{name!r}")
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"method 参数名必须是非空字符串：{key!r}")
        if not isinstance(value, str | int | bool) or isinstance(value, float):
            raise ValueError(f"method 参数 {key!r} 只接受 str / int / bool，收到 {value!r}")
    return f"{name}:{canonical_json(dict(params))}"


def parse_state_method(method: str) -> tuple[str, dict[str, MethodParam]]:
    """`state_method` 的逆：返回 `(name, params)`；不是规范编码则 `ValueError`。"""
    name, sep, raw = method.partition(":")
    if not sep:
        raise ValueError(f"method 不是 <name>:<params> 编码：{method!r}")
    try:
        params = json.loads(raw, parse_float=_refuse_float_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"method 参数不是 JSON：{exc}") from None
    if not isinstance(params, dict):
        raise ValueError("method 参数必须是 JSON 对象")
    if state_method(name, params) != method:
        raise ValueError(f"method 不是规范编码（键序 / 空白 / 类型）：{method!r}")
    return name, params


def _refuse_float_text(text: str) -> object:
    raise ValueError(f"method 参数不接受浮点数 {text!r}（十进制数以字符串传入）")


# ======================================================================================
# DTO
# ======================================================================================


def _input_order(item: StateInput) -> tuple[datetime, str]:
    return (item.evaluation_time, str(item.feature))


def _require_window(training_window: timedelta | None) -> None:
    if training_window is not None and (
        not isinstance(training_window, timedelta) or training_window <= timedelta(0)
    ):
        raise ValueError("training_window 若提供则必须是正 timedelta")


class StateInput(Contract):
    """交给 Provider 的一个 Feature 值：`feature` 在 `evaluation_time` 的值（`None` = 不可计算）。

    `source_result_hash` 是给出该值的 `FeatureResult.result_hash`（来源绑定）。
    """

    feature: Ref
    evaluation_time: UtcDatetime
    value: FeatureScalar | None
    source_result_hash: ContentHash

    @model_validator(mode="after")
    def _feature_only(self) -> StateInput:
        if self.feature.kind is not Kind.FEATURE:
            raise ValueError(
                f"StateInput.feature 必须是 kind=feature 的引用，收到 {self.feature}"
                "（Outcome 永不作为输入，Constitution C-L2）"
            )
        return self


class StateRequest(Contract):
    """一次状态识别请求。

    - `state` 必须是 `kind=state` 的引用，`spec_hash` 为该 `StateSpec` 的内容哈希；
    - `evaluation_times`：非空、严格升序（因而唯一）；
    - `inputs`：按 `(evaluation_time, feature)` 规范排序；该二元组重复即拒绝（同一 feature 同一时刻
      只能有一个值）。

    同一请求内容 → 同一 `content_hash()`（输入顺序不影响）。
    """

    state: Ref
    spec_hash: ContentHash
    evaluation_times: tuple[UtcDatetime, ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    inputs: tuple[StateInput, ...]

    @field_validator("evaluation_times")
    @classmethod
    def _ascending(cls, value: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(later <= earlier for earlier, later in pairwise(value)):
            raise ValueError("evaluation_times 必须严格升序（因而唯一）")
        return value

    @field_validator("inputs")
    @classmethod
    def _canonical_inputs(cls, value: tuple[StateInput, ...]) -> tuple[StateInput, ...]:
        ordered = tuple(sorted(value, key=_input_order))
        for earlier, later in pairwise(ordered):
            if _input_order(earlier) == _input_order(later):
                raise ValueError(
                    f"inputs 中 (evaluation_time, feature) 重复：{later.feature} @ "
                    f"{later.evaluation_time.isoformat()}"
                )
        return ordered

    @model_validator(mode="after")
    def _request_invariants(self) -> StateRequest:
        if self.state.kind is not Kind.STATE:
            raise ValueError(f"state 必须是 kind=state 的引用，收到 {self.state}")
        return self

    def visible_at(
        self, evaluation_time: datetime, training_window: timedelta | None
    ) -> tuple[StateInput, ...]:
        """`evaluation_time` 的可见集合（见模块文档），按规范顺序返回。"""
        _require_window(training_window)
        if not isinstance(evaluation_time, datetime) or evaluation_time.tzinfo is None:
            raise ValueError("evaluation_time 必须是带时区的 UTC 时间")
        start = None if training_window is None else evaluation_time - training_window
        return tuple(
            item
            for item in self.inputs
            if item.evaluation_time <= evaluation_time
            and (start is None or item.evaluation_time > start)
        )


class StateValue(Contract):
    """一个评估时刻的状态。

    - `state`：必填；`None` 表示显式"不可计算"（历史不足等），不填补；
    - `inputs_used`：识别该状态实际使用的 Feature 值个数；为 0 当且仅当 `latest_input_time` 为空，
      且此时 `state` 必须为 `None`；
    - `latest_input_time`：所用 Feature 值中最大的 `evaluation_time`，不晚于 `evaluation_time`。
    """

    evaluation_time: UtcDatetime
    state: StateLabel | None
    inputs_used: int = Field(ge=0, strict=True)
    latest_input_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def _input_invariants(self) -> StateValue:
        if (self.inputs_used == 0) != (self.latest_input_time is None):
            raise ValueError("inputs_used 为 0 当且仅当 latest_input_time 为空")
        if self.inputs_used == 0 and self.state is not None:
            raise ValueError("inputs_used 为 0 时 state 必须为 None（不填补）")
        if self.latest_input_time is not None and self.latest_input_time > self.evaluation_time:
            raise ValueError("latest_input_time 不得晚于 evaluation_time")
        return self


class StateProviderDescriptor(Contract):
    """StateProvider 的身份与能力声明（05-plugin.md §3：StateProvider 必须确定性）。

    `supported_states`：非空，`state:name@semver` → 该 `StateSpec` 的内容哈希（训练窗口、种子与
    method 参数都属规格内容，因而由哈希绑定）。实例生命周期内不变。训练型模型的随机性只能来自
    规格中的 `seed`，因此同样声明 `deterministic=True`。
    """

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    supported_states: FrozenMapping[StateRefKey, ContentHash]

    @field_validator("supported_states")
    @classmethod
    def _non_empty(cls, value: FrozenMapping[str, str]) -> FrozenMapping[str, str]:
        if not value:
            raise ValueError("supported_states 不得为空")
        return value

    @property
    def plugin_key(self) -> str:
        """`name@version`（与复现元组 `plugin_versions` 的键同一语法）。"""
        return f"{self.name}@{self.version}"

    def supports(self, state: Ref, spec_hash: str) -> bool:
        """是否声明支持该 state 版本的这一份规格内容。"""
        return self.supported_states.get(str(state)) == spec_hash


def _result_hash(
    request_hash: str, provider: str, provider_hash: str, values: Iterable[StateValue]
) -> str:
    return content_hash(
        {
            "request_hash": request_hash,
            "provider": provider,
            "provider_hash": provider_hash,
            "values": [item.model_dump(mode="json") for item in values],
        }
    )


class StateResult(Contract):
    """一次 `compute` 的结果（即一份 State 序列）。

    - `request_hash`：请求的 `content_hash()`；`provider` / `provider_hash`：Provider 的
      `name@version` 与其 descriptor 的内容哈希；
    - `values`：非空，`evaluation_time` 严格升序；与请求的 `evaluation_times` 一一对应（由
      `check_answers` 对照请求检查）；
    - `result_hash`：以上内容的规范 JSON SHA-256，构造时复核。
    """

    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    values: tuple[StateValue, ...] = Field(min_length=1)
    result_hash: ContentHash

    @field_validator("values")
    @classmethod
    def _ascending(cls, value: tuple[StateValue, ...]) -> tuple[StateValue, ...]:
        times = [item.evaluation_time for item in value]
        if any(later <= earlier for earlier, later in pairwise(times)):
            raise ValueError("values 的 evaluation_time 必须严格升序（因而唯一）")
        return value

    @model_validator(mode="after")
    def _self_consistent_hash(self) -> StateResult:
        expected = _result_hash(self.request_hash, self.provider, self.provider_hash, self.values)
        if self.result_hash != expected:
            raise ValueError("result_hash 与结果内容不符")
        return self

    @classmethod
    def build(
        cls,
        request: StateRequest,
        descriptor: StateProviderDescriptor,
        values: Iterable[StateValue],
    ) -> StateResult:
        """由请求、descriptor 与值构造结果（计算 request_hash、provider_hash 与 result_hash）。"""
        items = tuple(values)
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            values=items,
            result_hash=_result_hash(request_hash, descriptor.plugin_key, provider_hash, items),
        )

    def check_answers(
        self,
        request: StateRequest,
        descriptor: StateProviderDescriptor,
        spec: StateSpec,
    ) -> None:
        """对照请求与规格检查本结果；不符抛 `ValueError`。

        请求属于该规格；`request_hash` 与 Provider 身份一致；值与 `evaluation_times` 一一对应；
        每个非空状态属于 `spec.state_space`；`latest_input_time` 是该时刻可见集合（含训练窗口）中
        某个输入的 `evaluation_time`；`inputs_used` 不超过可见集合的大小。
        """
        if request.state != spec.ref or request.spec_hash != spec.content_hash():
            raise ValueError("请求不属于该 StateSpec")
        if self.request_hash != request.content_hash():
            raise ValueError("request_hash 与请求不符")
        if (self.provider, self.provider_hash) != (
            descriptor.plugin_key,
            descriptor.content_hash(),
        ):
            raise ValueError("provider / provider_hash 与 descriptor 不符")
        times = tuple(item.evaluation_time for item in self.values)
        if times != request.evaluation_times:
            raise ValueError("values 必须与请求的 evaluation_times 一一对应")
        labels = set(spec.state_space)
        for item in self.values:
            if item.state is not None and item.state not in labels:
                raise ValueError(
                    f"{item.evaluation_time.isoformat()} 的状态 {item.state!r} 不属于 state_space"
                )
            visible = request.visible_at(item.evaluation_time, spec.training_window)
            if item.inputs_used > len(visible):
                raise ValueError(
                    f"{item.evaluation_time.isoformat()} 的 inputs_used 超过可见输入个数"
                )
            latest = item.latest_input_time
            if latest is not None and latest not in {obs.evaluation_time for obs in visible}:
                raise ValueError(
                    f"{item.evaluation_time.isoformat()} 的 latest_input_time 不属于该时刻可见的"
                    "输入（evaluation_time <= t，且在训练窗口内）"
                )


class StateProvider(Protocol):
    """确定性市场状态识别（ADR-0035）。语义见模块文档。"""

    @property
    def descriptor(self) -> StateProviderDescriptor:
        """Provider 身份与支持的 state 规格；实例生命周期内不变。"""
        ...

    def compute(self, request: StateRequest) -> StateResult:
        """识别请求中每个评估时刻的状态；不支持的规格 → `UnsupportedState`。"""
        ...
