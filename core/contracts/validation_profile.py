"""Validation Profile 契约（ADR-0007 第二层；07-validation.md §5）。

**本模块只定义字段，不含任何阈值数值。** 参数值在 Phase 4 校准后写入具体 Profile 版本
（两步冻结 Step 2）；Constitution 只写原则，不写数字。

Profile 一经被实验使用即不可变：`status = frozen` 后只能发布新版本。
"""

from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from core.domain.base import Contract, FrozenMapping, Kind, Ref, VersionedSpec

__all__ = [
    "BenchmarkParams",
    "CostStressParams",
    "DataSplitParams",
    "LifecycleParams",
    "NonNegativeDuration",
    "ParameterStabilityParams",
    "PositiveDuration",
    "PositiveMultiplier",
    "ProfileScope",
    "ProfileStatus",
    "Provenance",
    "SampleSizeParams",
    "SignificanceParams",
    "ValidationProfile",
    "WalkForwardParams",
]


#: 严格正的时间跨度（ADR-0014 §D-20.4）。零长度或负长度的训练 / 检验窗口、封存区、观察期
#: 在任何标的与周期下都不是"另一种校准选择"，而是结构上无意义的配置。
#:
#: **JSON Schema 表达限制**：`timedelta` 的线格式是 ISO 8601 duration 字符串
#: （`type: string, format: duration`），而 `minimum` / `exclusiveMinimum` 是数值关键字，
#: 无法表达字符串上的时长顺序。因此该约束是**运行时**约束：Pydantic 在 Python 与
#: `model_validate_json` 两条入口都强制执行，导出的 Schema 诚实保持字符串形状，
#: 不把数值 minimum 伪装上去（07-validation.md §5.4）。
PositiveDuration = Annotated[timedelta, Field(gt=timedelta(0))]

#: 非负的时间跨度（ADR-0014 §D-20.4）：零表示"不设该期限"，是合法配置；负数不是。
#: JSON Schema 表达限制同 `PositiveDuration`。
NonNegativeDuration = Annotated[timedelta, Field(ge=timedelta(0))]

#: 严格正的倍数（ADR-0014 §D-20.4）：零倍成本压力等于"不施压"，负倍没有语义。
#: 约束作用于**序列的每个元素**，因此导出的 Schema 里是 `items.exclusiveMinimum`。
#: 有限性由 ADR-0013 §D-20.1 的 `Contract.model_config.allow_inf_nan=False` 保证，
#: 本别名不重复声明。
PositiveMultiplier = Annotated[float, Field(gt=0.0)]


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
    """Walk-forward 配置。三个时间跨度必须严格为正（ADR-0014 §D-20.4）。

    本模型**不**约束跨字段关系（例如 `test_window` 与 `step` 的比例）：那取决于校准与
    方法选择，不是普适结构。
    """

    train_window: PositiveDuration
    test_window: PositiveDuration
    step: PositiveDuration
    min_positive_window_fraction: float = Field(ge=0.0, le=1.0)
    max_single_window_pnl_share: float = Field(gt=0.0, le=1.0)


class DataSplitParams(Contract):
    """对应 Constitution 第四章。封存区使用固定日期边界，不随运行时间滚动。

    结构不变量（ADR-0014 §D-20.4）：`sealed_oos_length > 0`（零长度封存区不构成样本外检验）；
    `embargo >= 0` 与 `sealed_oos_max_extension >= 0`（零分别表示不设隔离期、不允许延长）。
    """

    research_window_start: date
    sealed_oos_boundary: date
    sealed_oos_length: PositiveDuration
    sealed_oos_max_extension: NonNegativeDuration
    embargo: NonNegativeDuration
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
    """对应 Constitution C-T1：多重检验校正 + 过拟合概率。

    两个阈值是**无量纲的 unit interval 量**，结构范围为闭区间 `[0, 1]`（ADR-0013 D-20.2）。
    这只是**结构上的合法取值范围**，不是校准值：具体数值属于 D-09 的 TBD 系列，
    Phase 4 校准后才写入具体 Profile 版本。端点 `0` 与 `1` 是否算合理配置属于校准判断，
    因此刻意保留。
    """

    multiple_testing_method: str = Field(min_length=1)
    multiple_testing_threshold: float = Field(ge=0.0, le=1.0)
    overfitting_metric: str = Field(min_length=1)
    overfitting_threshold: float = Field(ge=0.0, le=1.0)
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
    """对应 Constitution C-R4 与 A6。

    结构不变量（ADR-0014 §D-20.4）：两个倍数序列的**每一项**严格为正；
    `cost_model` 必须指向 `cost_model` kind——这是跨字段语义，JSON Schema 不表达它，
    由运行时校验保证。
    """

    cost_model: Ref
    fill_assumption: str = Field(min_length=1)
    stress_multipliers: tuple[PositiveMultiplier, ...] = Field(min_length=1)
    reported_only_multipliers: tuple[PositiveMultiplier, ...] = ()
    delay_stress_bars: int = Field(ge=0)
    min_breakeven_cost_multiple: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _cost_model_kind(self) -> CostStressParams:
        if self.cost_model.kind is not Kind.COST_MODEL:
            raise ValueError(
                f"CostStressParams.cost_model 必须指向 cost_model，"
                f"收到 {self.cost_model.kind.value}"
            )
        return self


class LifecycleParams(Contract):
    """对应 Constitution C-G3 与 ADR-0006（Q-6 仍开放）。

    结构不变量（ADR-0014 §D-20.4）：`paper_period > 0`——观察期必须有长度。
    本约束只管符号，不回答"观察期该多长"（Phase 4）或"该由谁定义"（Q-6）。
    """

    paper_period: PositiveDuration
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

    kind: Literal[Kind.PROFILE] = Kind.PROFILE
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
