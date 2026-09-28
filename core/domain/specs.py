"""研究对象规格：Instrument、Dataset、Feature / State / Event / Outcome / Strategy / Risk。

对应 docs/architecture/02-domain.md §2。只定义**契约**，不含任何计算实现。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from core.domain.base import (
    Contract,
    FrozenMapping,
    Kind,
    Ref,
    UtcDatetime,
    VersionedSpec,
    omit_none,
)

__all__ = [
    "ADR_0088_VERSION",
    "FEATURE_INPUT_KINDS",
    "FEATURE_INPUT_ZONES",
    "STRATEGY_SIGNAL_KINDS",
    "ConditionedStrategy",
    "DatasetRef",
    "EnsembleStrategy",
    "EventSpec",
    "FeatureSpec",
    "Instrument",
    "InstrumentType",
    "NegatedStrategy",
    "OutcomeSpec",
    "RepresentationSpec",
    "RiskPolicy",
    "StateSpec",
    "StrategyComposition",
    "StrategySpec",
    "Zone",
]

#: ADR-0088 的字段、模型与取值自契约 2.4.0 起（ADR-0052 §4：新增契约内容不得以旧版本发布）。
#: 2.0.0 ~ 2.3.0 信封携带它们即拒绝；不带它们的旧载荷照旧读取、哈希逐位不变。
ADR_0088_VERSION: Final = "2.4.0"


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


# ---------------------------------------------------------------------------------------
# 信息流白名单（ADR-0012 §D-23.2）
#
# 冻结的方向是 Canonical → Feature → State / Event → Research Dataset；Outcome 由 Canonical
# 计算，**永不回流**为 Feature / State / Event / Strategy 的输入（Constitution C-L2、
# 03-data.md §2）。下面的集合把这条方向落成**声明层面**的可执行反例。
#
# **诚实边界**：契约层只看得见直接引用**声明的类型**，这是必要条件，不等于信息流已验证。
# 传递依赖闭包的方向性、被引用对象是否真的是该类型、物化数据是否使用了 `available_time > t`
# 的行，分别属于 Registry、Runner 与验证服务的泄漏门（ADR-0012「运行时延期义务」）。
# `lineage` 是溯源而非计算输入，本轮明确不收紧：它仍可引用 Outcome。
# ---------------------------------------------------------------------------------------

#: `FeatureSpec.inputs` 允许的 `Ref.kind`。
FEATURE_INPUT_KINDS = frozenset({Kind.REPRESENTATION, Kind.FEATURE})
#: `FeatureSpec.inputs` 允许的 `DatasetRef.zone`。
FEATURE_INPUT_ZONES = frozenset({Zone.CANONICAL, Zone.FEATURE, Zone.RESEARCH_DATASET})
#: `StrategySpec.signals` 允许的 `Ref.kind`。
STRATEGY_SIGNAL_KINDS = frozenset({Kind.FEATURE, Kind.STATE, Kind.EVENT})


def _check_inputs(
    values: Iterable[Ref | DatasetRef],
    *,
    field: str,
    kinds: frozenset[Kind],
    zones: frozenset[Zone] = frozenset(),
) -> None:
    """按白名单校验直接输入；不在白名单内一律拒绝（ADR-0012 §D-23.2）。"""
    allowed_kinds = "、".join(sorted(kind.value for kind in kinds)) or "（无）"
    allowed_zones = "、".join(sorted(zone.value for zone in zones)) or "（无）"
    for value in values:
        if isinstance(value, DatasetRef):
            if value.zone not in zones:
                raise ValueError(
                    f"{field} 不接受 zone={value.zone.value} 的数据集；"
                    f"只允许 {allowed_zones}（ADR-0012 §D-23.2，Constitution C-L2）"
                )
        elif value.kind not in kinds:
            raise ValueError(
                f"{field} 不接受 {value} 这样的 {value.kind.value} 引用；"
                f"只允许 {allowed_kinds}（ADR-0012 §D-23.2，Constitution C-L2）"
            )


class RepresentationSpec(VersionedSpec):
    """原始数据到研究可用形式的变换（bar、tick 聚合、订单簿快照、成交量钟…）。

    必须声明自身的时间语义：`event_time` 的取值方式与可用延迟。
    """

    kind: Literal[Kind.REPRESENTATION] = Kind.REPRESENTATION
    method: str = Field(min_length=1)
    inputs: tuple[DatasetRef, ...] = Field(min_length=1)
    params: FrozenMapping[str, str | int | float | bool] = Field(
        default_factory=dict, validate_default=True
    )
    event_time_semantics: str = Field(min_length=1)
    available_lag: timedelta = timedelta(0)

    @model_validator(mode="after")
    def _non_negative_lag(self) -> RepresentationSpec:
        if self.available_lag < timedelta(0):
            raise ValueError("available_lag 不得为负（会构成未来函数）")
        return self


class FeatureSpec(VersionedSpec):
    """从 Representation 计算的时间序列量。

    `available_lag` 是防未来函数的核心声明：t 时刻只能使用
    `event_time + available_lag <= t` 的数据（03-data.md §4）。
    """

    kind: Literal[Kind.FEATURE] = Kind.FEATURE
    definition: str
    inputs: tuple[Ref | DatasetRef, ...] = Field(min_length=1)
    params: FrozenMapping[str, str | int | float | bool] = Field(
        default_factory=dict, validate_default=True
    )
    #: ADR-0023 §3 / §8：`available_lag` 是本规格的 `declared_latency`，只作用于历史轴：
    #: `derived.available_time = max(本规格自身的可用约束,
    #: max(input.available_time) + available_lag)`，`t` 即 `simulation_time`；
    #: 知识轴另受 `knowledge_time <= knowledge_cutoff` 约束（03-data.md §4.3 / §4.5）。
    #: 类文档字符串中的旧公式是已发布 Schema 的 `description`，为保持 Schema 逐字节不变
    #: 暂不改写，以本注释为准。
    available_lag: timedelta
    deterministic: bool = True

    @model_validator(mode="after")
    def _non_negative_lag(self) -> FeatureSpec:
        if self.available_lag < timedelta(0):
            raise ValueError("available_lag 不得为负（会构成未来函数）")
        return self

    @model_validator(mode="after")
    def _allowed_inputs(self) -> FeatureSpec:
        _check_inputs(
            self.inputs,
            field="FeatureSpec.inputs",
            kinds=FEATURE_INPUT_KINDS,
            zones=FEATURE_INPUT_ZONES,
        )
        return self


class StateSpec(VersionedSpec):
    """市场状态定义；t 时刻的状态只能依赖 <= t 的信息。"""

    kind: Literal[Kind.STATE] = Kind.STATE
    features: tuple[Ref, ...] = Field(min_length=1)
    state_space: tuple[str, ...] = Field(min_length=1)
    method: str
    training_window: timedelta | None = None
    seed: int | None = None

    @model_validator(mode="after")
    def _allowed_features(self) -> StateSpec:
        _check_inputs(self.features, field="StateSpec.features", kinds=frozenset({Kind.FEATURE}))
        return self

    @model_validator(mode="after")
    def _unique_state_space(self) -> StateSpec:
        if len(set(self.state_space)) != len(self.state_space):
            raise ValueError("state_space 的标签必须唯一，不得重复")
        return self

    @model_validator(mode="after")
    def _positive_training_window(self) -> StateSpec:
        if self.training_window is not None and self.training_window <= timedelta(0):
            raise ValueError("training_window 若提供则必须为正（None 表示不适用）")
        return self


class EventSpec(VersionedSpec):
    """离散事件 / 交互；事件时间必须是可被观测到的时间。"""

    # 类文档字符串是已发布 Schema 的 `description`，为保持 Schema 除新字段外逐字节不变，不改写。
    # ADR-0088 决策 1：`bar_spec` 非空时指向 `kind=representation`，声明事件判定所用的 bar 表示；
    # `None` = 未声明（与 2.4.0 之前相同），从载荷中省略，既有哈希逐位不变。
    _FIELDS_SINCE = {"bar_spec": ADR_0088_VERSION}

    kind: Literal[Kind.EVENT] = Kind.EVENT
    trigger: str
    features: tuple[Ref, ...] = ()
    states: tuple[Ref, ...] = ()
    observable_lag: timedelta = timedelta(0)
    bar_spec: Ref | None = Field(default=None, exclude_if=omit_none)

    @model_validator(mode="after")
    def _bar_spec_kind(self) -> EventSpec:
        if self.bar_spec is not None and self.bar_spec.kind is not Kind.REPRESENTATION:
            raise ValueError(
                f"EventSpec.bar_spec 必须指向 representation，收到 {self.bar_spec.kind.value}"
                "（ADR-0088 决策 1）"
            )
        return self

    @model_validator(mode="after")
    def _has_input(self) -> EventSpec:
        if not self.features and not self.states:
            raise ValueError("EventSpec 必须至少依赖一个 Feature 或 State")
        return self

    @model_validator(mode="after")
    def _allowed_inputs(self) -> EventSpec:
        _check_inputs(self.features, field="EventSpec.features", kinds=frozenset({Kind.FEATURE}))
        _check_inputs(self.states, field="EventSpec.states", kinds=frozenset({Kind.STATE}))
        return self

    @model_validator(mode="after")
    def _non_negative_observable_lag(self) -> EventSpec:
        if self.observable_lag < timedelta(0):
            raise ValueError("observable_lag 不得为负（会构成未来函数）")
        return self


class OutcomeSpec(VersionedSpec):
    """结果标签。Outcome 只能作为标签，**永不**作为输入（Constitution C-L2）。"""

    kind: Literal[Kind.OUTCOME] = Kind.OUTCOME
    horizon: timedelta
    label_definition: str

    @model_validator(mode="after")
    def _positive_horizon(self) -> OutcomeSpec:
        if self.horizon <= timedelta(0):
            raise ValueError("Outcome horizon 必须为正")
        return self


# ---------------------------------------------------------------------------------------
# 组合策略（ADR-0088 决策 2，契约 2.4.0）
#
# `StrategySpec.composition` 按 `type` 判别的三种组合。契约层只证明**本对象内**可检查的结构：
# 引用的 kind、成员去重与数量、不引用自身、`conditioned` 的门控状态出现在 `signals` 中。
# 被引用策略是否存在、嵌套组合与循环引用、成员的风险政策与适用标的是否一致（ADR-0069）、
# `signals` 是否覆盖 base / 成员需要的全部信号，都要解析被引用对象，属 Registry。
# 执行语义（门控、等权平均、取反）由后续 Provider 实现；取反**不是**验证负对照。
# ---------------------------------------------------------------------------------------


def _require_strategy_ref(value: Ref, field: str) -> None:
    if value.kind is not Kind.STRATEGY:
        raise ValueError(f"{field} 必须指向 strategy，收到 {value.kind.value}（ADR-0088 决策 2）")


class ConditionedStrategy(Contract):
    """门控组合：状态 `state` 等于 `state_value` 时持有 `base` 的目标仓位，否则空仓。

    未知或缺失的状态视为空仓；每个 (base, state, state_value) 计一个 trial（ADR-0088 决策 2）。
    """

    _MODEL_SINCE = ADR_0088_VERSION

    type: Literal["conditioned"] = "conditioned"
    base: Ref
    state: Ref
    state_value: str = Field(min_length=1)

    @model_validator(mode="after")
    def _kinds(self) -> ConditionedStrategy:
        _require_strategy_ref(self.base, "ConditionedStrategy.base")
        if self.state.kind is not Kind.STATE:
            raise ValueError(
                f"ConditionedStrategy.state 必须指向 state，收到 {self.state.kind.value}"
                "（ADR-0088 决策 2）"
            )
        return self

    def references(self) -> tuple[Ref, ...]:
        """被组合的策略引用。"""
        return (self.base,)


class EnsembleStrategy(Contract):
    """等权组合：各成员目标仓位的等权平均（`rule="equal_weight_mean"`）。

    成员至少 2 个、不得重复（按目标身份）；风险政策与适用标的一致由 Registry 核对（ADR-0069）。
    """

    _MODEL_SINCE = ADR_0088_VERSION

    type: Literal["ensemble"] = "ensemble"
    members: tuple[Ref, ...] = Field(min_length=2)
    rule: Literal["equal_weight_mean"] = "equal_weight_mean"

    @model_validator(mode="after")
    def _members(self) -> EnsembleStrategy:
        for member in self.members:
            _require_strategy_ref(member, "EnsembleStrategy.members")
        identities = [member.target_identity() for member in self.members]
        if len(set(identities)) != len(identities):
            raise ValueError("EnsembleStrategy.members 不得重复（ADR-0088 决策 2）")
        return self

    def references(self) -> tuple[Ref, ...]:
        """被组合的策略引用。"""
        return self.members


class NegatedStrategy(Contract):
    """取反组合：`base` 的目标仓位取反。

    **不**作为验证负对照（负对照由验证层定义）；现货下的执行受做空成本缺口（ST-4）限制，
    由 Provider fail closed（ADR-0088 决策 2）。
    """

    _MODEL_SINCE = ADR_0088_VERSION

    type: Literal["negated"] = "negated"
    base: Ref

    @model_validator(mode="after")
    def _kinds(self) -> NegatedStrategy:
        _require_strategy_ref(self.base, "NegatedStrategy.base")
        return self

    def references(self) -> tuple[Ref, ...]:
        """被组合的策略引用。"""
        return (self.base,)


#: `StrategySpec.composition` 的类型：按 `type` 判别的三种组合（ADR-0088 决策 2）。
StrategyComposition = Annotated[
    ConditionedStrategy | EnsembleStrategy | NegatedStrategy, Field(discriminator="type")
]


class StrategySpec(VersionedSpec):
    """信号 → 目标仓位的声明式规格。

    参数空间必须声明，用于多重检验的尝试次数计算（Constitution C-T1）。
    """

    # 类文档字符串是已发布 Schema 的 `description`，为保持 Schema 除新字段外逐字节不变，不改写。
    # ADR-0088 决策 2：`composition` 非空时本策略是组合策略；`None` = 普通策略（同 2.4.0 之前），
    # 从载荷中省略，既有哈希逐位不变。
    _FIELDS_SINCE = {"composition": ADR_0088_VERSION}

    kind: Literal[Kind.STRATEGY] = Kind.STRATEGY
    signals: tuple[Ref, ...] = Field(min_length=1)
    params: FrozenMapping[str, str | int | float | bool] = Field(
        default_factory=dict, validate_default=True
    )
    param_search_space: FrozenMapping[str, tuple[str | int | float | bool, ...]] = Field(
        default_factory=dict, validate_default=True
    )
    risk_policy: Ref | None = None
    applicable_instruments: tuple[Instrument, ...] = ()
    composition: StrategyComposition | None = Field(default=None, exclude_if=omit_none)

    @model_validator(mode="after")
    def _allowed_signals(self) -> StrategySpec:
        """信号只能是 Feature / State / Event；Outcome 因此也被排除（Constitution C-L2）。"""
        _check_inputs(self.signals, field="StrategySpec.signals", kinds=STRATEGY_SIGNAL_KINDS)
        return self

    @model_validator(mode="after")
    def _composition_shape(self) -> StrategySpec:
        """组合不得引用自身；`conditioned` 的门控状态必须出现在 `signals` 中（ADR-0088 决策 2）。"""
        composition = self.composition
        if composition is None:
            return self
        own = (Kind.STRATEGY, self.name, self.version)
        if any(item.target_identity() == own for item in composition.references()):
            raise ValueError(f"组合策略不得引用自身：{self.ref}（ADR-0088 决策 2）")
        if isinstance(composition, ConditionedStrategy):
            identities = {signal.target_identity() for signal in self.signals}
            if composition.state.target_identity() not in identities:
                raise ValueError(
                    f"conditioned 组合的门控状态 {composition.state} 必须出现在 signals 中"
                    "（ADR-0088 决策 2）"
                )
        return self

    @model_validator(mode="after")
    def _risk_policy_kind(self) -> StrategySpec:
        if self.risk_policy is not None and self.risk_policy.kind is not Kind.RISK:
            raise ValueError(
                f"StrategySpec.risk_policy 必须指向 risk，收到 {self.risk_policy.kind.value}"
            )
        return self


class RiskPolicy(VersionedSpec):
    """仓位、止损、敞口、杠杆等约束；独立于策略版本化。"""

    kind: Literal[Kind.RISK] = Kind.RISK
    rules: tuple[str, ...] = Field(min_length=1)
    params: FrozenMapping[str, str | int | float | bool] = Field(
        default_factory=dict, validate_default=True
    )
