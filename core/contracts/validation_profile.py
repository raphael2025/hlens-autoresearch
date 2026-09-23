"""Validation Profile 契约（ADR-0007 第二层；07-validation.md §5）。

**本模块只定义字段，不含任何阈值数值。** 参数值在 Phase 4 校准后写入具体 Profile 版本
（两步冻结 Step 2）；Constitution 只写原则，不写数字。

Profile 一经被实验使用即不可变：`status = frozen` 后只能发布新版本。
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import Contract, FrozenMapping, Kind, Ref, VersionedSpec

__all__ = [
    "BenchmarkParams",
    "CostStressParams",
    "DataSplitParams",
    "LifecycleParams",
    "ParameterStabilityParams",
    "ProfileScope",
    "ProfileStatus",
    "Provenance",
    "SampleSizeParams",
    "SignificanceParams",
    "ValidationProfile",
    "WalkForwardParams",
]


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"
    SUPERSEDED = "superseded"


class ProfileScope(Contract):
    """Profile 的适用范围；与选择规则（ProfileSelectionRule）匹配。

    `research_class` 的取值集合（例如按持仓周期划分）仍未决定（D-09 H-7），
    因此这里只约束为标识符，不预设分类。
    """

    venue: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    research_class: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class WalkForwardParams(Contract):
    train_window: timedelta
    test_window: timedelta
    step: timedelta
    min_positive_window_fraction: float = Field(ge=0.0, le=1.0)
    max_single_window_pnl_share: float = Field(gt=0.0, le=1.0)


class DataSplitParams(Contract):
    """对应 Constitution 第四章。封存区使用固定日期边界，不随运行时间滚动。"""

    research_window_start: date
    sealed_oos_boundary: date
    sealed_oos_length: timedelta
    sealed_oos_max_extension: timedelta
    embargo: timedelta
    walk_forward: WalkForwardParams

    @model_validator(mode="after")
    def _boundary_after_start(self) -> DataSplitParams:
        if self.sealed_oos_boundary <= self.research_window_start:
            raise ValueError("封存边界必须晚于研究窗口起点")
        return self


class SampleSizeParams(Contract):
    """对应 Constitution C-T2：以有效独立样本计。"""

    min_effective_trades_in_sample: int = Field(gt=0)
    min_effective_trades_out_of_sample: int = Field(gt=0)
    min_effective_trades_per_state: int = Field(gt=0)
    effective_sample_method: str = Field(min_length=1)
    min_regime_coverage: str = Field(min_length=1)


class SignificanceParams(Contract):
    """对应 Constitution C-T1：多重检验校正 + 过拟合概率。"""

    multiple_testing_method: str = Field(min_length=1)
    multiple_testing_threshold: float
    overfitting_metric: str = Field(min_length=1)
    overfitting_threshold: float
    trial_count_scope: str = Field(min_length=1)
    reported_only_metrics: tuple[str, ...] = ()


class BenchmarkParams(Contract):
    """对应 Constitution C-T4：空模型为主，市场基准按类别适用。"""

    null_model: str = Field(min_length=1)
    null_model_simulations: int = Field(gt=0)
    null_model_percentile: float = Field(ge=0.0, le=100.0)
    market_benchmark_rule: str = Field(min_length=1)
    inverse_control_reported: bool


class ParameterStabilityParams(Contract):
    """对应 Constitution C-R1。"""

    neighborhood_definition: str = Field(min_length=1)
    min_neighborhood_performance_ratio: float
    min_positive_neighbor_fraction: float = Field(ge=0.0, le=1.0)
    time_alignment_offsets: tuple[timedelta, ...] = ()


class CostStressParams(Contract):
    """对应 Constitution C-R4 与 A6。"""

    cost_model: Ref
    fill_assumption: str = Field(min_length=1)
    stress_multipliers: tuple[float, ...] = Field(min_length=1)
    reported_only_multipliers: tuple[float, ...] = ()
    delay_stress_bars: int = Field(ge=0)
    min_breakeven_cost_multiple: float = Field(gt=0.0)


class LifecycleParams(Contract):
    """对应 Constitution C-G3 与 ADR-0006（Q-6 仍开放）。"""

    paper_period: timedelta
    paper_acceptance_rule: str = Field(min_length=1)
    degradation_thresholds: FrozenMapping[str, float] = Field(
        default_factory=dict, validate_default=True
    )


class Provenance(Contract):
    """Step 2 冻结依据。"""

    calibration_report: str | None = None
    approval_adr: str | None = None
    previous_version: str | None = None


class ValidationProfile(VersionedSpec):
    """按"标的 × 周期 × 研究类别"给出的版本化阈值集合。

    不可变性：`status = frozen` 的版本永不修改；修正 = 新版本（Constitution C-A5）。
    完整性：五类门槛必须齐全，否则 Profile 无效（Constitution C-A7）。
    """

    kind: Kind = Kind.PROFILE
    status: ProfileStatus = ProfileStatus.DRAFT
    scope: ProfileScope
    data_split: DataSplitParams
    sample_size: SampleSizeParams
    significance: SignificanceParams
    benchmark: BenchmarkParams
    parameter_stability: ParameterStabilityParams
    cost_stress: CostStressParams
    lifecycle: LifecycleParams
    inconclusive_bands: FrozenMapping[str, float] = Field(
        default_factory=dict, validate_default=True
    )
    provenance: Provenance = Provenance()

    @model_validator(mode="after")
    def _frozen_requires_provenance(self) -> ValidationProfile:
        if self.status is ProfileStatus.FROZEN and self.provenance.calibration_report is None:
            raise ValueError("冻结的 Profile 必须引用校准报告（Constitution C-A8）")
        return self

    @classmethod
    def _non_semantic_fields(cls) -> set[str]:
        """`status` 是操作状态，不属于内容身份；`provenance` **保留**在哈希内（ADR-0008 决策 3）。

        因此内容相同的 `draft` 与 `frozen` 版本 `content_hash()` 相等，
        `ReproducibilityTuple.validation_profile_hash` 指向的就是这个值。
        """
        return super()._non_semantic_fields() | {"status"}
