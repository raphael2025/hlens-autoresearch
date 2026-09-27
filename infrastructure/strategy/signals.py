"""Adapters from answered upstream results to ``SignalObservation`` (see package docstring)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from core.contracts.event import Event
from core.contracts.feature import FeatureResult
from core.contracts.state import StateResult
from core.contracts.strategy import SignalObservation
from core.domain.base import Kind, Ref

__all__ = [
    "SignalAdapterError",
    "signals_from_events",
    "signals_from_features",
    "signals_from_states",
]


class SignalAdapterError(ValueError):
    """The upstream result cannot be turned into signals honestly; fail closed."""


def _require(ref: Ref, kind: Kind, role: str) -> None:
    if ref.kind is not kind:
        raise SignalAdapterError(f"{role} 必须是 kind={kind.value} 的引用，收到 {ref}")


def _check_known(latest: datetime, knowledge_time: datetime) -> None:
    if latest > knowledge_time:
        raise SignalAdapterError("knowledge_time 早于上游结果中的时刻：上游运行不可能在当时已知它")


def signals_from_features(
    result: FeatureResult, *, feature: Ref, instrument: str, knowledge_time: datetime
) -> tuple[SignalObservation, ...]:
    """每个 ``FeatureValue`` → 一条信号：``event_time = available_time = evaluation_time``。

    ``None`` 值原样保留（上游显式"不可计算"，不得填补）。
    """
    _require(feature, Kind.FEATURE, "feature")
    _check_known(result.values[-1].evaluation_time, knowledge_time)
    return tuple(
        SignalObservation(
            signal=feature,
            instrument=instrument,
            event_time=value.evaluation_time,
            available_time=value.evaluation_time,
            knowledge_time=knowledge_time,
            value=value.value,
        )
        for value in result.values
    )


def signals_from_states(
    result: StateResult, *, state: Ref, instrument: str, knowledge_time: datetime
) -> tuple[SignalObservation, ...]:
    """每个 ``StateValue`` → 一条信号（值为状态标签；``None`` 原样保留）。"""
    _require(state, Kind.STATE, "state")
    _check_known(result.values[-1].evaluation_time, knowledge_time)
    return tuple(
        SignalObservation(
            signal=state,
            instrument=instrument,
            event_time=value.evaluation_time,
            available_time=value.evaluation_time,
            knowledge_time=knowledge_time,
            value=value.state,
        )
        for value in result.values
    )


def signals_from_events(
    events: Iterable[Event],
    *,
    instrument: str,
    knowledge_time: datetime,
    value_attribute: str | None = None,
) -> tuple[SignalObservation, ...]:
    """每个 ``Event`` → 一条信号：``event_time`` 已是可观测时间，故 ``available_time`` 与之相同。

    ``value_attribute`` 为空时信号值为 ``True``（事件发生）；否则取该属性，缺失即拒绝（不填补）。
    """
    rows: list[SignalObservation] = []
    for event in events:
        _require(event.event, Kind.EVENT, "event")
        _check_known(event.event_time, knowledge_time)
        if value_attribute is None:
            value: object = True
        elif value_attribute in event.attributes:
            value = event.attributes[value_attribute]
        else:
            raise SignalAdapterError(f"事件 {event.event_id} 没有属性 {value_attribute!r}")
        rows.append(
            SignalObservation.model_validate(
                {
                    "signal": event.event,
                    "instrument": instrument,
                    "event_time": event.event_time,
                    "available_time": event.event_time,
                    "knowledge_time": knowledge_time,
                    "value": value,
                }
            )
        )
    return tuple(rows)
