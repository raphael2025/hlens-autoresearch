"""研究对象规格：Instrument、Dataset、Feature / State / Event / Outcome / Strategy / Risk。

对应 docs/architecture/02-domain.md §2。只定义**契约**，不含任何计算实现。
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import Contract, Kind, Ref, UtcDatetime, VersionedSpec

__all__ = [
    "DatasetRef",
    "EventSpec",
    "FeatureSpec",
    "Instrument",
    "InstrumentType",
    "OutcomeSpec",
    "RiskPolicy",
    "StateSpec",
    "StrategySpec",
    "Zone",
]


class InstrumentType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURE = "future"
    OPTION = "option"


class Zone(StrEnum):
    """数据分层（03-data.md §3）。"""

    RAW = "raw"
    CANONICAL = "canonical"
    FEATURE = "feature"
    STATE = "state"
    EVENT = "event"
    OUTCOME = "outcome"
    RESEARCH_DATASET = "research_dataset"


class Instrument(Contract):
    """可交易标的。跨 venue 的符号必须规范化映射。"""

    venue: str
    symbol: str
    instrument_type: InstrumentType
    base: str
    quote: str


class DatasetRef(Contract):
    """某个 Zone 的不可变数据快照引用（03-data.md §3）。"""

    zone: Zone
    table: str
    snapshot_id: str = Field(min_length=1)
    time_range_start: UtcDatetime
    time_range_end: UtcDatetime

    @model_validator(mode="after")
    def _ordered_range(self) -> DatasetRef:
        if self.time_range_end < self.time_range_start:
            raise ValueError("time_range_end 不得早于 time_range_start")
        return self


class FeatureSpec(VersionedSpec):
    """从 Representation 计算的时间序列量。

    `available_lag` 是防未来函数的核心声明：t 时刻只能使用
    `event_time + available_lag <= t` 的数据（03-data.md §4）。
    """

    kind: Kind = Kind.FEATURE
    definition: str
    inputs: tuple[Ref | DatasetRef, ...]
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    available_lag: timedelta
    deterministic: bool = True

    @model_validator(mode="after")
    def _non_negative_lag(self) -> FeatureSpec:
        if self.available_lag < timedelta(0):
            raise ValueError("available_lag 不得为负（会构成未来函数）")
        return self


class StateSpec(VersionedSpec):
    """市场状态定义；t 时刻的状态只能依赖 <= t 的信息。"""

    kind: Kind = Kind.STATE
    features: tuple[Ref, ...] = Field(min_length=1)
    state_space: tuple[str, ...] = Field(min_length=1)
    method: str
    training_window: timedelta | None = None
    seed: int | None = None


class EventSpec(VersionedSpec):
    """离散事件 / 交互；事件时间必须是可被观测到的时间。"""

    kind: Kind = Kind.EVENT
    trigger: str
    features: tuple[Ref, ...] = ()
    states: tuple[Ref, ...] = ()
    observable_lag: timedelta = timedelta(0)

    @model_validator(mode="after")
    def _has_input(self) -> EventSpec:
        if not self.features and not self.states:
            raise ValueError("EventSpec 必须至少依赖一个 Feature 或 State")
        return self


class OutcomeSpec(VersionedSpec):
    """结果标签。Outcome 只能作为标签，**永不**作为输入（Constitution C-L2）。"""

    kind: Kind = Kind.OUTCOME
    horizon: timedelta
    label_definition: str

    @model_validator(mode="after")
    def _positive_horizon(self) -> OutcomeSpec:
        if self.horizon <= timedelta(0):
            raise ValueError("Outcome horizon 必须为正")
        return self


class StrategySpec(VersionedSpec):
    """信号 → 目标仓位的声明式规格。

    参数空间必须声明，用于多重检验的尝试次数计算（Constitution C-T1）。
    """

    kind: Kind = Kind.STRATEGY
    signals: tuple[Ref, ...] = Field(min_length=1)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    param_search_space: dict[str, tuple[str | int | float | bool, ...]] = Field(default_factory=dict)
    risk_policy: Ref | None = None
    applicable_instruments: tuple[Instrument, ...] = ()

    @model_validator(mode="after")
    def _no_outcome_input(self) -> StrategySpec:
        if any(ref.kind is Kind.OUTCOME for ref in self.signals):
            raise ValueError("Outcome 不得作为策略输入（Constitution C-L2）")
        return self


class RiskPolicy(VersionedSpec):
    """仓位、止损、敞口、杠杆等约束；独立于策略版本化。"""

    kind: Kind = Kind.RISK
    rules: tuple[str, ...] = Field(min_length=1)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
