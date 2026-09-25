"""`SyntheticMarketProvider`：合成市场生成的 Protocol 与 DTO（ADR-0042；Phase 9）。

对应 05-plugin.md §3（"生成合成市场：generator spec + seed → synthetic Canonical data；给定种子必须
确定性"）与 roadmap Phase 9：用**已知真值**的合成市场检验方法本身（纯噪声上的假阳性率、植入效应的
检出力）。合成数据**不得**用来支持真实市场结论。

| 成员 | 输入 → 输出 |
|---|---|
| `descriptor` | → `SyntheticProviderDescriptor`（`name@version`，恒为确定性） |
| `generate` | `SyntheticMarketSpec` → `SyntheticMarket`（1 分钟 bar + 植入的真值） |

**真值**：`SyntheticMarket.truth` 记录生成器实际植入的效应（无植入即空）；校准报告用它判断检出与误报。
**确定性**：同一 spec（含 `seed`）→ 同一 `market_hash`。数值一律 `Decimal`（ADR-0013，无浮点）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from pydantic import Field, model_validator

from core.domain.base import (
    NAME_PATTERN,
    SEMVER_PATTERN,
    ContentHash,
    Contract,
    PluginKey,
    UtcDatetime,
    content_hash,
)

__all__ = [
    "PlantedEffect",
    "SyntheticBar",
    "SyntheticMarket",
    "SyntheticMarketProvider",
    "SyntheticMarketSpec",
    "SyntheticProviderDescriptor",
    "market_hash",
]


class PlantedEffect(Contract):
    """一个植入的已知效应：`lag_minutes` 分钟前的收益以 `strength` 的比例进入当期收益。

    `strength` 为 0 等价于不植入；取值范围 (-1, 1) 保证过程平稳。
    """

    kind: Literal["return_autocorrelation"] = "return_autocorrelation"
    lag_minutes: int = Field(ge=1, le=10_000, strict=True)
    strength: Decimal

    @model_validator(mode="after")
    def _stationary(self) -> PlantedEffect:
        if not Decimal(-1) < self.strength < Decimal(1):
            raise ValueError("strength 必须在 (-1, 1) 之间")
        return self


class SyntheticMarketSpec(Contract):
    """生成规格：一个合成标的在 `[start, start + minutes)` 的 1 分钟 bar。"""

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    symbol: str = Field(min_length=1)
    start: UtcDatetime
    minutes: int = Field(ge=1, le=10_000_000, strict=True)
    seed: int = Field(ge=0, strict=True)
    initial_price: Decimal = Field(gt=0)
    #: 每分钟收益的噪声标准差（小数，如 0.001 = 10bp）。
    volatility: Decimal = Field(gt=0, lt=1)
    #: 每分钟的确定性漂移（小数）。
    drift: Decimal = Decimal(0)
    effects: tuple[PlantedEffect, ...] = ()

    @model_validator(mode="after")
    def _minute_aligned(self) -> SyntheticMarketSpec:
        if self.start.second or self.start.microsecond:
            raise ValueError("start 必须对齐到整分钟")
        return self


class SyntheticBar(Contract):
    """一根合成 1 分钟 bar（字段与 `canonical.bars_1m` 的市场内容一致）。"""

    interval_start: UtcDatetime
    interval_end: UtcDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int = Field(ge=0, strict=True)


class SyntheticMarket(Contract):
    """生成结果：bar 按时间升序；`truth` 为实际植入的效应；`market_hash` 绑定规格与全部 bar。"""

    spec_hash: ContentHash
    provider: PluginKey
    bars: tuple[SyntheticBar, ...]
    truth: tuple[PlantedEffect, ...]
    market_hash: ContentHash

    @model_validator(mode="after")
    def _consistent(self) -> SyntheticMarket:
        starts = [bar.interval_start for bar in self.bars]
        if starts != sorted(starts) or len(set(starts)) != len(starts):
            raise ValueError("bars 必须按 interval_start 唯一升序")
        if self.market_hash != market_hash(self.spec_hash, self.provider, self.bars, self.truth):
            raise ValueError("market_hash 与内容不符")
        return self


def market_hash(
    spec_hash: str,
    provider: str,
    bars: tuple[SyntheticBar, ...],
    truth: tuple[PlantedEffect, ...],
) -> str:
    return content_hash(
        {
            "spec_hash": spec_hash,
            "provider": provider,
            "bars": [bar.content_hash() for bar in bars],
            "truth": [effect.content_hash() for effect in truth],
        }
    )


class SyntheticProviderDescriptor(Contract):
    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    deterministic: Literal[True]

    @property
    def plugin_key(self) -> str:
        return f"{self.name}@{self.version}"


class SyntheticMarketProvider(Protocol):
    """合成市场生成（ADR-0042）。语义见模块文档。"""

    @property
    def descriptor(self) -> SyntheticProviderDescriptor: ...

    def generate(self, spec: SyntheticMarketSpec) -> SyntheticMarket: ...
