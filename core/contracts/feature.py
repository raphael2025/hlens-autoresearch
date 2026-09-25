"""`FeatureProvider`：确定性特征计算的 Protocol 与 DTO（ADR-0030；Phase 1 F4）。

对应 docs/architecture/05-plugin.md §3 与 03-data.md §4.5。本模块只定义可执行 Protocol、可序列化
DTO 与它们在契约层可证明的不变量；**不含任何 Provider 实现或执行器**（执行器与首批 Provider 属 F4
实现批次，分别在 `infrastructure/feature/` 与 `plugins/features/`）。

| 成员 | 输入 → 输出 |
|---|---|
| `descriptor` | → `ProviderDescriptor`（`name@version`、确定性声明、支持的 feature 与 spec hash） |
| `compute` | `FeatureRequest` → `FeatureResult` |

**可见集合**（ADR-0030 §1）：评估时刻 `t` 的值只能是 `FeatureRequest.visible_at(t, available_lag)`
的函数——`available_time + available_lag <= t` 的观察；同一 `observation_key` 有多条观察时只取
`available_time` 最大的一条（PIT 替换，不是追加；由执行器从无冲突的 PIT 选择构造时，它恰好是
`t - available_lag` 时刻被选中的 revision）。`knowledge_time <= knowledge_cutoff` 由请求构造保证。
`available_lag` 是 `FeatureSpec.available_lag`：Provider 通过请求的 `feature` + `spec_hash` 绑定到它
所声明的规格；执行器另行**结构性截断**，每次只把可见集合交给 Provider（泄漏不依赖 Provider 自律）。

**不可计算**：历史不足（或 Provider 规格定义的其它"无法给出值"的情形）时 `value` 显式为 `None`，
不得填补为 0 或其它值；输入本身不合规（缺字段、类型不符）则以 `FeatureInputError` fail closed。

**数值**：观察与结果中的数值一律为 `Decimal` / `int` / `bool`；浮点数在 Python 与 JSON 两条入口都
被拒绝，NaN / ±Infinity 同样被拒绝（ADR-0013）。观察值中的字符串若可解析为有限十进制数，必须以
`Decimal` 传入（否则 JSON 往返后类型会改变，内容哈希不再唯一）。

**诚实边界**：DTO 只证明形状与请求 / 结果之间可局部检查的关系（一一对应、`request_hash`、
`result_hash` 自洽、`latest_input_available_time <= evaluation_time`）。带 `available_lag` 的完整
lag 不变量、值是否只依赖可见集合、确定性，由 `FeatureResult.check_answers`（执行器与 contract suite
共用）与 `tests/contract_suites/feature.py` 对具体实现检查。`manifest_content_hash` 是否真的对应一份
已登记的 `ResearchDatasetManifest`、观察是否真的来自其绑定的 snapshot，属 F3 / Registry。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Annotated, Literal, Protocol

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator, model_validator

from core.contracts.universe import SelectedRevisionLineage
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
    content_hash,
)

__all__ = [
    "FEATURE_REF_KEY_PATTERN",
    "FeatureInputError",
    "FeatureObservation",
    "FeatureProvider",
    "FeatureProviderError",
    "FeatureRequest",
    "FeatureResult",
    "FeatureValue",
    "ProviderDescriptor",
    "UnsupportedFeature",
]

#: `ProviderDescriptor.supported_features` 的键：`feature:name@semver`（与 `Ref` 规范串同一语法）。
FEATURE_REF_KEY_PATTERN = rf"^feature:{PLUGIN_KEY_PATTERN.removeprefix('^')}"

NonEmptyStr = Annotated[str, Field(min_length=1)]
ValueName = Annotated[str, Field(pattern=NAME_PATTERN)]
FeatureRefKey = Annotated[str, Field(pattern=FEATURE_REF_KEY_PATTERN)]


class FeatureProviderError(Exception):
    """FeatureProvider 契约错误的基类。"""


class UnsupportedFeature(FeatureProviderError):
    """请求的 feature `name@version` 或 `spec_hash` 不由该 Provider 声明支持。"""


class FeatureInputError(FeatureProviderError):
    """输入观察不符合该 feature 的输入约定（缺字段、类型不符、非法价格等）；fail closed。"""


def _refuse_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError(
            "不接受浮点数：数值一律以 Decimal 或 int 传入，二进制浮点不得进入内容哈希（ADR-0013）"
        )
    return value


def _finite_decimal(text: str) -> Decimal | None:
    try:
        parsed = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _non_finite_text(text: str) -> bool:
    """文本能被解析为 NaN / ±Infinity（F4-R1：不得以字符串形式绕过有限数规则）。"""
    try:
        return not Decimal(text.strip()).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _observation_scalar(value: object, info: ValidationInfo) -> object:
    """观察值：拒绝浮点；可解析为有限十进制数的文本只能是 `Decimal`。

    Python 入口：这样的字符串被拒绝（必须以 `Decimal` 传入）。JSON 入口：`Decimal` 的 wire 形式
    就是十进制字符串，因此这样的字符串还原为 `Decimal`；其它字符串保持为字符串。两条入口因此
    对同一载荷给出同一类型，JSON 往返不改变内容哈希。
    """
    _refuse_float(value)
    if isinstance(value, str):
        parsed = _finite_decimal(value)
        if parsed is not None:
            if info.mode == "json":
                return parsed
            raise ValueError(f"数值文本 {value!r} 必须以 Decimal 传入，不得作为字符串值")
        if _non_finite_text(value):
            raise ValueError(f"非有限数值文本 {value!r} 不得作为观察值（ADR-0013）")
    return value


def _feature_scalar(value: object, info: ValidationInfo) -> object:
    """特征值：拒绝浮点；JSON 入口的十进制字符串是 `Decimal` 的 wire 形式，按 `Decimal` 还原。"""
    _refuse_float(value)
    if info.mode == "json" and isinstance(value, str):
        parsed = _finite_decimal(value)
        if parsed is not None:
            return parsed
    return value


ObservationScalar = Annotated[Decimal | int | bool | str, BeforeValidator(_observation_scalar)]
FeatureScalar = Annotated[Decimal | int | bool, BeforeValidator(_feature_scalar)]


def _observation_order(item: FeatureObservation) -> tuple[datetime, str]:
    return (item.available_time, item.observation_key)


def _require_lag(available_lag: timedelta) -> None:
    if not isinstance(available_lag, timedelta) or available_lag < timedelta(0):
        raise ValueError("available_lag 必须是非负 timedelta")


class FeatureObservation(Contract):
    """交给 Provider 的一条输入观察：一个 PIT 选中的 Canonical revision（或其确定性派生）。

    - `observation_key`：业务观察的稳定键；`event_time` / `event_end_time`：事件时刻或半开区间
      `[event_time, event_end_time)`（区间型数据，例如 bar）；
    - `available_time`（历史轴）不早于观察本身可被观察的时刻（区间取结束端）；`knowledge_time`
      （知识轴）由请求的 `knowledge_cutoff` 约束；
    - `values`：市场内容，键为小写标识符，值为 `Decimal | int | bool | str`（数值规则见模块文档）；
    - `lineage`：Canonical revision → Raw row → Raw source 的来源链。
    """

    observation_key: NonEmptyStr
    event_time: UtcDatetime
    event_end_time: UtcDatetime | None = None
    available_time: UtcDatetime
    knowledge_time: UtcDatetime
    values: FrozenMapping[ValueName, ObservationScalar]
    lineage: SelectedRevisionLineage

    @property
    def observable_time(self) -> datetime:
        """观察本身可被观察的最早时刻：区间取结束端，瞬时事件取 `event_time`。"""
        return self.event_end_time if self.event_end_time is not None else self.event_time

    @model_validator(mode="after")
    def _time_invariants(self) -> FeatureObservation:
        if self.event_end_time is not None and self.event_end_time <= self.event_time:
            raise ValueError("event_end_time 必须晚于 event_time（半开区间不得为空）")
        if self.available_time < self.observable_time:
            raise ValueError("available_time 不得早于观察本身可被观察的时刻（区间取结束端）")
        return self


class FeatureRequest(Contract):
    """一次特征计算请求。

    - `feature` 必须是 `kind=feature` 的引用，`spec_hash` 为该 `FeatureSpec` 的内容哈希；
    - `manifest_content_hash`：输入所属 Research Dataset manifest 的内容哈希；
    - `evaluation_times`：非空、严格升序（因而唯一）；
    - `observations`：按 `(available_time, observation_key)` 规范排序；该二元组重复即拒绝（否则规范
      顺序与可见集合都不唯一）；每条观察 `knowledge_time <= knowledge_cutoff`，否则构造即拒绝。

    同一请求内容 → 同一 `content_hash()`（输入顺序不影响）。
    """

    feature: Ref
    spec_hash: ContentHash
    manifest_content_hash: ContentHash
    knowledge_cutoff: UtcDatetime
    evaluation_times: tuple[UtcDatetime, ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    observations: tuple[FeatureObservation, ...]

    @field_validator("evaluation_times")
    @classmethod
    def _ascending(cls, value: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(later <= earlier for earlier, later in pairwise(value)):
            raise ValueError("evaluation_times 必须严格升序（因而唯一）")
        return value

    @field_validator("observations")
    @classmethod
    def _canonical_observations(
        cls, value: tuple[FeatureObservation, ...]
    ) -> tuple[FeatureObservation, ...]:
        ordered = tuple(sorted(value, key=_observation_order))
        for earlier, later in pairwise(ordered):
            if _observation_order(earlier) == _observation_order(later):
                raise ValueError(
                    f"observations 中 (available_time, observation_key) 重复："
                    f"{later.observation_key!r} @ {later.available_time.isoformat()}"
                )
        return ordered

    @model_validator(mode="after")
    def _request_invariants(self) -> FeatureRequest:
        if self.feature.kind is not Kind.FEATURE:
            raise ValueError(f"feature 必须是 kind=feature 的引用，收到 {self.feature}")
        for item in self.observations:
            if item.knowledge_time > self.knowledge_cutoff:
                raise ValueError(
                    f"观察 {item.observation_key!r} 的 knowledge_time 晚于 knowledge_cutoff"
                )
        return self

    def visible_at(
        self, evaluation_time: datetime, available_lag: timedelta
    ) -> tuple[FeatureObservation, ...]:
        """`evaluation_time` 的可见集合（见模块文档），按规范顺序返回。"""
        _require_lag(available_lag)
        if not isinstance(evaluation_time, datetime) or evaluation_time.tzinfo is None:
            raise ValueError("evaluation_time 必须是带时区的 UTC 时间")
        latest: dict[str, FeatureObservation] = {}
        for item in self.observations:  # 已按 available_time 升序
            if item.available_time + available_lag > evaluation_time:
                break
            latest[item.observation_key] = item
        return tuple(sorted(latest.values(), key=_observation_order))


class FeatureValue(Contract):
    """一个评估时刻的特征值。

    - `value`：必填；`None` 表示显式"不可计算"（历史不足等），不填补；
    - `inputs_used`：计算该值实际使用的观察条数；为 0 当且仅当 `latest_input_available_time` 为空，
      且此时 `value` 必须为 `None`（零输入的值只能是填补）；
    - `latest_input_available_time`：所用观察中最大的 `available_time`，不晚于 `evaluation_time`
      （带 `available_lag` 的完整约束由 `FeatureResult.check_answers` 检查）。
    """

    evaluation_time: UtcDatetime
    value: FeatureScalar | None
    inputs_used: int = Field(ge=0, strict=True)
    latest_input_available_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def _input_invariants(self) -> FeatureValue:
        if (self.inputs_used == 0) != (self.latest_input_available_time is None):
            raise ValueError("inputs_used 为 0 当且仅当 latest_input_available_time 为空")
        if self.inputs_used == 0 and self.value is not None:
            # 没有用到任何输入的值只能是填补（F4-R1）：显式"不可计算"，不填 0。
            raise ValueError("inputs_used 为 0 时 value 必须为 None（不填补）")
        if (
            self.latest_input_available_time is not None
            and self.latest_input_available_time > self.evaluation_time
        ):
            raise ValueError("latest_input_available_time 不得晚于 evaluation_time")
        return self


class ProviderDescriptor(Contract):
    """FeatureProvider 的身份与能力声明（05-plugin.md §1 / §3：FeatureProvider 必须确定性）。

    `supported_features`：非空，`feature:name@semver` → 该 `FeatureSpec` 的内容哈希。规格的参数
    （窗口长度等）属于规格内容，因而由哈希绑定。实例生命周期内不变。
    """

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    supported_features: FrozenMapping[FeatureRefKey, ContentHash]

    @field_validator("supported_features")
    @classmethod
    def _non_empty(cls, value: FrozenMapping[str, str]) -> FrozenMapping[str, str]:
        if not value:
            raise ValueError("supported_features 不得为空")
        return value

    @property
    def plugin_key(self) -> str:
        """`name@version`（与复现元组 `plugin_versions` 的键同一语法）。"""
        return f"{self.name}@{self.version}"

    def supports(self, feature: Ref, spec_hash: str) -> bool:
        """是否声明支持该 feature 版本的这一份规格内容。"""
        return self.supported_features.get(str(feature)) == spec_hash


def _result_hash(
    request_hash: str, provider: str, provider_hash: str, values: Iterable[FeatureValue]
) -> str:
    return content_hash(
        {
            "request_hash": request_hash,
            "provider": provider,
            "provider_hash": provider_hash,
            "values": [item.model_dump(mode="json") for item in values],
        }
    )


class FeatureResult(Contract):
    """一次 `compute` 的结果。

    - `request_hash`：请求的 `content_hash()`；`provider` / `provider_hash`：Provider 的
      `name@version` 与其 descriptor 的内容哈希；
    - `values`：非空，`evaluation_time` 严格升序；与请求的 `evaluation_times` 一一对应（由
      `check_answers` 对照请求检查）；
    - `result_hash`：以上内容的规范 JSON SHA-256，构造时复核（不接受自报的哈希）。

    同一请求 + 同一 Provider 版本 → 同一 `result_hash`（确定性由 contract suite 检查）。
    """

    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    values: tuple[FeatureValue, ...] = Field(min_length=1)
    result_hash: ContentHash

    @field_validator("values")
    @classmethod
    def _ascending(cls, value: tuple[FeatureValue, ...]) -> tuple[FeatureValue, ...]:
        times = [item.evaluation_time for item in value]
        if any(later <= earlier for earlier, later in pairwise(times)):
            raise ValueError("values 的 evaluation_time 必须严格升序（因而唯一）")
        return value

    @model_validator(mode="after")
    def _self_consistent_hash(self) -> FeatureResult:
        expected = _result_hash(self.request_hash, self.provider, self.provider_hash, self.values)
        if self.result_hash != expected:
            raise ValueError("result_hash 与结果内容不符")
        return self

    @classmethod
    def build(
        cls,
        request: FeatureRequest,
        descriptor: ProviderDescriptor,
        values: Iterable[FeatureValue],
    ) -> FeatureResult:
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
        request: FeatureRequest,
        descriptor: ProviderDescriptor,
        available_lag: timedelta,
    ) -> None:
        """对照请求检查本结果；不符抛 `ValueError`。

        `request_hash` 与 Provider 身份一致；值与 `evaluation_times` 一一对应；每个值的
        `latest_input_available_time` 是该时刻可见集合中某条观察的 `available_time`（因而
        `+ available_lag <= evaluation_time`），`inputs_used` 不超过可见集合的大小。
        """
        _require_lag(available_lag)
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
        for item in self.values:
            visible = request.visible_at(item.evaluation_time, available_lag)
            if item.inputs_used > len(visible):
                raise ValueError(
                    f"{item.evaluation_time.isoformat()} 的 inputs_used 超过可见观察条数"
                )
            latest = item.latest_input_available_time
            if latest is not None and latest not in {obs.available_time for obs in visible}:
                raise ValueError(
                    f"{item.evaluation_time.isoformat()} 的 latest_input_available_time 不属于"
                    "该时刻可见的观察（available_time + available_lag <= evaluation_time）"
                )


class FeatureProvider(Protocol):
    """确定性特征计算（ADR-0030）。语义见模块文档。"""

    @property
    def descriptor(self) -> ProviderDescriptor:
        """Provider 身份与支持的 feature 规格；实例生命周期内不变。"""
        ...

    def compute(self, request: FeatureRequest) -> FeatureResult:
        """计算请求中每个评估时刻的值；不支持的规格 → `UnsupportedFeature`。"""
        ...
