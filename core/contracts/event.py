"""`EventProvider`：离散事件识别的 Protocol 与 DTO（ADR-0036；Phase 3）。

对应 docs/architecture/05-plugin.md §3（"识别事件/交互：Features + States → Events；必须确定性"）与
roadmap Phase 3。本模块只定义可执行 Protocol、可序列化 DTO 与它们在契约层可证明的不变量；执行器在
`infrastructure/event/`，首批实现在 `plugins/events/`。

| 成员 | 输入 → 输出 |
|---|---|
| `descriptor` | → `EventProviderDescriptor`（`name@version`、确定性、支持的 event 与 spec hash） |
| `detect` | `EventRequest` → `EventResult`（截至 `as_of` 的事件表） |

**输入**：`EventInputPoint` 是上游序列上的一个点——一个 Feature 值或一个 State 标签——带
`available_time`（该点可被观测的最早时刻）与 `source_lineage_hash`（该点来源的哈希，溯源；只能依赖
`available_time` 时已知的信息——不得是整段上游运行的结果哈希，否则过去事件的身份会随未来数据改变）。
交互算子另以上游 `Event` 为输入（`EventRequest.upstream_events`）。每条序列只追加：同一 `source` 按
`evaluation_time` 排序后 `available_time` 不递减，因此"截至 t 可见"的点总是该序列的前缀。

**事件时间 = 可观测时间**（ADR-0036 §2）：`Event.event_time` 恰好等于它所引用的全部输入（点的
`available_time`、上游事件的 `event_time`）中最晚的那个再加 `EventSpec.observable_lag`。事件只能引用
在 `event_time` 已可见的输入（`available_time + observable_lag <= event_time`）。

**截至 `as_of` 的事件表**：`detect` 返回 `event_time <= as_of` 的全部事件，且只能是 `as_of`
可见集合（`EventRequest.visible_at`）的函数。执行器对多个检查点 `t` 各调用一次（每次只交出 `t`
的可见集合），并要求截至 `t` 的表恰好是截至更晚时刻的表在 `event_time <= t` 上的限制——
事件一经出现不得撤回，也不得事后回填到更早的时间（"未来确认"，roadmap Phase 3 禁止事项）。

**溯源**：`Event.input_ids` 是所用输入点的 `content_hash()`；`upstream_event_ids` 是所用上游事件的
`event_id`；`event_id` 是事件自身内容的哈希，构造时复核。交互算子的输出因此可逐跳追溯到上游事件与其
Feature / State 输入。

**数值**：输入值与事件属性一律为 `Decimal` / `int` / `bool` / 非数值文本（与 `FeatureObservation`
同一规则，ADR-0013）；浮点、NaN / ±Infinity 被拒绝。

**标的（subject，ADR-0057）**：可选的 `EventRequest.subject` 指明该请求所属的标的 / 序列（例如
一个 instrument）。一个请求只对应一个标的（ADR-0036 §4 原则），多标的事件表 = 每个标的一个请求，
其事件按 `subject` 键合并。给出时：`Event.subject` / `EventResult.subject` 必须与请求相同
（`EventResult.build` 把未绑定的事件绑定到请求的标的，冲突即拒绝），上游事件也必须属于同一标的；
`subject` 进入请求哈希、`event_id` 与 `result_hash`。缺失（`None`）时从载荷与哈希输入中省略：
既有请求、事件与结果的哈希逐位不变。

**诚实边界**：DTO 只证明形状与请求 / 结果之间可局部检查的关系。事件是否只依赖可见集合、确定性、
截至不同时刻的表是否一致，由执行器（`infrastructure/event/runner.py`）与 contract suite
（`tests/contract_suites/event.py`）对具体实现检查。`source_lineage_hash` 是否真的对应一份
已登记的上游值属 Registry。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from core.contracts.feature import ObservationScalar
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
    "EVENT_REF_KEY_PATTERN",
    "Event",
    "EventInputError",
    "EventInputPoint",
    "EventProvider",
    "EventProviderDescriptor",
    "EventProviderError",
    "EventRequest",
    "EventResult",
    "SubjectName",
    "UnsupportedEvent",
]

#: `EventProviderDescriptor.supported_events` 的键：`event:name@semver`。
EVENT_REF_KEY_PATTERN = rf"^event:{PLUGIN_KEY_PATTERN.removeprefix('^')}"

ValueName = Annotated[str, Field(pattern=NAME_PATTERN)]
EventRefKey = Annotated[str, Field(pattern=EVENT_REF_KEY_PATTERN)]


#: ADR-0057 的 `subject` 自契约 2.1.0 起（ADR-0052 §4、Codex K3 / K5）：2.0.0 信封携带它即拒绝；
#: 不带它的 2.0.0 请求 / 事件 / 结果照旧读取，哈希由其自身载荷复核、逐位不变。
ADR_0057_VERSION: Final = "2.1.0"


def _omit_none(value: object) -> bool:
    """ADR-0057：可选的 `subject` 为 `None` 时从载荷中省略，使既有载荷与哈希逐位不变。"""
    return value is None


#: 请求 / 事件 / 结果所属的标的或序列名（ADR-0057）；非空（纯空白经去空白后同样拒绝）。
SubjectName = Annotated[str, Field(min_length=1)]

#: `EventInputPoint.source` 允许的 `Ref.kind`（EventSpec 只能依赖 Feature / State，ADR-0012）。
EVENT_INPUT_KINDS = frozenset({Kind.FEATURE, Kind.STATE})


class EventProviderError(Exception):
    """EventProvider 契约错误的基类。"""


class UnsupportedEvent(EventProviderError):
    """请求的 event `name@version` 或 `spec_hash` 不由该 Provider 声明支持。"""


class EventInputError(EventProviderError):
    """输入不符合该事件定义的输入约定（缺序列、类型不符等）；fail closed。"""


def _require_lag(lag: timedelta) -> None:
    if not isinstance(lag, timedelta) or lag < timedelta(0):
        raise ValueError("observable_lag 必须是非负 timedelta")


def _require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("时间必须是带时区的 UTC 时间")


def _sorted_unique_hashes(value: tuple[str, ...], label: str) -> tuple[str, ...]:
    if list(value) != sorted(set(value)):
        raise ValueError(f"{label} 必须严格升序（因而唯一）")
    return value


class EventInputPoint(Contract):
    """上游序列上的一个点：一个 Feature 值或 State 标签（ADR-0036 §1）。

    - `source`：`kind=feature` 或 `kind=state` 的引用；序列身份即 `str(source)`；
    - `source_lineage_hash`：该点来源的哈希（例如上游规格 + 数据集 + Provider + 该时刻的值，见
      `infrastructure.event.inputs`）；只能依赖 `available_time` 时已知的信息，因此过去的点与
      引用它们的事件的身份不随未来数据改变；
    - `evaluation_time`：该值所描述的时刻；`available_time`：它可被观测的最早时刻，不早于
      `evaluation_time`；
    - `value`：`None` 表示上游显式"不可计算"；State 标签以非数值文本给出。
    """

    source: Ref
    source_lineage_hash: ContentHash
    evaluation_time: UtcDatetime
    available_time: UtcDatetime
    value: ObservationScalar | None

    @model_validator(mode="after")
    def _invariants(self) -> EventInputPoint:
        if self.source.kind not in EVENT_INPUT_KINDS:
            raise ValueError(f"source 只能是 feature 或 state 引用，收到 {self.source}")
        if self.available_time < self.evaluation_time:
            raise ValueError("available_time 不得早于 evaluation_time")
        return self

    @property
    def point_id(self) -> str:
        """该点的内容哈希；`Event.input_ids` 引用它。"""
        return self.content_hash()


def _event_id(payload: dict[str, Any]) -> str:
    return content_hash({key: value for key, value in payload.items() if key != "event_id"})


class Event(Contract):
    """一个离散事件（Event 表的一行）。

    - `event` / `spec_hash`：产出它的事件定义（版本化，内容哈希绑定参数）；
    - `event_time`：可观测时间（见模块文档）；
    - `attributes`：事件的描述性内容（方向、前后状态、阈值……）；
    - `input_ids`：所用输入点的 `point_id`，严格升序；`upstream_event_ids`：所用上游事件的
      `event_id`，严格升序；两者至少一个非空；
    - `subject`：可选，事件所属的标的（ADR-0057）；给出时进入 `event_id`，缺失时省略（旧 id 不变）；
    - `event_id`：以上内容的哈希，构造时复核（不接受自报的哈希）。
    """

    _FIELDS_SINCE = {"subject": ADR_0057_VERSION}

    event: Ref
    spec_hash: ContentHash
    event_time: UtcDatetime
    attributes: FrozenMapping[ValueName, ObservationScalar] = Field(
        default_factory=dict, validate_default=True
    )
    input_ids: tuple[ContentHash, ...] = ()
    upstream_event_ids: tuple[ContentHash, ...] = ()
    event_id: ContentHash
    subject: SubjectName | None = Field(default=None, exclude_if=_omit_none)

    @field_validator("input_ids")
    @classmethod
    def _inputs_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_hashes(value, "input_ids")

    @field_validator("upstream_event_ids")
    @classmethod
    def _upstream_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_hashes(value, "upstream_event_ids")

    @model_validator(mode="after")
    def _invariants(self) -> Event:
        if self.event.kind is not Kind.EVENT:
            raise ValueError(f"event 必须是 kind=event 的引用，收到 {self.event}")
        if not self.input_ids and not self.upstream_event_ids:
            raise ValueError("事件必须至少引用一个输入点或上游事件（可追溯）")
        if self.event_id != _event_id(self.model_dump(mode="json")):
            raise ValueError("event_id 与事件内容不符")
        return self

    @classmethod
    def build(
        cls,
        *,
        event: Ref,
        spec_hash: str,
        event_time: datetime,
        attributes: dict[str, Any] | None = None,
        inputs: Iterable[EventInputPoint] = (),
        upstream: Iterable[Event] = (),
        subject: str | None = None,
    ) -> Event:
        """由所用输入点与上游事件构造（计算 `input_ids`、`upstream_event_ids` 与 `event_id`）。"""
        fields: dict[str, Any] = {
            "event": event,
            "spec_hash": spec_hash,
            "event_time": event_time,
            "attributes": dict(attributes or {}),
            "input_ids": tuple(sorted({item.point_id for item in inputs})),
            "upstream_event_ids": tuple(sorted({item.event_id for item in upstream})),
        }
        if subject is not None:
            fields["subject"] = subject
        # 同形的无 id 模型先完整校验并规范化载荷，再由其 JSON 形式计算 id。
        probe = _EventProbe(**fields).model_dump(mode="json")
        return cls(**fields, event_id=_event_id(probe))

    def bound_to(self, subject: str | None) -> Event:
        """绑定到 `subject` 的同一事件（ADR-0057）：已是该标的 → 自身；未绑定 → 重算 `event_id`；
        已绑定到另一个标的 → `ValueError`（不改写别的标的的事件）。`None` 只接受未绑定的事件。"""
        if self.subject == subject:
            return self
        if subject is None or self.subject is not None:
            raise ValueError(
                f"事件 {self.event_id[:12]} 属于标的 {self.subject!r}，不能绑定到 {subject!r}"
            )
        fields = self.model_dump(exclude={"event_id", "schema_version"})
        fields["subject"] = subject
        probe = _EventProbe(**fields).model_dump(mode="json")
        return type(self)(**fields, event_id=_event_id(probe))


class _EventProbe(Contract):
    """`Event` 去掉 `event_id` 后的同形模型：只用于计算 `event_id`，不登记、不导出。

    字段必须与 `Event` 逐一相同（`tests/test_event_contracts.py` 断言），否则 id 不自洽。
    """

    _FIELDS_SINCE = {"subject": ADR_0057_VERSION}

    event: Ref
    spec_hash: ContentHash
    event_time: UtcDatetime
    attributes: FrozenMapping[ValueName, ObservationScalar] = Field(
        default_factory=dict, validate_default=True
    )
    input_ids: tuple[ContentHash, ...] = ()
    upstream_event_ids: tuple[ContentHash, ...] = ()
    subject: SubjectName | None = Field(default=None, exclude_if=_omit_none)


def _input_order(item: EventInputPoint) -> tuple[datetime, str, datetime]:
    return (item.available_time, str(item.source), item.evaluation_time)


def _event_order(item: Event) -> tuple[datetime, str]:
    return (item.event_time, item.event_id)


class EventRequest(Contract):
    """一次事件识别请求：截至 `as_of` 的事件表。

    - `event` 必须是 `kind=event` 的引用，`spec_hash` 为该 `EventSpec` 的内容哈希；
    - `inputs`：按 `(available_time, source, evaluation_time)` 规范排序；
      `(source, evaluation_time)` 重复即拒绝；每条序列只追加（同一 source 按 `evaluation_time`
      排序后 `available_time` 不递减）；
    - `upstream_events`：交互算子的上游事件，按 `(event_time, event_id)` 规范排序，`event_id` 唯一；
    - `subject`：可选，请求所属的标的（ADR-0057）。一个请求只对应一个标的：上游事件的 `subject`
      必须与请求相同（都缺失亦可）。缺失时从载荷省略，既有请求哈希逐位不变。

    同一请求内容 → 同一 `content_hash()`（输入顺序不影响）。
    """

    _FIELDS_SINCE = {"subject": ADR_0057_VERSION}

    event: Ref
    spec_hash: ContentHash
    as_of: UtcDatetime
    inputs: tuple[EventInputPoint, ...] = ()
    upstream_events: tuple[Event, ...] = ()
    subject: SubjectName | None = Field(default=None, exclude_if=_omit_none)

    @field_validator("inputs")
    @classmethod
    def _canonical_inputs(cls, value: tuple[EventInputPoint, ...]) -> tuple[EventInputPoint, ...]:
        ordered = tuple(sorted(value, key=_input_order))
        seen: set[tuple[str, datetime]] = set()
        by_source: dict[str, list[EventInputPoint]] = {}
        for item in ordered:
            key = (str(item.source), item.evaluation_time)
            if key in seen:
                raise ValueError(
                    f"inputs 中 (source, evaluation_time) 重复：{key[0]} @ {key[1].isoformat()}"
                )
            seen.add(key)
            by_source.setdefault(key[0], []).append(item)
        for source, points in by_source.items():
            points.sort(key=lambda point: point.evaluation_time)
            for earlier, later in pairwise(points):
                if later.available_time < earlier.available_time:
                    raise ValueError(
                        f"序列 {source} 不是只追加的：{later.evaluation_time.isoformat()} 的点"
                        "比更早的点先可用"
                    )
        return ordered

    @field_validator("upstream_events")
    @classmethod
    def _canonical_upstream(cls, value: tuple[Event, ...]) -> tuple[Event, ...]:
        ordered = tuple(sorted(value, key=_event_order))
        ids = [item.event_id for item in ordered]
        if len(set(ids)) != len(ids):
            raise ValueError("upstream_events 的 event_id 重复")
        return ordered

    @model_validator(mode="after")
    def _request_invariants(self) -> EventRequest:
        if self.event.kind is not Kind.EVENT:
            raise ValueError(f"event 必须是 kind=event 的引用，收到 {self.event}")
        if any(item.event == self.event for item in self.upstream_events):
            raise ValueError("事件不得以自身的输出为上游输入")
        foreign = sorted(
            {repr(item.subject) for item in self.upstream_events if item.subject != self.subject}
        )
        if foreign:
            raise ValueError(
                f"上游事件的标的 {foreign} 与请求的标的 {self.subject!r} 不同"
                "（一个请求只对应一个标的，ADR-0057）"
            )
        return self

    def visible_at(
        self, at: datetime, observable_lag: timedelta
    ) -> tuple[tuple[EventInputPoint, ...], tuple[Event, ...]]:
        """`at` 的可见集合：`available_time + lag <= at` 的点与对应的上游事件。"""
        _require_lag(observable_lag)
        _require_utc(at)
        points = tuple(item for item in self.inputs if item.available_time + observable_lag <= at)
        upstream = tuple(
            item for item in self.upstream_events if item.event_time + observable_lag <= at
        )
        return points, upstream

    def truncated(self, at: datetime, observable_lag: timedelta) -> EventRequest:
        """同一事件定义、`as_of = at`、只含 `at` 可见集合的子请求（执行器用）。"""
        points, upstream = self.visible_at(at, observable_lag)
        return EventRequest(
            event=self.event,
            spec_hash=self.spec_hash,
            as_of=at,
            inputs=points,
            upstream_events=upstream,
            subject=self.subject,
        )


class EventProviderDescriptor(Contract):
    """EventProvider 的身份与能力声明（05-plugin.md §3：EventProvider 必须确定性）。

    `supported_events`：非空，`event:name@semver` → 该 `EventSpec` 的内容哈希。事件定义的参数
    （阈值、窗口、上游事件）属于规格内容，因而由哈希绑定。实例生命周期内不变。
    """

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    supported_events: FrozenMapping[EventRefKey, ContentHash]

    @field_validator("supported_events")
    @classmethod
    def _non_empty(cls, value: FrozenMapping[str, str]) -> FrozenMapping[str, str]:
        if not value:
            raise ValueError("supported_events 不得为空")
        return value

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"

    def supports(self, event: Ref, spec_hash: str) -> bool:
        return self.supported_events.get(str(event)) == spec_hash


def _result_hash(
    request_hash: str,
    provider: str,
    provider_hash: str,
    as_of: datetime,
    events: Iterable[Event],
    subject: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "request_hash": request_hash,
        "provider": provider,
        "provider_hash": provider_hash,
        "as_of": as_of.isoformat(),
        "events": [item.event_id for item in events],
    }
    if subject is not None:  # ADR-0057：缺失时不进入哈希输入，既有 result_hash 不变
        payload["subject"] = subject
    return content_hash(payload)


class EventResult(Contract):
    """一次 `detect` 的结果：截至 `as_of` 的事件表。

    - `events`：按 `(event_time, event_id)` 严格升序（`event_id` 唯一），且 `event_time <= as_of`；
    - `subject`：可选，所答请求的标的（ADR-0057）；给出时每个事件都属于它，并进入 `result_hash`；
    - `result_hash`：请求哈希、Provider 身份、`as_of`、事件 id 序列（与 `subject`，若有）的哈希，
      构造时复核。

    同一请求 + 同一 Provider 版本 → 同一 `result_hash`（确定性由 contract suite 检查）。
    """

    _FIELDS_SINCE = {"subject": ADR_0057_VERSION}

    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    as_of: UtcDatetime
    events: tuple[Event, ...] = ()
    result_hash: ContentHash
    subject: SubjectName | None = Field(default=None, exclude_if=_omit_none)

    @model_validator(mode="after")
    def _invariants(self) -> EventResult:
        keys = [_event_order(item) for item in self.events]
        if any(later <= earlier for earlier, later in pairwise(keys)):
            raise ValueError("events 必须按 (event_time, event_id) 严格升序")
        if len({item.event_id for item in self.events}) != len(self.events):
            raise ValueError("events 的 event_id 重复")
        if any(item.event_time > self.as_of for item in self.events):
            raise ValueError("事件的 event_time 不得晚于 as_of")
        if any(item.subject != self.subject for item in self.events):
            raise ValueError("事件的 subject 必须与结果的 subject 相同（ADR-0057）")
        expected = _result_hash(
            self.request_hash,
            self.provider,
            self.provider_hash,
            self.as_of,
            self.events,
            self.subject,
        )
        if self.result_hash != expected:
            raise ValueError("result_hash 与结果内容不符")
        return self

    @classmethod
    def build(
        cls,
        request: EventRequest,
        descriptor: EventProviderDescriptor,
        events: Iterable[Event],
    ) -> EventResult:
        """由请求、descriptor 与事件构造结果（事件按规范顺序排列，重复的同一事件合并）。

        请求有 `subject` 时，未绑定的事件绑定到它（`Event.bound_to`），属于别的标的的事件拒绝。
        """
        unique = {
            bound.event_id: bound for bound in (item.bound_to(request.subject) for item in events)
        }
        items = tuple(sorted(unique.values(), key=_event_order))
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        fields: dict[str, Any] = {}
        if request.subject is not None:
            fields["subject"] = request.subject
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            as_of=request.as_of,
            events=items,
            result_hash=_result_hash(
                request_hash,
                descriptor.plugin_key,
                provider_hash,
                request.as_of,
                items,
                request.subject,
            ),
            **fields,
        )

    def restricted_to(self, at: datetime) -> tuple[Event, ...]:
        """`event_time <= at` 的事件（规范顺序）。"""
        _require_utc(at)
        return tuple(item for item in self.events if item.event_time <= at)

    def check_answers(
        self,
        request: EventRequest,
        descriptor: EventProviderDescriptor,
        observable_lag: timedelta,
    ) -> None:
        """对照请求检查本结果；不符抛 `ValueError`。

        请求哈希、Provider 身份与 `as_of` 一致；每个事件属于请求的事件定义；引用的输入点与上游
        事件都在请求中、且在 `event_time` 已可见；`event_time` 恰好是所引用输入中最晚可见的时刻
        （事件时间 = 可观测时间）。
        """
        _require_lag(observable_lag)
        if self.request_hash != request.content_hash():
            raise ValueError("request_hash 与请求不符")
        if (self.provider, self.provider_hash) != (
            descriptor.plugin_key,
            descriptor.content_hash(),
        ):
            raise ValueError("provider / provider_hash 与 descriptor 不符")
        if self.as_of != request.as_of:
            raise ValueError("as_of 与请求不符")
        if self.subject != request.subject:
            raise ValueError("subject 与请求不符（ADR-0057）")
        points = {item.point_id: item for item in request.inputs}
        upstream = {item.event_id: item for item in request.upstream_events}
        for item in self.events:
            label = f"事件 {item.event_id[:12]} @ {item.event_time.isoformat()}"
            if item.event != request.event or item.spec_hash != request.spec_hash:
                raise ValueError(f"{label} 不属于请求的事件定义")
            times: list[datetime] = []
            for point_id in item.input_ids:
                point = points.get(point_id)
                if point is None:
                    raise ValueError(f"{label} 引用了请求中没有的输入点")
                times.append(point.available_time)
            for event_id in item.upstream_event_ids:
                source = upstream.get(event_id)
                if source is None:
                    raise ValueError(f"{label} 引用了请求中没有的上游事件")
                times.append(source.event_time)
            observable = max(times) + observable_lag
            if observable > item.event_time:
                raise ValueError(f"{label} 引用了在 event_time 尚不可见的输入（未来信息）")
            if observable != item.event_time:
                raise ValueError(
                    f"{label} 的 event_time 不是可观测时间（应为 {observable.isoformat()}）"
                )


class EventProvider(Protocol):
    """确定性事件识别（ADR-0036）。语义见模块文档。"""

    @property
    def descriptor(self) -> EventProviderDescriptor:
        """Provider 身份与支持的事件规格；实例生命周期内不变。"""
        ...

    def detect(self, request: EventRequest) -> EventResult:
        """截至 `request.as_of` 的事件表；不支持的规格 → `UnsupportedEvent`。"""
        ...
