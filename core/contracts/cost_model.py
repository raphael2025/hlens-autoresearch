"""成本模型 v1 的规格（ADR-0037 §4；Phase 4；Constitution C-R4 / A6）。

`ValidationProfile.cost_stress.cost_model` 与 `ReproducibilityTuple.cost_model_ref` 都是
`kind=cost_model` 的引用；本模块给出被引用的对象本身：一份可版本化、可哈希的成本规格。

v1 只有一种方法 `proportional_v1`：每次成交（每一侧）按名义价值收取 `fee_rate_per_side` 的手续费与
`slippage_rate_per_side` 的滑点，一次往返的成本 = `2 × (fee + slippage)`（收益率单位）。
费率与滑点是**成本模型参数**，不是验证阈值；压力倍数来自 Profile
（`cost_stress.stress_multipliers`）。

结构不变量：两个费率在 `[0, 1)`；两者之和必须严格为正——零成本等于跳过成本模型（roadmap Phase 4
禁止事项），这是结构约束而非校准。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from core.contracts.outcome import FiniteDecimal
from core.domain.base import Kind, VersionedSpec

__all__ = ["CostModelSpec"]


class CostModelSpec(VersionedSpec):
    """成本模型 v1：比例手续费 + 比例滑点（每侧）。"""

    kind: Literal[Kind.COST_MODEL] = Kind.COST_MODEL
    method: Literal["proportional_v1"] = "proportional_v1"
    fee_rate_per_side: FiniteDecimal = Field(ge=0, lt=1)
    slippage_rate_per_side: FiniteDecimal = Field(ge=0, lt=1)

    @model_validator(mode="after")
    def _cost_is_not_skipped(self) -> CostModelSpec:
        if self.fee_rate_per_side + self.slippage_rate_per_side <= 0:
            raise ValueError("成本模型的总费率必须为正：零成本等于跳过成本模型")
        return self

    def round_trip_cost(self, multiplier: Decimal = Decimal(1)) -> Decimal:
        """一次往返（入场 + 出场）的成本，收益率单位；`multiplier` 为压力倍数（必须为正）。"""
        if not isinstance(multiplier, Decimal) or not multiplier.is_finite() or multiplier <= 0:
            raise ValueError("multiplier 必须是有限的正 Decimal")
        return 2 * (self.fee_rate_per_side + self.slippage_rate_per_side) * multiplier
