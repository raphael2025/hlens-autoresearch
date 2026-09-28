"""`StrategyProvider` / `RiskProvider` / `BacktestProvider`：Protocol 与 DTO（ADR-0038；Phase 5）。

对应 docs/architecture/05-plugin.md §3 的三行概念语义：

- `StrategyProvider.target_positions`：`StrategyRequest`（信号观察 + 决策时刻）
  → `StrategyResult`（目标仓位）；
- `RiskProvider.constrain`：`RiskRequest`（一个决策时刻的目标仓位 + 组合状态 + 风控信号）
  → `RiskResult`（约束后的仓位）；
- `BacktestProvider.run`：`BacktestRequest`（目标仓位 + 价格 bar + 成本模型）
  → `BacktestResult`（成交、权益、PnL）。

本模块只定义可执行 Protocol、可序列化 DTO 与它们在契约层可证明的不变量；**不含任何实现**
（回测器在 `plugins/backtest/`，研究期策略与风控在 `research/strategies/`）。以
`core/contracts/feature.py`（ADR-0030）为模板。

**确定性与数值**：三类 Provider 都必须确定性（descriptor 的 `deterministic` 只能为 `true`）；
全部数值一律 `Decimal` / `int` / `bool`，浮点在 Python 与 JSON 两条入口都被拒绝，NaN / ±Infinity
同样被拒绝（ADR-0013）。

**可见集合**：决策时刻 `t` 的目标仓位只能是 `available_time <= t` 的信号观察的函数；同一
`(signal, instrument, event_time)` 有多条观察时只取 `available_time` 最大的一条（PIT 替换）。
`knowledge_time <= knowledge_cutoff` 由请求构造保证。`RiskRequest` 更严格：它只描述**一个**决策
时刻，任何 `available_time > decision_time` 的信号在构造时即被拒绝。信号只能是 Feature / State /
Event（`STRATEGY_SIGNAL_KINDS`）——Outcome 永不作为输入（Constitution C-L2）。

**执行**：v1 的执行模型 `next_bar_open`：决策时刻 `t` 的目标在该标的**第一根**
`interval_start >= t` 的 bar 的开盘价成交（`Fill.fill_time >= Fill.decision_time` 是构造不变量）。
ADR-0054 additive 增加 `next_bar_open_participation`：目标在同一执行 bar 按执行前权益
定量为**目标变化量**，按成交量上限没成交完的**剩余量**在该标的之后的 bar 开盘继续成交，
直到全部成交、被同一标的更晚的目标取代或数据结束；每个目标的结转记录在
`BacktestResult.remainders`（`FillRemainder`），`check_answers` 按 descriptor 的执行模型分支。
新字段（`PriceBar.volume`、`BacktestResult.remainders`）缺省时从载荷中省略，既有哈希逐位不变。
回测**只是模拟**：descriptor 的 `simulation_only` 只能为 `true`，本契约没有任何下单、账户或
交易所端点。

**诚实边界**：DTO 只证明形状与请求 / 结果之间可局部检查的关系（一一对应、哈希自洽、输入时间不晚于
决策时刻、成交不早于决策时刻、费用合计）。目标仓位是否只依赖可见集合、风控是否真的执行了其声明的
规则、PnL 是否按成本模型正确计算，由各结果的 `check_answers` 与 `tests/contract_suites/{strategy,
risk,backtest}.py` 对具体实现检查，契约层不能证明。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal, localcontext
from itertools import pairwise
from typing import Annotated, Final, Literal, Protocol

from pydantic import BeforeValidator, Field, field_validator, model_validator

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
from core.domain.specs import ADR_0088_VERSION, STRATEGY_SIGNAL_KINDS

__all__ = [
    "RISK_REF_KEY_PATTERN",
    "STRATEGY_REF_KEY_PATTERN",
    "BacktestCostModel",
    "BacktestInputError",
    "BacktestProvider",
    "BacktestProviderDescriptor",
    "BacktestProviderError",
    "BacktestRequest",
    "BacktestResult",
    "ConstrainedPosition",
    "EquityPoint",
    "ExecutionModelName",
    "Fill",
    "FillRemainder",
    "FillRemainderEnd",
    "PortfolioState",
    "PriceBar",
    "RiskInputError",
    "RiskProvider",
    "RiskProviderDescriptor",
    "RiskProviderError",
    "RiskRequest",
    "RiskResult",
    "SignalObservation",
    "StrategyInputError",
    "StrategyProvider",
    "StrategyProviderDescriptor",
    "StrategyProviderError",
    "StrategyRequest",
    "StrategyResult",
    "TargetPosition",
    "UnsupportedRiskPolicy",
    "UnsupportedStrategy",
    "execution_bar",
]

#: `StrategyProviderDescriptor.supported_strategies` 的键：`strategy:name@semver`。
STRATEGY_REF_KEY_PATTERN = rf"^strategy:{PLUGIN_KEY_PATTERN.removeprefix('^')}"
#: `RiskProviderDescriptor.supported_policies` 的键：`risk:name@semver`。
RISK_REF_KEY_PATTERN = rf"^risk:{PLUGIN_KEY_PATTERN.removeprefix('^')}"

NonEmptyStr = Annotated[str, Field(min_length=1)]
ParamName = Annotated[str, Field(pattern=NAME_PATTERN)]
RuleName = Annotated[str, Field(pattern=NAME_PATTERN)]
StrategyRefKey = Annotated[str, Field(pattern=STRATEGY_REF_KEY_PATTERN)]
RiskRefKey = Annotated[str, Field(pattern=RISK_REF_KEY_PATTERN)]

#: 契约内部合计（费用、滑点）用的精度：远大于实现输出的有效位数，合计在其中是精确的。
_SUM_PRECISION = 120

#: 回测执行模型（ADR-0038 / ADR-0054）：`next_bar_open` = 每个目标在其执行 bar 一次成交；
#: `next_bar_open_participation` = 按成交量上限分 bar 成交，剩余量结转到同一标的之后的 bar。
ExecutionModelName = Literal["next_bar_open", "next_bar_open_participation"]
#: 一个目标的结转在何处结束（ADR-0054 §3）。
FillRemainderEnd = Literal["filled", "superseded", "end_of_data"]


#: ADR-0054 的字段、模型与执行模型字面量自契约 2.1.0 起（ADR-0052 §4、Codex K3：新增契约内容
#: 不得以 2.0.0 发布）。2.0.0 信封携带它们即拒绝；不带它们的 2.0.0 载荷照旧读取、哈希逐位不变。
ADR_0054_VERSION: Final = "2.1.0"


def _omit_none(value: object) -> bool:
    """ADR-0054：可选字段为 `None` 时从载荷中省略，使既有载荷与哈希逐位不变。"""
    return value is None


def _omit_empty(value: object) -> bool:
    """ADR-0054：可选序列为空时从载荷中省略，使既有载荷与哈希逐位不变。"""
    return value == ()


# ---------------------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------------------


class StrategyProviderError(Exception):
    """StrategyProvider 契约错误的基类。"""


class UnsupportedStrategy(StrategyProviderError):
    """请求的 strategy `name@version` / `spec_hash` 未被声明支持，或参数不在声明的参数空间内。"""


class StrategyInputError(StrategyProviderError):
    """信号观察不符合该策略的输入约定；fail closed。"""


class RiskProviderError(Exception):
    """RiskProvider 契约错误的基类。"""


class UnsupportedRiskPolicy(RiskProviderError):
    """请求的 risk `name@version` / `policy_hash` 未被声明支持。"""


class RiskInputError(RiskProviderError):
    """风控输入不符合该策略的约定；fail closed。"""


class BacktestProviderError(Exception):
    """BacktestProvider 契约错误的基类。"""


class BacktestInputError(BacktestProviderError):
    """回测输入无法按执行模型模拟（例如 bar 网格不一致）；fail closed。"""


# ---------------------------------------------------------------------------------------
# 数值
# ---------------------------------------------------------------------------------------


def _finite_decimal(value: object) -> object:
    """只接受有限 `Decimal` / `int`（或 JSON 入口的十进制文本）；拒绝浮点与 NaN / ±Infinity。"""
    if isinstance(value, float):
        raise ValueError("不接受浮点数：数值一律以 Decimal 或 int 传入（ADR-0013）")
    if isinstance(value, bool):
        raise ValueError("布尔值不是数量")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("数值必须有限（ADR-0013）")
    return value


FiniteDecimal = Annotated[Decimal, BeforeValidator(_finite_decimal), Field(allow_inf_nan=False)]
NonNegativeDecimal = Annotated[
    Decimal, BeforeValidator(_finite_decimal), Field(ge=0, allow_inf_nan=False)
]
PositiveDecimal = Annotated[
    Decimal, BeforeValidator(_finite_decimal), Field(gt=0, allow_inf_nan=False)
]
#: 成本率：`[0, 1)` 的小数（例如 `0.001` = 10bp）。
Rate = Annotated[Decimal, BeforeValidator(_finite_decimal), Field(ge=0, lt=1, allow_inf_nan=False)]


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _SUM_PRECISION
        return sum(values, Decimal(0))


def _strictly_ascending(values: Sequence[datetime], label: str) -> None:
    if any(later <= earlier for earlier, later in pairwise(values)):
        raise ValueError(f"{label} 必须严格升序（因而唯一）")


def _inputs_invariant(
    inputs_used: int, latest: datetime | None, decision_time: datetime, label: str
) -> None:
    if (inputs_used == 0) != (latest is None):
        raise ValueError(f"{label}：inputs_used 为 0 当且仅当 latest_input_available_time 为空")
    if latest is not None and latest > decision_time:
        raise ValueError(f"{label}：latest_input_available_time 不得晚于决策时刻（未来函数）")


def _signal_order(item: SignalObservation) -> tuple[datetime, str, str, datetime]:
    return (item.available_time, str(item.signal), item.instrument, item.event_time)


def _canonical_signals(value: tuple[SignalObservation, ...]) -> tuple[SignalObservation, ...]:
    ordered = tuple(sorted(value, key=_signal_order))
    for earlier, later in pairwise(ordered):
        if _signal_order(earlier) == _signal_order(later):
            raise ValueError(
                f"signals 中 (available_time, signal, instrument, event_time) 重复："
                f"{later.signal} / {later.instrument} @ {later.available_time.isoformat()}"
            )
    return ordered


def _visible(
    signals: Iterable[SignalObservation], evaluation_time: datetime
) -> tuple[SignalObservation, ...]:
    """`available_time <= t` 的信号，同一 `(signal, instrument, event_time)` 只取最晚可用的一条。"""
    if not isinstance(evaluation_time, datetime) or evaluation_time.tzinfo is None:
        raise ValueError("evaluation_time 必须是带时区的 UTC 时间")
    latest: dict[tuple[str, str, datetime], SignalObservation] = {}
    for item in signals:  # 已按 available_time 升序
        if item.available_time > evaluation_time:
            break
        latest[(str(item.signal), item.instrument, item.event_time)] = item
    return tuple(sorted(latest.values(), key=_signal_order))


# ---------------------------------------------------------------------------------------
# StrategyProvider
# ---------------------------------------------------------------------------------------


class SignalObservation(Contract):
    """交给 Strategy / Risk Provider 的一条信号观察：某个 Feature / State / Event 在某标的上的值。

    - `signal`：只能是 `feature` / `state` / `event` 引用（Outcome 永不作为输入，C-L2）；
    - `event_time`：信号所描述的时刻；`available_time >= event_time` 是它可被使用的最早时刻；
    - `value`：`None` 表示上游显式"不可计算"，不得被填补。
    """

    signal: Ref
    instrument: NonEmptyStr
    event_time: UtcDatetime
    available_time: UtcDatetime
    knowledge_time: UtcDatetime
    value: ObservationScalar | None

    @model_validator(mode="after")
    def _invariants(self) -> SignalObservation:
        if self.signal.kind not in STRATEGY_SIGNAL_KINDS:
            raise ValueError(
                f"信号只能是 feature / state / event，收到 {self.signal}（Constitution C-L2）"
            )
        if self.available_time < self.event_time:
            raise ValueError("available_time 不得早于 event_time")
        return self


class StrategyRequest(Contract):
    """一次目标仓位计算请求。

    - `strategy`（`kind=strategy`）+ `spec_hash`：Provider 声明支持的 `StrategySpec`；
    - `params`：本次试验的参数点；Provider 必须拒绝不在该规格 `param_search_space` 内的参数
      （参数空间是 trial count 的来源，C-T1）；
    - `instruments`：非空、唯一、升序；`decision_times`：非空、严格升序；
    - `signals`：按 `(available_time, signal, instrument, event_time)` 规范排序且不重复；每条的
      `instrument` 属于 `instruments`，`knowledge_time <= knowledge_cutoff`。
    """

    strategy: Ref
    spec_hash: ContentHash
    params: FrozenMapping[ParamName, ObservationScalar] = Field(
        default_factory=dict, validate_default=True
    )
    instruments: tuple[NonEmptyStr, ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    knowledge_cutoff: UtcDatetime
    decision_times: tuple[UtcDatetime, ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    signals: tuple[SignalObservation, ...]

    @field_validator("instruments")
    @classmethod
    def _sorted_instruments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(set(value)):
            raise ValueError("instruments 必须唯一且升序")
        return value

    @field_validator("decision_times")
    @classmethod
    def _ascending(cls, value: tuple[datetime, ...]) -> tuple[datetime, ...]:
        _strictly_ascending(value, "decision_times")
        return value

    @field_validator("signals")
    @classmethod
    def _canonical(cls, value: tuple[SignalObservation, ...]) -> tuple[SignalObservation, ...]:
        return _canonical_signals(value)

    @model_validator(mode="after")
    def _request_invariants(self) -> StrategyRequest:
        if self.strategy.kind is not Kind.STRATEGY:
            raise ValueError(f"strategy 必须是 kind=strategy 的引用，收到 {self.strategy}")
        known = set(self.instruments)
        for item in self.signals:
            if item.instrument not in known:
                raise ValueError(f"信号的 instrument {item.instrument!r} 不在 instruments 中")
            if item.knowledge_time > self.knowledge_cutoff:
                raise ValueError(f"信号 {item.signal} 的 knowledge_time 晚于 knowledge_cutoff")
        return self

    def visible_at(self, decision_time: datetime) -> tuple[SignalObservation, ...]:
        """`decision_time` 的可见集合（见模块文档），按规范顺序返回。"""
        return _visible(self.signals, decision_time)


class TargetPosition(Contract):
    """一个决策时刻、一个标的的目标仓位。

    - `target_weight`：带符号的目标权重（名义敞口 / 组合权益；`1` = 满仓多头，`-1` = 满仓空头）；
    - `inputs_used` / `latest_input_available_time`：计算它实际使用的观察条数与其中最晚的
      `available_time`；为 0 当且仅当后者为空，此时目标只能是 `0`（没有信息就不持仓，不填补）。
    """

    decision_time: UtcDatetime
    instrument: NonEmptyStr
    target_weight: FiniteDecimal
    inputs_used: int = Field(ge=0, strict=True)
    latest_input_available_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def _invariants(self) -> TargetPosition:
        _inputs_invariant(
            self.inputs_used,
            self.latest_input_available_time,
            self.decision_time,
            "TargetPosition",
        )
        if self.inputs_used == 0 and self.target_weight != 0:
            raise ValueError("inputs_used 为 0 时 target_weight 必须为 0（没有信息就不持仓）")
        return self


def _position_order(item: TargetPosition) -> tuple[datetime, str]:
    return (item.decision_time, item.instrument)


class StrategyProviderDescriptor(Contract):
    """StrategyProvider 的身份与能力声明。

    `supported_strategies`：非空，`strategy:name@semver` → 该 `StrategySpec` 的内容哈希（规格的
    信号、默认参数与参数空间由哈希绑定）。实例生命周期内不变。
    """

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    supported_strategies: FrozenMapping[StrategyRefKey, ContentHash]

    @field_validator("supported_strategies")
    @classmethod
    def _non_empty(cls, value: FrozenMapping[str, str]) -> FrozenMapping[str, str]:
        if not value:
            raise ValueError("supported_strategies 不得为空")
        return value

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"

    def supports(self, strategy: Ref, spec_hash: str) -> bool:
        return self.supported_strategies.get(str(strategy)) == spec_hash


def _hash_of(**parts: object) -> str:
    return content_hash(parts)


def _dump(items: Iterable[Contract]) -> list[object]:
    return [item.model_dump(mode="json") for item in items]


class StrategyResult(Contract):
    """一次 `target_positions` 的结果。

    `positions` 按 `(decision_time, instrument)` 严格升序；与请求的 `decision_times × instruments`
    一一对应（`check_answers`）；`result_hash` 构造时复核。
    """

    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    positions: tuple[TargetPosition, ...] = Field(min_length=1)
    result_hash: ContentHash

    @field_validator("positions")
    @classmethod
    def _ascending(cls, value: tuple[TargetPosition, ...]) -> tuple[TargetPosition, ...]:
        keys = [_position_order(item) for item in value]
        if any(later <= earlier for earlier, later in pairwise(keys)):
            raise ValueError("positions 必须按 (decision_time, instrument) 严格升序")
        return value

    @model_validator(mode="after")
    def _self_consistent_hash(self) -> StrategyResult:
        if self.result_hash != self._expected_hash():
            raise ValueError("result_hash 与结果内容不符")
        return self

    def _expected_hash(self) -> str:
        return _hash_of(
            request_hash=self.request_hash,
            provider=self.provider,
            provider_hash=self.provider_hash,
            positions=_dump(self.positions),
        )

    @classmethod
    def build(
        cls,
        request: StrategyRequest,
        descriptor: StrategyProviderDescriptor,
        positions: Iterable[TargetPosition],
    ) -> StrategyResult:
        items = tuple(sorted(positions, key=_position_order))
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            positions=items,
            result_hash=_hash_of(
                request_hash=request_hash,
                provider=descriptor.plugin_key,
                provider_hash=provider_hash,
                positions=_dump(items),
            ),
        )

    def at(self, decision_time: datetime) -> tuple[TargetPosition, ...]:
        return tuple(item for item in self.positions if item.decision_time == decision_time)

    def check_answers(
        self, request: StrategyRequest, descriptor: StrategyProviderDescriptor
    ) -> None:
        """对照请求检查本结果；不符抛 `ValueError`。

        请求哈希与 Provider 身份一致；仓位与 `decision_times × instruments` 一一对应；每个仓位的
        `latest_input_available_time` 是该时刻可见集合中某条观察的 `available_time`，`inputs_used`
        不超过可见集合的大小。
        """
        if self.request_hash != request.content_hash():
            raise ValueError("request_hash 与请求不符")
        if (self.provider, self.provider_hash) != (
            descriptor.plugin_key,
            descriptor.content_hash(),
        ):
            raise ValueError("provider / provider_hash 与 descriptor 不符")
        expected = [(t, name) for t in request.decision_times for name in request.instruments]
        if [_position_order(item) for item in self.positions] != expected:
            raise ValueError("positions 必须与 decision_times × instruments 一一对应")
        for item in self.positions:
            visible = request.visible_at(item.decision_time)
            if item.inputs_used > len(visible):
                raise ValueError(
                    f"{item.decision_time.isoformat()} 的 inputs_used 超过可见观察条数"
                )
            latest = item.latest_input_available_time
            if latest is not None and latest not in {obs.available_time for obs in visible}:
                raise ValueError(
                    f"{item.decision_time.isoformat()} / {item.instrument} 的 "
                    "latest_input_available_time 不属于该时刻可见的观察"
                )


class StrategyProvider(Protocol):
    """信号 → 目标仓位（ADR-0038）。语义见模块文档。"""

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        """Provider 身份与支持的策略规格；实例生命周期内不变。"""
        ...

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        """每个决策时刻 × 标的的目标仓位；不支持的规格或参数 → `UnsupportedStrategy`。"""
        ...


# ---------------------------------------------------------------------------------------
# RiskProvider
# ---------------------------------------------------------------------------------------


class PortfolioState(Contract):
    """风控看到的组合状态（`as_of` 时刻）。

    - `current_weights`：各标的当前（或上一次约束后的）权重；缺失即 0；
    - `equity`：已知时为组合权益（> 0）；在模拟之前运行风控时为 `None`——依赖路径的风控规则
      （回撤、权益止损）必须在 `equity` 为 `None` 时以 `RiskInputError` fail closed。
    """

    # 类文档字符串是已发布 Schema 的 `description`，为保持 Schema 除新字段外逐字节不变，不改写。
    # ADR-0088 决策 3：`peak_equity` 是截至 `as_of` 已实现权益路径的峰值，由回测 / 执行层提供，
    # 风控不得自己记忆峰值；非空时 `equity` 必须也非空且 `peak_equity >= equity`。`None` 时从载荷中
    # 省略，既有哈希逐位不变；`drawdown_control` 在 `equity` 或 `peak_equity` 缺失时 fail closed。
    _FIELDS_SINCE = {"peak_equity": ADR_0088_VERSION}

    as_of: UtcDatetime
    current_weights: FrozenMapping[NonEmptyStr, FiniteDecimal] = Field(
        default_factory=dict, validate_default=True
    )
    equity: PositiveDecimal | None = None
    peak_equity: PositiveDecimal | None = Field(default=None, exclude_if=_omit_none)

    @model_validator(mode="after")
    def _peak_not_below_equity(self) -> PortfolioState:
        if self.peak_equity is None:
            return self
        if self.equity is None:
            raise ValueError("peak_equity 非空时 equity 也必须非空（ADR-0088 决策 3）")
        if self.peak_equity < self.equity:
            raise ValueError("peak_equity 不得小于 equity（ADR-0088 决策 3）")
        return self


class RiskRequest(Contract):
    """一个决策时刻的风控请求。

    - `policy`（`kind=risk`）+ `policy_hash`：Provider 声明支持的 `RiskPolicy`；
    - `targets`：非空，全部属于 `decision_time`，按 `instrument` 唯一升序；
    - `portfolio.as_of <= decision_time`；
    - `signals`（例如波动率估计）：规范排序、不重复，**每条 `available_time <= decision_time`**
      且 `knowledge_time <= knowledge_cutoff`，否则构造即拒绝（结构性无未来函数）。
    """

    policy: Ref
    policy_hash: ContentHash
    decision_time: UtcDatetime
    knowledge_cutoff: UtcDatetime
    targets: tuple[TargetPosition, ...] = Field(min_length=1)
    portfolio: PortfolioState
    signals: tuple[SignalObservation, ...] = ()

    @field_validator("signals")
    @classmethod
    def _canonical(cls, value: tuple[SignalObservation, ...]) -> tuple[SignalObservation, ...]:
        return _canonical_signals(value)

    @model_validator(mode="after")
    def _request_invariants(self) -> RiskRequest:
        if self.policy.kind is not Kind.RISK:
            raise ValueError(f"policy 必须是 kind=risk 的引用，收到 {self.policy}")
        names = [item.instrument for item in self.targets]
        if names != sorted(set(names)):
            raise ValueError("targets 必须按 instrument 唯一升序")
        if any(item.decision_time != self.decision_time for item in self.targets):
            raise ValueError("targets 必须全部属于 decision_time")
        if self.portfolio.as_of > self.decision_time:
            raise ValueError("portfolio.as_of 不得晚于 decision_time（未来函数）")
        for item in self.signals:
            if item.available_time > self.decision_time:
                raise ValueError(
                    f"信号 {item.signal} / {item.instrument} 在 decision_time 之后才可用"
                    "（未来函数）"
                )
            if item.knowledge_time > self.knowledge_cutoff:
                raise ValueError(f"信号 {item.signal} 的 knowledge_time 晚于 knowledge_cutoff")
        return self

    def visible(self) -> tuple[SignalObservation, ...]:
        """可见集合（同键取最晚可用的一条）。"""
        return _visible(self.signals, self.decision_time)


class ConstrainedPosition(Contract):
    """一个标的约束后的仓位。

    - `binding_rules`：实际改变了仓位的规则名（`RiskPolicy.rules` 的机读名），唯一升序；为空当且仅当
      `constrained_weight == requested_weight`（任何调整都必须有名可查）；
    - `inputs_used` / `latest_input_available_time`：风控侧（信号）实际使用的观察。
    """

    instrument: NonEmptyStr
    requested_weight: FiniteDecimal
    constrained_weight: FiniteDecimal
    binding_rules: tuple[RuleName, ...] = ()
    inputs_used: int = Field(ge=0, strict=True)
    latest_input_available_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def _invariants(self) -> ConstrainedPosition:
        if list(self.binding_rules) != sorted(set(self.binding_rules)):
            raise ValueError("binding_rules 必须唯一且升序")
        if (not self.binding_rules) != (self.constrained_weight == self.requested_weight):
            raise ValueError("binding_rules 为空当且仅当仓位未被调整（任何调整都必须有名可查）")
        if (self.inputs_used == 0) != (self.latest_input_available_time is None):
            raise ValueError("inputs_used 为 0 当且仅当 latest_input_available_time 为空")
        return self

    def as_target(self, upstream: TargetPosition) -> TargetPosition:
        """把约束后的仓位写回为 `TargetPosition`（合并上游与风控两侧的输入溯源），供回测使用。"""
        if upstream.instrument != self.instrument:
            raise ValueError("upstream 与约束仓位不是同一标的")
        times = [
            t
            for t in (upstream.latest_input_available_time, self.latest_input_available_time)
            if t is not None
        ]
        inputs = upstream.inputs_used + self.inputs_used
        return TargetPosition(
            decision_time=upstream.decision_time,
            instrument=self.instrument,
            target_weight=self.constrained_weight if inputs else Decimal(0),
            inputs_used=inputs,
            latest_input_available_time=max(times) if times else None,
        )


class RiskProviderDescriptor(Contract):
    """RiskProvider 的身份与能力声明：`risk:name@semver` → 该 `RiskPolicy` 的内容哈希。"""

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    supported_policies: FrozenMapping[RiskRefKey, ContentHash]

    @field_validator("supported_policies")
    @classmethod
    def _non_empty(cls, value: FrozenMapping[str, str]) -> FrozenMapping[str, str]:
        if not value:
            raise ValueError("supported_policies 不得为空")
        return value

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"

    def supports(self, policy: Ref, policy_hash: str) -> bool:
        return self.supported_policies.get(str(policy)) == policy_hash


class RiskResult(Contract):
    """一次 `constrain` 的结果：`positions` 按 `instrument` 唯一升序，`result_hash` 构造时复核。"""

    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    decision_time: UtcDatetime
    positions: tuple[ConstrainedPosition, ...] = Field(min_length=1)
    result_hash: ContentHash

    @field_validator("positions")
    @classmethod
    def _sorted(cls, value: tuple[ConstrainedPosition, ...]) -> tuple[ConstrainedPosition, ...]:
        names = [item.instrument for item in value]
        if names != sorted(set(names)):
            raise ValueError("positions 必须按 instrument 唯一升序")
        return value

    @model_validator(mode="after")
    def _self_consistent_hash(self) -> RiskResult:
        if self.result_hash != self._expected_hash():
            raise ValueError("result_hash 与结果内容不符")
        return self

    def _expected_hash(self) -> str:
        return _hash_of(
            request_hash=self.request_hash,
            provider=self.provider,
            provider_hash=self.provider_hash,
            decision_time=self.decision_time.isoformat(),
            positions=_dump(self.positions),
        )

    @classmethod
    def build(
        cls,
        request: RiskRequest,
        descriptor: RiskProviderDescriptor,
        positions: Iterable[ConstrainedPosition],
    ) -> RiskResult:
        items = tuple(sorted(positions, key=lambda item: item.instrument))
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            decision_time=request.decision_time,
            positions=items,
            result_hash=_hash_of(
                request_hash=request_hash,
                provider=descriptor.plugin_key,
                provider_hash=provider_hash,
                decision_time=request.decision_time.isoformat(),
                positions=_dump(items),
            ),
        )

    def check_answers(self, request: RiskRequest, descriptor: RiskProviderDescriptor) -> None:
        """请求哈希与身份一致；仓位与 `targets` 一一对应且 `requested_weight` 原样回显；风控侧
        输入时间属于可见集合。"""
        if self.request_hash != request.content_hash():
            raise ValueError("request_hash 与请求不符")
        if (self.provider, self.provider_hash) != (
            descriptor.plugin_key,
            descriptor.content_hash(),
        ):
            raise ValueError("provider / provider_hash 与 descriptor 不符")
        if self.decision_time != request.decision_time:
            raise ValueError("decision_time 与请求不符")
        pairs = [(item.instrument, item.target_weight) for item in request.targets]
        if [(item.instrument, item.requested_weight) for item in self.positions] != pairs:
            raise ValueError("positions 必须与 targets 一一对应并原样回显 requested_weight")
        visible = request.visible()
        times = {obs.available_time for obs in visible}
        for item in self.positions:
            if item.inputs_used > len(visible):
                raise ValueError(f"{item.instrument} 的 inputs_used 超过可见观察条数")
            latest = item.latest_input_available_time
            if latest is not None and latest not in times:
                raise ValueError(f"{item.instrument} 的 latest_input_available_time 不属于可见观察")


class RiskProvider(Protocol):
    """目标仓位 + 组合状态 → 约束后的仓位（ADR-0038）。"""

    @property
    def descriptor(self) -> RiskProviderDescriptor: ...

    def constrain(self, request: RiskRequest) -> RiskResult:
        """约束一个决策时刻的全部目标；不支持的规则集 → `UnsupportedRiskPolicy`。"""
        ...


# ---------------------------------------------------------------------------------------
# BacktestProvider
# ---------------------------------------------------------------------------------------


class BacktestCostModel(Contract):
    """回测成本模型 v1：按成交名义额收取的费率 + 相对参考价的不利滑点率（均为 `[0, 1)` 小数）。

    买入成交价 = `open × (1 + slippage_rate)`，卖出 = `open × (1 − slippage_rate)`；
    费用 = `|quantity| × fill_price × fee_rate`。`ref` 是其 `cost_model:name@version` 引用
    （进入复现元组的 `cost_model_ref`）。
    """

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    fee_rate: Rate
    slippage_rate: Rate

    @property
    def ref(self) -> Ref:
        return Ref(kind=Kind.COST_MODEL, name=self.name, version=self.version)


class PriceBar(Contract):
    """一根价格 bar：`[interval_start, interval_end)`，`available_time >= interval_end`。

    `volume`（ADR-0054 §4，可选）：该 bar 的成交数量（`>= 0`）。它是 bar 收盘后才知道的量，
    只被模拟器用来界定"这根 bar 市场能承载多少"，**从不**进入策略或风控的决策输入
    （`StrategyRequest` / `RiskRequest` 不含 `PriceBar`），因此不构成 C-L1 泄漏。为 `None` 时
    从载荷中省略：既有 `PriceBar` 与 `BacktestRequest` 的内容哈希逐位不变。
    """

    _FIELDS_SINCE = {"volume": ADR_0054_VERSION}

    instrument: NonEmptyStr
    interval_start: UtcDatetime
    interval_end: UtcDatetime
    available_time: UtcDatetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal | None = Field(default=None, exclude_if=_omit_none)

    @model_validator(mode="after")
    def _invariants(self) -> PriceBar:
        if self.interval_end <= self.interval_start:
            raise ValueError("interval_end 必须晚于 interval_start")
        if self.available_time < self.interval_end:
            raise ValueError("available_time 不得早于 interval_end（bar 收盘前不可见）")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("必须 low <= min(open, close) 且 high >= max(open, close)")
        return self


def _bar_order(item: PriceBar) -> tuple[datetime, str]:
    return (item.interval_start, item.instrument)


def execution_bar(
    bars: Sequence[PriceBar], instrument: str, decision_time: datetime
) -> PriceBar | None:
    """执行 bar：该标的第一根 `interval_start >= decision_time` 的 bar（两种执行模型相同）。"""
    for bar in bars:  # 已按 (interval_start, instrument) 升序
        if bar.instrument == instrument and bar.interval_start >= decision_time:
            return bar
    return None


class BacktestRequest(Contract):
    """一次回测请求。

    - `bars`：非空，按 `(interval_start, instrument)` 唯一升序；同一标的的 bar 不重叠；
    - `targets`：按 `(decision_time, instrument)` 唯一升序，每个标的都必须有 bar；
    - `initial_equity > 0`，初始全部为现金、无持仓。
    """

    cost_model: BacktestCostModel
    initial_equity: PositiveDecimal
    bars: tuple[PriceBar, ...] = Field(min_length=1)
    targets: tuple[TargetPosition, ...]

    @field_validator("bars")
    @classmethod
    def _canonical_bars(cls, value: tuple[PriceBar, ...]) -> tuple[PriceBar, ...]:
        ordered = tuple(sorted(value, key=_bar_order))
        for earlier, later in pairwise(ordered):
            if _bar_order(earlier) == _bar_order(later):
                raise ValueError(f"bars 重复：{later.instrument} @ {later.interval_start}")
        last_end: dict[str, datetime] = {}
        for bar in ordered:
            end = last_end.get(bar.instrument)
            if end is not None and bar.interval_start < end:
                raise ValueError(f"{bar.instrument} 的 bar 在 {bar.interval_start} 重叠")
            last_end[bar.instrument] = bar.interval_end
        return ordered

    @field_validator("targets")
    @classmethod
    def _canonical_targets(cls, value: tuple[TargetPosition, ...]) -> tuple[TargetPosition, ...]:
        ordered = tuple(sorted(value, key=_position_order))
        for earlier, later in pairwise(ordered):
            if _position_order(earlier) == _position_order(later):
                raise ValueError(f"targets 重复：{later.instrument} @ {later.decision_time}")
        return ordered

    @model_validator(mode="after")
    def _targets_have_prices(self) -> BacktestRequest:
        priced = {bar.instrument for bar in self.bars}
        missing = sorted({item.instrument for item in self.targets} - priced)
        if missing:
            raise ValueError(f"targets 的标的没有价格 bar：{missing}")
        return self


class Fill(Contract):
    """一笔模拟成交（不是订单；v1 没有任何下单能力）。

    `fill_time`（成交 bar 的 `interval_start`）不得早于 `decision_time`——结构性无未来函数。
    `slippage_cost = |quantity| × |fill_price − reference_price|`。
    """

    instrument: NonEmptyStr
    decision_time: UtcDatetime
    fill_time: UtcDatetime
    reference_price: PositiveDecimal
    fill_price: PositiveDecimal
    quantity: FiniteDecimal
    fee: NonNegativeDecimal
    slippage_cost: NonNegativeDecimal

    @model_validator(mode="after")
    def _invariants(self) -> Fill:
        if self.fill_time < self.decision_time:
            raise ValueError("fill_time 不得早于 decision_time（未来函数）")
        if self.quantity == 0:
            raise ValueError("零数量不是成交")
        return self


class FillRemainder(Contract):
    """一个目标在 `next_bar_open_participation` 执行模型下的结转记录（ADR-0054 §3）。

    - `requested_quantity`：目标变化量（执行 bar 按执行前权益定量，之后不再重新定量），非零；
    - `filled_quantity`：该目标各笔成交数量之和，与 `requested_quantity` 同号（或为 0），
      绝对值不超过它；
    - `remaining_quantity = |requested_quantity| − |filled_quantity|`（`>= 0`）；
    - `ended_by`：`filled`（剩余为 0）、`superseded`（同一标的更晚的目标在其执行 bar 取代它）、
      `end_of_data`（该标的的 bar 用完）；后两者剩余为正；
    - `ended_at`：结束所在 bar 的 `interval_start`（`filled` = 最后一笔成交的 bar；
      `superseded` = 取代它的目标的执行 bar；`end_of_data` = 该标的最后一根 bar）。
    """

    _MODEL_SINCE = ADR_0054_VERSION  # the whole model is new in 2.1.0 (ADR-0054)

    instrument: NonEmptyStr
    decision_time: UtcDatetime
    requested_quantity: FiniteDecimal
    filled_quantity: FiniteDecimal
    remaining_quantity: NonNegativeDecimal
    ended_by: FillRemainderEnd
    ended_at: UtcDatetime

    @model_validator(mode="after")
    def _invariants(self) -> FillRemainder:
        if self.requested_quantity == 0:
            raise ValueError("零目标变化量没有结转")
        if self.filled_quantity != 0 and (self.filled_quantity > 0) != (
            self.requested_quantity > 0
        ):
            raise ValueError("成交方向必须与目标变化量相同")
        if abs(self.filled_quantity) > abs(self.requested_quantity):
            raise ValueError("成交数量绝对值之和超过目标变化量")
        with localcontext() as ctx:
            ctx.prec = _SUM_PRECISION
            remaining = abs(self.requested_quantity) - abs(self.filled_quantity)
        if self.remaining_quantity != remaining:
            raise ValueError("remaining_quantity 必须等于 |requested| − |filled|")
        if (self.ended_by == "filled") != (self.remaining_quantity == 0):
            raise ValueError("filled 当且仅当剩余为 0；superseded / end_of_data 剩余必须为正")
        if self.ended_at < self.decision_time:
            raise ValueError("ended_at 不得早于 decision_time")
        return self


class EquityPoint(Contract):
    """一个时点（bar 收盘）的组合估值：`equity = cash + Σ quantity × mark_price`。"""

    time: UtcDatetime
    cash: FiniteDecimal
    equity: FiniteDecimal
    gross_exposure: NonNegativeDecimal


class BacktestProviderDescriptor(Contract):
    """BacktestProvider 的身份：只能确定性、只能模拟；执行模型为 `next_bar_open`（v1）或
    `next_bar_open_participation`（ADR-0054：剩余量跨 bar 结转）。"""

    _VALUES_SINCE = {"execution_model": {"next_bar_open_participation": ADR_0054_VERSION}}

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]
    simulation_only: Literal[True]
    execution_model: ExecutionModelName

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"


class BacktestResult(Contract):
    """一次 `run` 的结果。

    - `fills` 按 `(fill_time, instrument)` 升序；`equity_curve` 按 `time` 严格升序、非空；
    - `final_equity` 等于权益曲线的最后一点；`total_fees` / `total_slippage` 等于各成交之和；
    - `unexecuted_targets`：没有成交的目标数（其后无 bar，或在成交前被同一标的更晚的目标取代）；
    - `remainders`（ADR-0054）：`next_bar_open_participation` 下每个已定量目标的结转记录，按
      `(decision_time, instrument)` 唯一升序；只有该执行模型可以非空。为空时从哈希载荷中省略，
      既有 `result_hash` 逐位不变；
    - `result_hash` 构造时复核。PnL = `final_equity − initial_equity`（已扣除费用与滑点）。
    """

    _FIELDS_SINCE = {"remainders": ADR_0054_VERSION}

    request_hash: ContentHash
    provider: PluginKey
    provider_hash: ContentHash
    initial_equity: PositiveDecimal
    fills: tuple[Fill, ...]
    equity_curve: tuple[EquityPoint, ...] = Field(min_length=1)
    final_equity: FiniteDecimal
    total_fees: NonNegativeDecimal
    total_slippage: NonNegativeDecimal
    unexecuted_targets: int = Field(ge=0, strict=True)
    remainders: tuple[FillRemainder, ...] = Field(default=(), exclude_if=_omit_empty)
    result_hash: ContentHash

    @model_validator(mode="after")
    def _invariants(self) -> BacktestResult:
        fill_keys = [(item.fill_time, item.instrument) for item in self.fills]
        if fill_keys != sorted(fill_keys):
            raise ValueError("fills 必须按 (fill_time, instrument) 升序")
        remainder_keys = [(item.decision_time, item.instrument) for item in self.remainders]
        if any(later <= earlier for earlier, later in pairwise(remainder_keys)):
            raise ValueError("remainders 必须按 (decision_time, instrument) 唯一升序")
        _strictly_ascending([point.time for point in self.equity_curve], "equity_curve.time")
        if self.final_equity != self.equity_curve[-1].equity:
            raise ValueError("final_equity 必须等于权益曲线的最后一点")
        if self.total_fees != _exact_sum(item.fee for item in self.fills):
            raise ValueError("total_fees 必须等于各成交费用之和")
        if self.total_slippage != _exact_sum(item.slippage_cost for item in self.fills):
            raise ValueError("total_slippage 必须等于各成交滑点之和")
        if self.result_hash != self._expected_hash():
            raise ValueError("result_hash 与结果内容不符")
        return self

    def _expected_hash(self) -> str:
        return _hash_of(**self._hashed_fields())

    def _hashed_fields(self) -> dict[str, object]:
        dumped = self.model_dump(mode="json", exclude={"result_hash", "schema_version"})
        return dict(dumped)

    @property
    def pnl(self) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = _SUM_PRECISION
            return self.final_equity - self.initial_equity

    @classmethod
    def build(
        cls,
        request: BacktestRequest,
        descriptor: BacktestProviderDescriptor,
        *,
        fills: Iterable[Fill],
        equity_curve: Iterable[EquityPoint],
        unexecuted_targets: int,
        remainders: Iterable[FillRemainder] = (),
    ) -> BacktestResult:
        fill_items = tuple(fills)
        curve = tuple(equity_curve)
        remainder_items = tuple(remainders)
        if not curve:
            raise ValueError("equity_curve 不得为空")
        request_hash = request.content_hash()
        provider_hash = descriptor.content_hash()
        total_fees = _exact_sum(item.fee for item in fill_items)
        total_slippage = _exact_sum(item.slippage_cost for item in fill_items)
        draft = cls.model_construct(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            initial_equity=request.initial_equity,
            fills=fill_items,
            equity_curve=curve,
            final_equity=curve[-1].equity,
            total_fees=total_fees,
            total_slippage=total_slippage,
            unexecuted_targets=unexecuted_targets,
            remainders=remainder_items,
            result_hash="0" * 64,
        )
        return cls(
            request_hash=request_hash,
            provider=descriptor.plugin_key,
            provider_hash=provider_hash,
            initial_equity=request.initial_equity,
            fills=fill_items,
            equity_curve=curve,
            final_equity=curve[-1].equity,
            total_fees=total_fees,
            total_slippage=total_slippage,
            unexecuted_targets=unexecuted_targets,
            remainders=remainder_items,
            result_hash=_hash_of(**draft._hashed_fields()),
        )

    def check_answers(
        self, request: BacktestRequest, descriptor: BacktestProviderDescriptor
    ) -> None:
        """请求哈希与身份一致；初始权益一致；再按 descriptor 的执行模型核对成交（ADR-0054 §2）。

        `next_bar_open`：每笔成交对应一个目标，并恰好在执行模型规定的 bar（该标的第一根
        `interval_start >= decision_time` 的 bar）以其开盘价为参考价成交；不得有结转记录。
        `next_bar_open_participation`：见 `_check_carry_over`。
        """
        if self.request_hash != request.content_hash():
            raise ValueError("request_hash 与请求不符")
        if (self.provider, self.provider_hash) != (
            descriptor.plugin_key,
            descriptor.content_hash(),
        ):
            raise ValueError("provider / provider_hash 与 descriptor 不符")
        if self.initial_equity != request.initial_equity:
            raise ValueError("initial_equity 与请求不符")
        if descriptor.execution_model == "next_bar_open_participation":
            self._check_carry_over(request)
            return
        if self.remainders:
            raise ValueError("只有 next_bar_open_participation 执行模型可以有结转记录")
        targets = {_position_order(item) for item in request.targets}
        for item in self.fills:
            if (item.decision_time, item.instrument) not in targets:
                raise ValueError(f"成交 {item.instrument} @ {item.fill_time} 没有对应的目标")
            bar = execution_bar(request.bars, item.instrument, item.decision_time)
            if bar is None or bar.interval_start != item.fill_time:
                raise ValueError(
                    f"成交 {item.instrument} @ {item.fill_time} 不在执行模型规定的 bar"
                )
            if item.reference_price != bar.open:
                raise ValueError(f"成交 {item.instrument} 的参考价不是该 bar 的开盘价")
        if len(self.fills) + self.unexecuted_targets > len(request.targets):
            raise ValueError("成交数 + 未执行目标数超过目标数")

    def _check_carry_over(self, request: BacktestRequest) -> None:
        """`next_bar_open_participation` 的成交规则（ADR-0054 §1 – §3）。

        - 成交属于 `(decision_time, instrument)` 命中的目标；`fill_time` 是该标的某根 bar 的
          `interval_start`，且 `执行 bar <= fill_time < 同标的下一目标的执行 bar`（没有下一目标、
          或下一目标没有执行 bar，则不设上界）；参考价 = 该 bar 开盘价；同一目标的成交按
          `fill_time` 严格递增（一根 bar 至多一笔）；
        - 有成交的目标必须有结转记录；记录的目标存在且有执行 bar；每笔成交与目标变化量同向；
          `filled_quantity` 等于各笔成交之和（绝对值不超过目标变化量由 `FillRemainder` 保证）；
        - `ended_at` 是该标的的一根 bar：`filled` = 最后一笔成交的 bar；`superseded` = 下一目标的
          执行 bar；`end_of_data` = 该标的最后一根 bar，且没有下一目标的执行 bar；
        - 计数：有成交的目标数 + `unexecuted_targets <= 目标数`。
        """
        bars_of: dict[str, dict[datetime, PriceBar]] = {}
        for bar in request.bars:  # 已按 (interval_start, instrument) 升序
            bars_of.setdefault(bar.instrument, {})[bar.interval_start] = bar
        by_instrument: dict[str, list[TargetPosition]] = {}
        for target in request.targets:
            by_instrument.setdefault(target.instrument, []).append(target)
        windows: dict[tuple[datetime, str], tuple[datetime | None, datetime | None]] = {}
        for instrument, targets in by_instrument.items():
            first_bars = _first_at_or_after(sorted(bars_of[instrument]), targets)
            for index, target in enumerate(targets):
                upper = first_bars[index + 1] if index + 1 < len(targets) else None
                windows[_position_order(target)] = (first_bars[index], upper)

        fills_of: dict[tuple[datetime, str], list[Fill]] = {}
        for item in self.fills:
            key = (item.decision_time, item.instrument)
            if key not in windows:
                raise ValueError(f"成交 {item.instrument} @ {item.fill_time} 没有对应的目标")
            first, upper = windows[key]
            fill_bar = bars_of[item.instrument].get(item.fill_time)
            if (
                fill_bar is None
                or first is None
                or item.fill_time < first
                or (upper is not None and item.fill_time >= upper)
            ):
                raise ValueError(
                    f"成交 {item.instrument} @ {item.fill_time} 不在该目标的结转窗口"
                    "（执行 bar 至同标的下一目标的执行 bar 之前）的某根 bar"
                )
            if item.reference_price != fill_bar.open:
                raise ValueError(f"成交 {item.instrument} 的参考价不是该 bar 的开盘价")
            earlier = fills_of.setdefault(key, [])
            if earlier and item.fill_time <= earlier[-1].fill_time:
                raise ValueError(
                    f"目标 {item.instrument} @ {item.decision_time} 在同一根 bar 成交两次"
                )
            earlier.append(item)

        records = {(item.decision_time, item.instrument): item for item in self.remainders}
        missing = sorted(set(fills_of) - set(records))
        if missing:
            raise ValueError(f"有成交的目标缺少结转记录：{missing[0][1]} @ {missing[0][0]}")
        for key, record in records.items():
            label = f"{record.instrument} @ {record.decision_time}"
            if key not in windows or windows[key][0] is None:
                raise ValueError(f"结转记录 {label} 没有对应的、有执行 bar 的目标")
            upper = windows[key][1]
            fills = fills_of.get(key, [])
            if any((item.quantity > 0) != (record.requested_quantity > 0) for item in fills):
                raise ValueError(f"目标 {label} 有与目标变化量反向的成交")
            if _exact_sum(item.quantity for item in fills) != record.filled_quantity:
                raise ValueError(f"结转记录 {label} 的 filled_quantity 与其成交之和不符")
            if record.ended_by == "filled":
                ended_ok = bool(fills) and record.ended_at == fills[-1].fill_time
            elif record.ended_by == "superseded":
                ended_ok = upper is not None and record.ended_at == upper
            else:
                ended_ok = upper is None and record.ended_at == max(bars_of[record.instrument])
            if not ended_ok:
                raise ValueError(
                    f"结转记录 {label} 的 {record.ended_by} 结束于 {record.ended_at}，"
                    "与成交 / 下一目标 / bar 不符"
                )
        if len(fills_of) + self.unexecuted_targets > len(request.targets):
            raise ValueError("有成交的目标数 + 未执行目标数超过目标数")


def _first_at_or_after(
    starts: Sequence[datetime], targets: Sequence[TargetPosition]
) -> list[datetime | None]:
    """每个目标（按 `decision_time` 升序）在升序 `starts` 中第一个 `>= decision_time` 的时刻，
    即该标的的执行 bar（同 `execution_bar`）；没有则为 `None`。"""
    out: list[datetime | None] = []
    index = 0
    for target in targets:
        while index < len(starts) and starts[index] < target.decision_time:
            index += 1
        out.append(starts[index] if index < len(starts) else None)
    return out


class BacktestProvider(Protocol):
    """目标仓位 + 价格 + 成本模型 → 成交与 PnL（ADR-0038）。只是模拟。"""

    @property
    def descriptor(self) -> BacktestProviderDescriptor: ...

    def run(self, request: BacktestRequest) -> BacktestResult:
        """按 descriptor 声明的执行模型模拟全部目标；输入无法模拟 → `BacktestInputError`。"""
        ...
