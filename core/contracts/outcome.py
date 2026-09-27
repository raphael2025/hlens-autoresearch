"""`OutcomeProvider`：结果标签计算的 Protocol 与 DTO（ADR-0037；Phase 4）。

对应 05-plugin.md §3（"Canonical + event times + horizon → Outcomes"）与 03-data.md §2：
Outcome 由 Canonical 价格在事件时刻**之后**的 horizon 内计算，**永不**回流为
Feature / State / Event / Strategy 的输入（Constitution C-L2）。本模块只定义可执行 Protocol、
可序列化 DTO 与契约层可证明的不变量；实现在 `plugins/outcomes/`，物化在 `research/outcomes/`。

| 成员 | 输入 → 输出 |
|---|---|
| `descriptor` | → `OutcomeProviderDescriptor`（`name@version`、确定性、支持的标签规格哈希） |
| `compute` | `OutcomeRequest` → `OutcomeResult` |

**标签规格**：`OutcomeSpec`（已冻结，只有 `horizon` 与自由文本 `label_definition`）没有参数槽位；
按"契约只追加"的规则，方法与参数（`method`、`horizon`、屏障）放在新增的 `OutcomeLabelSpec` 中，
它通过 `outcome` 引用 + `outcome_spec_hash` 绑定一份 `OutcomeSpec`，并由 `bind` 从该规格复制 horizon
（两处不可能不一致）。

**时间对齐**（每个事件）：
- `event_time` 是决策时刻（信号可被观察的时刻）；入场 bar = 第一根 `interval_start >= event_time`
  的 bar，且入场延迟必须短于该 bar 的长度（否则视为缺数据，标签为 `None`）；
- 标签窗口 = `[entry_time, entry_time + horizon]`；窗口内的 bar 必须首尾相接，
  缺口 → `None`，不填补；
- `OutcomeLabel.available_time` 是标签**可被知道**的时刻（所用 bar 的最大 `available_time`）：
  任何在它之前的时刻都不得使用该标签（`research/outcomes` 的 `known_as_of`）。

**只作标签的判别**：`OutcomeLabel` 与 `OutcomeResult` 带 `label_only: Literal[True]` 字段。
任何输入 DTO（`FeatureObservation` 等）都是 `extra="forbid"`，因此把 Outcome 载荷
（对象或 dump 出的字典）交给输入 DTO 必然被拒绝；`refuse_outcome_input` 是验证流水线
在运行时使用的同一判别。

**诚实边界**：契约层不能阻止有人把一个 Outcome 的**数值**抄进某个 `FeatureObservation.values`；
那属于信息流审计与 G1 泄漏门（负对照）。`manifest_content_hash` 是否对应已登记的 manifest
属 Registry。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal, Protocol

from pydantic import BeforeValidator, Field, field_validator, model_validator

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
from core.domain.specs import OutcomeSpec

__all__ = [
    "OUTCOME_MODELS",
    "OUTCOME_REF_KEY_PATTERN",
    "FiniteDecimal",
    "OutcomeEvent",
    "OutcomeInputError",
    "OutcomeLabel",
    "OutcomeLabelSpec",
    "OutcomeMethod",
    "OutcomePriceBar",
    "OutcomeProvider",
    "OutcomeProviderDescriptor",
    "OutcomeProviderError",
    "OutcomeRequest",
    "OutcomeResult",
    "OutcomeUsedAsInput",
    "UnsupportedOutcome",
    "is_outcome_payload",
    "refuse_outcome_input",
]

#: `OutcomeProviderDescriptor.supported_outcomes` 的键：`outcome:name@semver`。
OUTCOME_REF_KEY_PATTERN = rf"^outcome:{PLUGIN_KEY_PATTERN.removeprefix('^')}"

NonEmptyStr = Annotated[str, Field(min_length=1)]
OutcomeRefKey = Annotated[str, Field(pattern=OUTCOME_REF_KEY_PATTERN)]


class OutcomeProviderError(Exception):
    """OutcomeProvider 契约错误的基类。"""


class UnsupportedOutcome(OutcomeProviderError):
    """请求的标签规格不由该 Provider 声明支持。"""


class OutcomeInputError(OutcomeProviderError):
    """价格输入不符合约定（例如混入多个标的）；fail closed。"""


class OutcomeUsedAsInput(ValueError):
    """Outcome 载荷被当作计算输入（Constitution C-L2）。"""


def _refuse_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("不接受浮点数：数值一律以 Decimal 或 int 传入（ADR-0013）")
    return value


#: 有限的 `Decimal`：拒绝浮点入口；NaN / ±Infinity 由 `Contract.allow_inf_nan=False` 拒绝。
FiniteDecimal = Annotated[Decimal, BeforeValidator(_refuse_float)]


class OutcomeMethod(StrEnum):
    """首批标签方法（ADR-0037 §2）。"""

    FORWARD_RETURN = "forward_return"
    TRIPLE_BARRIER = "triple_barrier"


class OutcomeLabelSpec(Contract):
    """一份 `OutcomeSpec` 的可执行标签参数。

    - `forward_return`：`exit_close / entry_open - 1`，两个屏障必须为空；
    - `triple_barrier`：上屏障 `entry * (1 + upper_barrier)`、下屏障 `entry * (1 - lower_barrier)`，
      垂直屏障 = horizon；两个屏障必须给出，`lower_barrier < 1`。

    屏障与 horizon 是**标签定义参数**，不是验证阈值。
    """

    outcome: Ref
    outcome_spec_hash: ContentHash
    method: OutcomeMethod
    horizon: timedelta
    upper_barrier: FiniteDecimal | None = None
    lower_barrier: FiniteDecimal | None = None

    @model_validator(mode="after")
    def _shape(self) -> OutcomeLabelSpec:
        if self.outcome.kind is not Kind.OUTCOME:
            raise ValueError(f"outcome 必须是 kind=outcome 的引用，收到 {self.outcome}")
        if self.horizon <= timedelta(0):
            raise ValueError("horizon 必须为正")
        barriers = (self.upper_barrier, self.lower_barrier)
        if self.method is OutcomeMethod.FORWARD_RETURN:
            if any(item is not None for item in barriers):
                raise ValueError("forward_return 不接受屏障参数")
        else:
            if self.upper_barrier is None or self.lower_barrier is None:
                raise ValueError("triple_barrier 必须同时给出上下屏障")
            if self.upper_barrier <= 0 or not Decimal(0) < self.lower_barrier < Decimal(1):
                raise ValueError("屏障必须满足 upper > 0、0 < lower < 1")
        return self

    @classmethod
    def bind(
        cls,
        spec: OutcomeSpec,
        method: OutcomeMethod,
        *,
        upper_barrier: Decimal | None = None,
        lower_barrier: Decimal | None = None,
    ) -> OutcomeLabelSpec:
        """从一份 `OutcomeSpec` 构造：引用、内容哈希与 horizon 都取自该规格。"""
        return cls(
            outcome=spec.ref,
            outcome_spec_hash=spec.content_hash(),
            method=method,
            horizon=spec.horizon,
            upper_barrier=upper_barrier,
            lower_barrier=lower_barrier,
        )

    def matches(self, spec: OutcomeSpec) -> bool:
        """是否绑定到这一份 `OutcomeSpec`（引用、内容哈希与 horizon 都一致）。"""
        return (
            self.outcome.target_identity() == spec.ref.target_identity()
            and self.outcome_spec_hash == spec.content_hash()
            and self.horizon == spec.horizon
        )


class OutcomePriceBar(Contract):
    """一根 Canonical 价格 bar：区间 `[interval_start, interval_end)`，区间结束后才可用。"""

    interval_start: UtcDatetime
    interval_end: UtcDatetime
    available_time: UtcDatetime
    open: FiniteDecimal = Field(gt=0)
    high: FiniteDecimal = Field(gt=0)
    low: FiniteDecimal = Field(gt=0)
    close: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def _lawful(self) -> OutcomePriceBar:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end 必须晚于 interval_start")
        if self.available_time < self.interval_end:
            raise ValueError("bar 在区间结束前不可用：available_time 不得早于 interval_end")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC 不自洽：low <= open/close <= high")
        return self


class OutcomeEvent(Contract):
    """一个需要标签的事件：`event_time` 是决策时刻（信号可被观察的时刻）。"""

    event_key: NonEmptyStr
    event_time: UtcDatetime


def _first_at_or_after(starts: list[datetime], t: datetime) -> int:
    """二分查找：第一个 `starts[i] >= t` 的下标（`starts` 升序），不存在则为 `len(starts)`。"""
    low, high = 0, len(starts)
    while low < high:
        middle = (low + high) // 2
        if starts[middle] < t:
            low = middle + 1
        else:
            high = middle
    return low


def _event_order(item: OutcomeEvent) -> tuple[datetime, str]:
    return (item.event_time, item.event_key)


class OutcomeRequest(Contract):
    """一次标签计算请求（单一标的）。

    - `events`：非空，按 `(event_time, event_key)` 规范排序；`event_key` 唯一；
    - `bars`：按 `interval_start` 严格升序且互不重叠；每根 `available_time <= price_cutoff`；
    - 同一请求内容 → 同一 `content_hash()`（事件输入顺序不影响）。
    """

    label_spec: OutcomeLabelSpec
    manifest_content_hash: ContentHash
    price_cutoff: UtcDatetime
    events: tuple[OutcomeEvent, ...] = Field(min_length=1)
    bars: tuple[OutcomePriceBar, ...]

    @field_validator("events")
    @classmethod
    def _canonical_events(cls, value: tuple[OutcomeEvent, ...]) -> tuple[OutcomeEvent, ...]:
        ordered = tuple(sorted(value, key=_event_order))
        keys = [item.event_key for item in ordered]
        if len(set(keys)) != len(keys):
            raise ValueError("events 的 event_key 必须唯一")
        return ordered

    @field_validator("bars")
    @classmethod
    def _ordered_bars(cls, value: tuple[OutcomePriceBar, ...]) -> tuple[OutcomePriceBar, ...]:
        for earlier, later in pairwise(value):
            if later.interval_start < earlier.interval_end:
                raise ValueError("bars 必须按 interval_start 升序且互不重叠")
        return value

    @model_validator(mode="after")
    def _cutoff(self) -> OutcomeRequest:
        for bar in self.bars:
            if bar.available_time > self.price_cutoff:
                raise ValueError(
                    f"{bar.interval_start.isoformat()} 的 bar 晚于 price_cutoff 才可用"
                )
        return self


class OutcomeLabel(Contract):
    """一个事件的标签。`value is None` = 显式不可计算（缺数据 / 窗口未完成），不填补。

    `barrier`：triple barrier 的触达结果（`1` 上、`-1` 下、`0` 垂直）；forward return 恒为 `None`。
    `available_time`：标签可被知道的时刻，不早于 `exit_time`。
    """

    label_only: Literal[True] = True
    event_key: NonEmptyStr
    event_time: UtcDatetime
    value: FiniteDecimal | None
    barrier: Literal[-1, 0, 1] | None = None
    entry_time: UtcDatetime | None = None
    exit_time: UtcDatetime | None = None
    entry_price: FiniteDecimal | None = Field(default=None, gt=0)
    exit_price: FiniteDecimal | None = Field(default=None, gt=0)
    available_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def _shape(self) -> OutcomeLabel:
        details = (
            self.entry_time,
            self.exit_time,
            self.entry_price,
            self.exit_price,
            self.available_time,
        )
        if self.value is None:
            if any(item is not None for item in details) or self.barrier is not None:
                raise ValueError("不可计算的标签（value 为 None）不得携带入场 / 出场细节")
            return self
        if any(item is None for item in details):
            raise ValueError("可计算的标签必须给出入场 / 出场时间与价格及 available_time")
        assert self.entry_time is not None and self.exit_time is not None
        assert self.available_time is not None
        if self.entry_time < self.event_time:
            raise ValueError("entry_time 不得早于 event_time（标签窗口必须在事件之后）")
        if self.exit_time <= self.entry_time:
            raise ValueError("exit_time 必须晚于 entry_time")
        if self.available_time < self.exit_time:
            raise ValueError("available_time 不得早于 exit_time")
        return self


class OutcomeProviderDescriptor(Contract):
    """OutcomeProvider 的身份与能力；`supported_outcomes`：`outcome:name@semver` → 标签规格哈希。"""

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    supported_outcomes: FrozenMapping[OutcomeRefKey, ContentHash]

    @field_validator("supported_outcomes")
    @classmethod
    def _non_empty(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if not value:
            raise ValueError("supported_outcomes 不得为空")
        return value

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"

    def supports(self, label_spec: OutcomeLabelSpec) -> bool:
        return self.supported_outcomes.get(str(label_spec.outcome)) == label_spec.content_hash()


def _result_hash(
    request_hash: str, provider: str, provider_hash: str, labels: Iterable[OutcomeLabel]
) -> str:
    return content_hash(
        {
            "request_hash": request_hash,
            "provider": provider,
            "provider_hash": provider_hash,
            "labels": [item.model_dump(mode="json") for item in labels],
        }
    )


class OutcomeResult(Contract):
    """一次 `compute` 的结果：标签与请求事件一一对应（同序），`result_hash` 构造时复核。"""

    label_only: Literal[True] = True
    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    labels: tuple[OutcomeLabel, ...] = Field(min_length=1)
    result_hash: ContentHash

    @model_validator(mode="after")
    def _self_consistent_hash(self) -> OutcomeResult:
        expected = _result_hash(self.request_hash, self.provider, self.provider_hash, self.labels)
        if self.result_hash != expected:
            raise ValueError("result_hash 与结果内容不符")
        return self

    @classmethod
    def build(
        cls,
        request: OutcomeRequest,
        descriptor: OutcomeProviderDescriptor,
        labels: Iterable[OutcomeLabel],
    ) -> OutcomeResult:
        items = tuple(labels)
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            labels=items,
            result_hash=_result_hash(request_hash, descriptor.plugin_key, provider_hash, items),
        )

    def check_answers(self, request: OutcomeRequest, descriptor: OutcomeProviderDescriptor) -> None:
        """对照请求检查本结果；不符抛 `ValueError`。

        请求哈希与 Provider 身份一致；标签与事件一一对应；每个可计算标签：入场是事件后的第一根
        bar 的起点，出场是某根 bar 的终点且不晚于 `entry_time + horizon`（未触达屏障时恰等于它），
        `available_time` 是窗口内 bar 的最大 `available_time`（因而 `<= price_cutoff`）。
        """
        if self.request_hash != request.content_hash():
            raise ValueError("request_hash 与请求不符")
        if (self.provider, self.provider_hash) != (
            descriptor.plugin_key,
            descriptor.content_hash(),
        ):
            raise ValueError("provider / provider_hash 与 descriptor 不符")
        answered = tuple((item.event_key, item.event_time) for item in self.labels)
        asked = tuple((item.event_key, item.event_time) for item in request.events)
        if answered != asked:
            raise ValueError("labels 必须与请求的 events 一一对应且同序")
        horizon = request.label_spec.horizon
        bars = request.bars
        starts = [bar.interval_start for bar in bars]  # bars are ordered and non-overlapping
        for label in self.labels:
            if label.value is None:
                continue
            assert label.entry_time is not None and label.exit_time is not None
            first = _first_at_or_after(starts, label.event_time)
            entry = bars[first] if first < len(bars) else None
            if entry is None or entry.interval_start != label.entry_time:
                raise ValueError(f"{label.event_key!r} 的入场不是事件后的第一根 bar")
            if label.exit_time > label.entry_time + horizon:
                raise ValueError(f"{label.event_key!r} 的出场晚于 entry_time + horizon")
            if label.barrier in (None, 0) and label.exit_time != label.entry_time + horizon:
                raise ValueError(
                    f"{label.event_key!r} 未触达屏障却未在 horizon 处出场（部分窗口不得填补）"
                )
            window = []
            for bar in bars[first:]:
                if bar.interval_end > label.exit_time:
                    break
                window.append(bar)
            if not window or window[-1].interval_end != label.exit_time:
                raise ValueError(f"{label.event_key!r} 的出场不是窗口内某根 bar 的终点")
            if label.available_time != max(bar.available_time for bar in window):
                raise ValueError(f"{label.event_key!r} 的 available_time 不是窗口内 bar 的最大值")


class OutcomeProvider(Protocol):
    """确定性结果标签计算（ADR-0037）。语义见模块文档。"""

    @property
    def descriptor(self) -> OutcomeProviderDescriptor:
        """Provider 身份与支持的标签规格；实例生命周期内不变。"""
        ...

    def compute(self, request: OutcomeRequest) -> OutcomeResult:
        """计算每个事件的标签；不支持的规格 → `UnsupportedOutcome`。"""
        ...


#: 只作标签的 Outcome 载荷模型（带 `label_only` 判别字段）。
OUTCOME_MODELS: tuple[type[Contract], ...] = (OutcomeLabel, OutcomeResult)


def is_outcome_payload(value: object) -> bool:
    """`value` 是否是 Outcome 载荷：Outcome DTO 实例，或带 `label_only` 判别键的映射。"""
    if isinstance(value, OUTCOME_MODELS):
        return True
    return isinstance(value, Mapping) and value.get("label_only") is True


def refuse_outcome_input(values: Iterable[object], where: str) -> None:
    """若任一值是 Outcome 载荷则抛 `OutcomeUsedAsInput`（Constitution C-L2）。"""
    for value in values:
        if is_outcome_payload(value):
            raise OutcomeUsedAsInput(f"{where} 收到 Outcome 载荷；Outcome 永不作为输入（C-L2）")
