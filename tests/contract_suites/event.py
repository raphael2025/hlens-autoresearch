"""`EventProvider` 的 provider-agnostic contract suite（core/contracts/event.py；ADR-0036 §6）。

实现方提供 `EventSubject`：

- `open`：每次调用返回一个**新** Provider 实例（模拟重启）；
- `spec`：被测的 `EventSpec`，Provider 必须在 descriptor 中声明支持它；`observable_lag` 必须为正
  （否则 lag 检查没有对象）；
- `inputs` / `upstream_events`：请求夹具（序列点与上游事件）；
- `as_of_times`：严格升序的截至时刻网格。它必须让检查不落空：最后一个时刻得到至少一个事件；至少
  一个时刻之后还有输入变为可见；至少一个时刻的 lag 窗口 `(t - lag, t]` 内有输入变为可用；
- `perturb`：可选，返回同源、同时间、值不同的输入点；给出时施加于全部输入点必须改变结果。

检查只看可观察行为：descriptor、`detect` 的结果，以及对请求的截断 / 扰动 / 变体下结果如何变化。
核心检查是**不得未来确认**（`check_point_in_time_consistency`）：截至 t 的事件表必须恰好是截至更晚
时刻的表在 `event_time <= t` 上的限制，且每个事件在截至它自己的 `event_time` 时已经出现。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from itertools import pairwise
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.event import (
    Event,
    EventInputPoint,
    EventProvider,
    EventProviderDescriptor,
    EventRequest,
    EventResult,
    UnsupportedEvent,
)
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import EventSpec
from tests.contract_suites._support import (
    ContractSuiteFailure,
    call_ok,
    expect_error,
    require,
    revalidated,
)

__all__ = [
    "EVENT_CHECKS",
    "EventCheck",
    "EventProviderContract",
    "EventSubject",
]


@dataclass(frozen=True)
class EventSubject:
    """被测 `EventProvider` 的接入点（见模块文档）。"""

    open: Callable[[], EventProvider]
    spec: EventSpec
    as_of_times: tuple[datetime, ...]
    inputs: tuple[EventInputPoint, ...] = ()
    upstream_events: tuple[Event, ...] = ()
    perturb: Callable[[EventInputPoint], EventInputPoint] | None = None


type EventCheck = Callable[[EventSubject], None]


# ======================================================================================
# 工具
# ======================================================================================


def _lag(subject: EventSubject) -> timedelta:
    lag = subject.spec.observable_lag
    require(lag > timedelta(0), "subject.spec 的 observable_lag 必须为正（lag 检查需要它）")
    return lag


def _request(
    subject: EventSubject,
    *,
    as_of: datetime | None = None,
    inputs: Sequence[EventInputPoint] | None = None,
    upstream_events: Sequence[Event] | None = None,
    **overrides: Any,
) -> EventRequest:
    fields: dict[str, Any] = {
        "event": subject.spec.ref,
        "spec_hash": subject.spec.content_hash(),
        "as_of": subject.as_of_times[-1] if as_of is None else as_of,
        "inputs": tuple(subject.inputs if inputs is None else inputs),
        "upstream_events": tuple(
            subject.upstream_events if upstream_events is None else upstream_events
        ),
    }
    fields.update(overrides)
    return call_ok("构造 EventRequest（夹具）", lambda: EventRequest(**fields))


def _descriptor(provider: EventProvider) -> EventProviderDescriptor:
    raw = call_ok("descriptor", lambda: provider.descriptor)
    return revalidated(EventProviderDescriptor, raw, "descriptor")


def _detect(subject: EventSubject, provider: EventProvider, request: EventRequest) -> EventResult:
    """执行一次 detect；结果必须恰好是合规的 `EventResult` 并如实回答该请求。"""
    raw = call_ok("detect", partial(provider.detect, request))
    result = revalidated(EventResult, raw, "detect 的结果")
    try:
        result.check_answers(request, _descriptor(provider), subject.spec.observable_lag)
    except ValueError as exc:
        raise ContractSuiteFailure(f"detect 的结果没有如实回答请求：{exc}") from exc
    return result


def _visible(
    subject: EventSubject, at: datetime
) -> tuple[tuple[EventInputPoint, ...], tuple[Event, ...]]:
    return _request(subject).visible_at(at, subject.spec.observable_lag)


def _perturbed(
    subject: EventSubject, which: Callable[[EventInputPoint], bool]
) -> tuple[EventInputPoint, ...]:
    perturb = subject.perturb
    if perturb is None:
        return subject.inputs
    out = []
    for item in subject.inputs:
        if which(item):
            changed = perturb(item)
            require(
                (changed.source, changed.evaluation_time, changed.available_time)
                == (item.source, item.evaluation_time, item.available_time),
                "subject.perturb 只能改变输入值，不能改变来源或时间",
            )
            out.append(changed)
        else:
            out.append(item)
    return tuple(out)


# ======================================================================================
# 检查
# ======================================================================================


def check_descriptor_declares_the_spec(subject: EventSubject) -> None:
    """descriptor 声明确定性并绑定被测规格的内容哈希；实例生命周期内与跨实例都不变。"""
    provider = subject.open()
    descriptor = _descriptor(provider)
    require(descriptor.deterministic is True, "EventProvider 必须声明 deterministic=True")
    require(
        descriptor.supports(subject.spec.ref, subject.spec.content_hash()),
        f"descriptor 必须声明支持 {subject.spec.ref} 及其 spec hash",
    )
    _detect(subject, provider, _request(subject))
    require(_descriptor(provider) == descriptor, "descriptor 在实例生命周期内不得改变")
    require(_descriptor(subject.open()) == descriptor, "同一实现的新实例 descriptor 必须相同")


def check_fixture_has_events(subject: EventSubject) -> None:
    """夹具自检：截至最后时刻至少有一个事件，每个事件都可追溯到输入。"""
    result = _detect(subject, subject.open(), _request(subject))
    require(bool(result.events), "夹具必须在最后一个截至时刻产生至少一个事件")
    for item in result.events:
        require(bool(item.input_ids or item.upstream_event_ids), "每个事件都必须引用输入（可追溯）")


def check_determinism(subject: EventSubject) -> None:
    """同一请求（同一实例重复、重建实例、输入顺序不同）→ 同一结果与同一 result_hash。"""
    request = _request(subject)
    provider = subject.open()
    first = _detect(subject, provider, request)
    shuffled = _request(
        subject,
        inputs=tuple(reversed(subject.inputs)),
        upstream_events=tuple(reversed(subject.upstream_events)),
    )
    require(shuffled == request, "EventRequest 必须把输入规范排序（夹具自检）")
    for label, again in (
        ("同一实例重复", _detect(subject, provider, request)),
        ("重建实例", _detect(subject, subject.open(), request)),
        ("输入顺序不同", _detect(subject, subject.open(), shuffled)),
    ):
        require(again.result_hash == first.result_hash, f"{label}：result_hash 必须相同")
        require(again == first, f"{label}：结果必须相同")


def check_only_the_visible_set_matters(subject: EventSubject) -> None:
    """截至 t 的结果只取决于 t 的可见集合：删去其余输入（之后才可见、或仍在 lag 内）不改变事件。"""
    lag = _lag(subject)
    provider = subject.open()
    exercised_future = exercised_lag = False
    for at in subject.as_of_times:
        points, upstream = _visible(subject, at)
        if len(points) < len(subject.inputs) or len(upstream) < len(subject.upstream_events):
            exercised_future = True
        if any(at - lag < item.available_time <= at for item in subject.inputs) or any(
            at - lag < item.event_time <= at for item in subject.upstream_events
        ):
            exercised_lag = True
        full = _detect(subject, provider, _request(subject, as_of=at))
        cut = _detect(
            subject, provider, _request(subject, as_of=at, inputs=points, upstream_events=upstream)
        )
        require(
            cut.events == full.events,
            f"截至 {at.isoformat()} 的事件受到了该时刻不可见的输入影响（未来信息或 lag 内的输入）",
        )
    require(exercised_future, "夹具必须至少有一个截至时刻之后还有输入变为可见")
    require(exercised_lag, "夹具必须至少有一个截至时刻的 lag 窗口内有输入变为可用")


def check_causal_perturbation(subject: EventSubject) -> None:
    """改变 t 不可见的输入点的值，截至 t 的事件不变；改变全部输入点必须改变结果（扰动有牙齿）。"""
    if subject.perturb is None:
        return
    lag = subject.spec.observable_lag
    provider = subject.open()
    base = _detect(subject, provider, _request(subject))
    everything = _detect(
        subject, provider, _request(subject, inputs=_perturbed(subject, lambda _: True))
    )
    require(
        everything.events != base.events,
        "subject.perturb 施加于全部输入点必须改变事件（否则扰动检查没有牙齿）",
    )
    for at in subject.as_of_times:

        def invisible(item: EventInputPoint, at: datetime = at) -> bool:
            return item.available_time + lag > at

        before = _detect(subject, provider, _request(subject, as_of=at))
        after = _detect(
            subject, provider, _request(subject, as_of=at, inputs=_perturbed(subject, invisible))
        )
        require(
            after.events == before.events,
            f"截至 {at.isoformat()} 的事件受到了该时刻不可见的输入值影响（扰动）",
        )


def check_point_in_time_consistency(subject: EventSubject) -> None:
    """不得未来确认（在执行器的截断下）。

    截至 t 的表 = 截至更晚时刻的表在 event_time <= t 上的限制；每个事件在截至它自己的
    event_time 时已经出现。
    """
    provider = subject.open()
    lag = subject.spec.observable_lag

    def as_of(at: datetime) -> EventResult:
        # Truncated exactly as the runner does: only the visible set of `at` is handed over.
        return _detect(subject, provider, _request(subject).truncated(at, lag))

    tables = {at: as_of(at) for at in subject.as_of_times}
    for earlier, later in pairwise(subject.as_of_times):
        require(
            tables[later].restricted_to(earlier) == tables[earlier].events,
            f"截至 {later.isoformat()} 的表在 {earlier.isoformat()} 之前的部分与截至 "
            f"{earlier.isoformat()} 的表不同（事件被回填或撤回：未来确认）",
        )
    final = tables[subject.as_of_times[-1]]
    for item in final.events:
        own = as_of(item.event_time)
        require(
            item in own.events,
            f"事件 {item.event_id[:12]} 在截至它自己的 event_time "
            f"{item.event_time.isoformat()} 时还不存在（用到了之后的信息确认）",
        )


def check_hash_sensitivity(subject: EventSubject) -> None:
    """请求的任一输入变化 → request_hash 与 result_hash 都变化（同一实例，防缓存旧结果）。"""
    provider = subject.open()
    base = _detect(subject, provider, _request(subject))
    last = subject.as_of_times[-1]
    variants: dict[str, EventRequest] = {
        "as_of 提前": _request(subject, as_of=subject.as_of_times[0]),
        "as_of 推后": _request(subject, as_of=last + timedelta(days=1)),
    }
    if subject.inputs:
        variants["少一个输入点"] = _request(subject, inputs=subject.inputs[1:])
    if subject.upstream_events:
        variants["少一个上游事件"] = _request(subject, upstream_events=subject.upstream_events[1:])
    seen = {base.result_hash}
    for label, request in variants.items():
        result = _detect(subject, provider, request)
        require(result.result_hash not in seen, f"{label}：result_hash 必须随输入变化")
        seen.add(result.result_hash)
    require(
        _detect(subject, provider, _request(subject)).result_hash == base.result_hash,
        "变体之后重算原请求必须回到原 result_hash",
    )


def check_unsupported_event_is_refused(subject: EventSubject) -> None:
    """未声明的 event 版本或 spec hash → `UnsupportedEvent`，不得"尽量识别"。"""
    provider = subject.open()
    descriptor = _descriptor(provider)
    spec = subject.spec
    major = 999_999
    while descriptor.supports(
        Ref(kind=Kind.EVENT, name=spec.name, version=f"{major}.0.0"), spec.content_hash()
    ):
        major += 1
    for label, request in (
        (
            "未声明的 spec hash",
            _request(subject, spec_hash=content_hash({"not": spec.content_hash()})),
        ),
        (
            "未声明的版本",
            _request(subject, event=Ref(kind=Kind.EVENT, name=spec.name, version=f"{major}.0.0")),
        ),
    ):
        expect_error(UnsupportedEvent, f"detect({label})", partial(provider.detect, request))


def check_non_finite_numbers_are_refused(subject: EventSubject) -> None:
    """NaN / ±Infinity / 二进制浮点既不能进入输入点，也不能作为事件属性（ADR-0013）。"""
    result = _detect(subject, subject.open(), _request(subject))
    for bad in (Decimal("NaN"), Decimal("Infinity"), float("nan"), 1.5):
        if subject.inputs:
            payload = subject.inputs[0].model_dump()
            payload["value"] = bad
            expect_error(
                ValidationError, f"输入值 {bad!r}", partial(EventInputPoint.model_validate, payload)
            )
        sample = result.events[0] if result.events else None
        if sample is not None:
            expect_error(
                ValidationError,
                f"事件属性 {bad!r}",
                partial(
                    Event.build,
                    event=sample.event,
                    spec_hash=sample.spec_hash,
                    event_time=sample.event_time,
                    attributes={"x": bad},
                    inputs=subject.inputs[:1],
                    upstream=subject.upstream_events[:1],
                ),
            )


EVENT_CHECKS: tuple[EventCheck, ...] = (
    check_descriptor_declares_the_spec,
    check_fixture_has_events,
    check_determinism,
    check_only_the_visible_set_matters,
    check_causal_perturbation,
    check_point_in_time_consistency,
    check_hash_sensitivity,
    check_unsupported_event_is_refused,
    check_non_finite_numbers_are_refused,
)


class EventProviderContract:
    """pytest 复用入口：子类以 `Test*` 命名并提供 `event_subject` fixture。"""

    @pytest.fixture
    def event_subject(self) -> EventSubject:
        raise NotImplementedError("子类必须提供 event_subject fixture")

    @pytest.mark.parametrize("check", EVENT_CHECKS, ids=lambda check: check.__name__)
    def test_event_contract(self, event_subject: EventSubject, check: EventCheck) -> None:
        check(event_subject)
