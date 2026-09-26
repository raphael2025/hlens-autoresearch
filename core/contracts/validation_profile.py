"""Validation Profile 契约（ADR-0007 第二层；07-validation.md §5）。

**本模块只定义字段，不含任何阈值数值。** 参数值在 Phase 4 校准后写入具体 Profile 版本
（两步冻结 Step 2）；Constitution 只写原则，不写数字。

Profile 一经被实验使用即不可变：`status = frozen` 后只能发布新版本。

ADR-0052（2026-09-26）新增的全部字段都是**可选**的，缺失时省略出载荷（旧 Profile 的载荷与
`content_hash()` 逐位不变）：

- D-FLOAT：每个浮点阈值字段有同名加 `_exact` 的精确兄弟字段（`ExactDecimal`；映射 / 序列字段
  为逐键 / 逐项的精确映射 / 序列）。存在时浮点字段必须恰为其派生值，哈希载荷只含精确值
  （`core.domain.base.ExactBacked`）。浮点字段弃用。
- D-PFIELDS / D-CTRL：`capacity.*`、`cross_asset.min_positive_fraction`、
  `significance.cscv_partitions`、`significance.negative_control_threshold`、
  `data_split.sealed_oos_max_unsealings`、`sample_size.max_undersampled_pnl_share`——只有字段与
  结构范围（符号 / 单位区间 / 偶数），**没有任何数值**；数值属 Step 2 校准，由 Raphael 冻结。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, Field, model_validator

from core.domain.base import (
    RESEARCH_CLASS_PATTERN,
    Contract,
    ExactBacked,
    ExactDecimal,
    FrozenMapping,
    Kind,
    Ref,
    VersionedSpec,
    omit_none,
)

#: The minor that introduced this module's ADR-0052 fields (never under 2.0.0).
ADR_0052_VERSION: Final = "2.1.0"

__all__ = [
    "BenchmarkParams",
    "CapacityParams",
    "CostStressParams",
    "CrossAssetParams",
    "CscvPartitions",
    "DataSplitParams",
    "LifecycleParams",
    "NonNegativeDuration",
    "NonNegativeExact",
    "ParameterStabilityParams",
    "PercentileExact",
    "PositiveDuration",
    "PositiveExact",
    "PositiveMultiplier",
    "ProfileScope",
    "ProfileStatus",
    "Provenance",
    "SampleSizeParams",
    "SignificanceParams",
    "UnitExact",
    "UnitPositiveExact",
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


def _exact_range(
    *,
    ge: int | None = None,
    gt: int | None = None,
    le: int | None = None,
) -> Callable[[Decimal], Decimal]:
    """精确小数的**结构**范围（ADR-0052 §2；ADR-0014 风格：只有符号 / 单位区间，不是校准值）。"""

    def check(value: Decimal) -> Decimal:
        if ge is not None and value < ge:
            raise ValueError(f"必须 >= {ge}")
        if gt is not None and value <= gt:
            raise ValueError(f"必须 > {gt}")
        if le is not None and value > le:
            raise ValueError(f"必须 <= {le}")
        return value

    return check


#: `[0, 1]` 的精确小数（无量纲 unit interval 量）。范围是运行时约束，Schema 只表达规范文本。
UnitExact = Annotated[ExactDecimal, AfterValidator(_exact_range(ge=0, le=1))]
#: `(0, 1]` 的精确小数。
UnitPositiveExact = Annotated[ExactDecimal, AfterValidator(_exact_range(gt=0, le=1))]
#: `>= 0` 的精确小数。
NonNegativeExact = Annotated[ExactDecimal, AfterValidator(_exact_range(ge=0))]
#: `> 0` 的精确小数（例如 `PositiveMultiplier` 的精确兄弟）。
PositiveExact = Annotated[ExactDecimal, AfterValidator(_exact_range(gt=0))]
#: `[0, 100]` 的精确小数（百分位）。
PercentileExact = Annotated[ExactDecimal, AfterValidator(_exact_range(ge=0, le=100))]


def _even_partitions(value: int) -> int:
    """CSCV 的结构要求：分块数为偶数且 `>= 2`（一半做样本内、一半做样本外）。"""
    if value < 2 or value % 2:
        raise ValueError("cscv_partitions 必须是 >= 2 的偶数（CSCV 结构要求）")
    return value


#: CSCV 分块数：偶数且 `>= 2`（结构要求，不是校准值）。
CscvPartitions = Annotated[int, AfterValidator(_even_partitions)]


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
    research_class: str = Field(pattern=RESEARCH_CLASS_PATTERN)


class WalkForwardParams(ExactBacked):
    """Walk-forward 配置。三个时间跨度必须严格为正（ADR-0014 §D-20.4）。

    本模型**不**约束跨字段关系（例如 `test_window` 与 `step` 的比例）：那取决于校准与
    方法选择，不是普适结构。
    """

    _FIELDS_SINCE = {
        "min_positive_window_fraction_exact": ADR_0052_VERSION,
        "max_single_window_pnl_share_exact": ADR_0052_VERSION,
    }

    _EXACT_SIBLINGS = (
        ("min_positive_window_fraction", "min_positive_window_fraction_exact"),
        ("max_single_window_pnl_share", "max_single_window_pnl_share_exact"),
    )

    train_window: PositiveDuration
    test_window: PositiveDuration
    step: PositiveDuration
    min_positive_window_fraction: float = Field(ge=0.0, le=1.0)
    max_single_window_pnl_share: float = Field(gt=0.0, le=1.0)
    min_positive_window_fraction_exact: UnitExact | None = Field(default=None, exclude_if=omit_none)
    max_single_window_pnl_share_exact: UnitPositiveExact | None = Field(
        default=None, exclude_if=omit_none
    )


class DataSplitParams(Contract):
    """对应 Constitution 第四章。封存区使用固定日期边界，不随运行时间滚动。

    结构不变量（ADR-0014 §D-20.4）：`sealed_oos_length > 0`（零长度封存区不构成样本外检验）；
    `embargo >= 0` 与 `sealed_oos_max_extension >= 0`（零分别表示不设隔离期、不允许延长）。
    """

    _FIELDS_SINCE = {"sealed_oos_max_unsealings": ADR_0052_VERSION}

    research_window_start: date
    sealed_oos_boundary: date
    sealed_oos_length: PositiveDuration
    sealed_oos_max_extension: NonNegativeDuration
    embargo: NonNegativeDuration
    walk_forward: WalkForwardParams
    #: 绑定本 Profile 的实验可消耗的封存 OOS 开封上限（C-S2；ADR-0052 §2）。计数账本仍是全局的，
    #: 族批准与批准人仍是实验元数据。缺失时沿用显式参数 `param:max_unsealings`。
    sealed_oos_max_unsealings: Annotated[int, Field(gt=0)] | None = Field(
        default=None, exclude_if=omit_none
    )

    @model_validator(mode="after")
    def _boundary_after_start(self) -> DataSplitParams:
        if self.sealed_oos_boundary <= self.research_window_start:
            raise ValueError("封存边界必须晚于研究窗口起点")
        return self


class SampleSizeParams(Contract):
    """对应 Constitution C-T2：以有效独立样本计。"""

    _FIELDS_SINCE = {"max_undersampled_pnl_share": ADR_0052_VERSION}

    min_effective_trades_in_sample: int = Field(gt=0)
    min_effective_trades_out_of_sample: int = Field(gt=0)
    min_effective_trades_per_state: int = Field(gt=0)
    effective_sample_method: str = Field(min_length=1)
    min_regime_coverage: str = Field(min_length=1)
    #: 欠采样状态的净收益占比上限（C-R2；ADR-0052 §2）。缺失时沿用
    #: `param:state.max_undersampled_pnl_share` 或 `INCONCLUSIVE`。
    max_undersampled_pnl_share: UnitExact | None = Field(default=None, exclude_if=omit_none)


class SignificanceParams(ExactBacked):
    """对应 Constitution C-T1：多重检验校正 + 过拟合概率。

    两个阈值是**无量纲的 unit interval 量**，结构范围为闭区间 `[0, 1]`（ADR-0013 D-20.2）。
    这只是**结构上的合法取值范围**，不是校准值：具体数值属于 D-09 的 TBD 系列，
    Phase 4 校准后才写入具体 Profile 版本。端点 `0` 与 `1` 是否算合理配置属于校准判断，
    因此刻意保留。
    """

    _FIELDS_SINCE = {
        "multiple_testing_threshold_exact": ADR_0052_VERSION,
        "overfitting_threshold_exact": ADR_0052_VERSION,
        "cscv_partitions": ADR_0052_VERSION,
        "negative_control_threshold": ADR_0052_VERSION,
    }

    _EXACT_SIBLINGS = (
        ("multiple_testing_threshold", "multiple_testing_threshold_exact"),
        ("overfitting_threshold", "overfitting_threshold_exact"),
    )

    multiple_testing_method: str = Field(min_length=1)
    multiple_testing_threshold: float = Field(ge=0.0, le=1.0)
    overfitting_metric: str = Field(min_length=1)
    overfitting_threshold: float = Field(ge=0.0, le=1.0)
    trial_count_scope: str = Field(min_length=1)
    reported_only_metrics: tuple[str, ...] = ()
    multiple_testing_threshold_exact: UnitExact | None = Field(default=None, exclude_if=omit_none)
    overfitting_threshold_exact: UnitExact | None = Field(default=None, exclude_if=omit_none)
    #: CSCV 分块数（C-T1 / C-R1；ADR-0052 §2）：偶数且 `>= 2`。缺失时沿用 `param:cscv_partitions`。
    cscv_partitions: CscvPartitions | None = Field(default=None, exclude_if=omit_none)
    #: G1 负对照的独立显著性阈值（C-L6；ADR-0052 §3）：对照 p 必须 `>=` 它。缺失时负对照
    #: 沿用 `multiple_testing_threshold`（旧行为；`threshold_source` 如实写出所用字段）。
    negative_control_threshold: UnitExact | None = Field(default=None, exclude_if=omit_none)


class BenchmarkParams(ExactBacked):
    """对应 Constitution C-T4：空模型为主，市场基准按类别适用。"""

    _FIELDS_SINCE = {"null_model_percentile_exact": ADR_0052_VERSION}

    _EXACT_SIBLINGS = (("null_model_percentile", "null_model_percentile_exact"),)

    null_model: str = Field(min_length=1)
    null_model_simulations: int = Field(gt=0)
    null_model_percentile: float = Field(ge=0.0, le=100.0)
    market_benchmark_rule: str = Field(min_length=1)
    inverse_control_reported: bool
    null_model_percentile_exact: PercentileExact | None = Field(default=None, exclude_if=omit_none)


class ParameterStabilityParams(ExactBacked):
    """对应 Constitution C-R1。"""

    _FIELDS_SINCE = {
        "min_neighborhood_performance_ratio_exact": ADR_0052_VERSION,
        "min_positive_neighbor_fraction_exact": ADR_0052_VERSION,
    }

    _EXACT_SIBLINGS = (
        ("min_neighborhood_performance_ratio", "min_neighborhood_performance_ratio_exact"),
        ("min_positive_neighbor_fraction", "min_positive_neighbor_fraction_exact"),
    )

    neighborhood_definition: str = Field(min_length=1)
    min_neighborhood_performance_ratio: float
    min_positive_neighbor_fraction: float = Field(ge=0.0, le=1.0)
    time_alignment_offsets: tuple[timedelta, ...] = ()
    min_neighborhood_performance_ratio_exact: ExactDecimal | None = Field(
        default=None, exclude_if=omit_none
    )
    min_positive_neighbor_fraction_exact: UnitExact | None = Field(
        default=None, exclude_if=omit_none
    )


class CostStressParams(ExactBacked):
    """对应 Constitution C-R4 与 A6。

    结构不变量（ADR-0014 §D-20.4）：两个倍数序列的**每一项**严格为正；
    `cost_model` 必须指向 `cost_model` kind——这是跨字段语义，JSON Schema 不表达它，
    由运行时校验保证。
    """

    _FIELDS_SINCE = {
        "stress_multipliers_exact": ADR_0052_VERSION,
        "reported_only_multipliers_exact": ADR_0052_VERSION,
        "min_breakeven_cost_multiple_exact": ADR_0052_VERSION,
    }

    _EXACT_SIBLINGS = (
        ("stress_multipliers", "stress_multipliers_exact"),
        ("reported_only_multipliers", "reported_only_multipliers_exact"),
        ("min_breakeven_cost_multiple", "min_breakeven_cost_multiple_exact"),
    )

    cost_model: Ref
    fill_assumption: str = Field(min_length=1)
    stress_multipliers: tuple[PositiveMultiplier, ...] = Field(min_length=1)
    reported_only_multipliers: tuple[PositiveMultiplier, ...] = ()
    delay_stress_bars: int = Field(ge=0)
    min_breakeven_cost_multiple: float = Field(gt=0.0)
    #: 逐项的精确兄弟序列：长度与浮点序列相同、逐项派生（ADR-0052 §1）。
    stress_multipliers_exact: tuple[PositiveExact, ...] | None = Field(
        default=None, exclude_if=omit_none
    )
    reported_only_multipliers_exact: tuple[PositiveExact, ...] | None = Field(
        default=None, exclude_if=omit_none
    )
    min_breakeven_cost_multiple_exact: PositiveExact | None = Field(
        default=None, exclude_if=omit_none
    )

    @model_validator(mode="after")
    def _cost_model_kind(self) -> CostStressParams:
        if self.cost_model.kind is not Kind.COST_MODEL:
            raise ValueError(
                f"CostStressParams.cost_model 必须指向 cost_model，"
                f"收到 {self.cost_model.kind.value}"
            )
        return self


class LifecycleParams(ExactBacked):
    """对应 Constitution C-G3 与 ADR-0006（Q-6 仍开放）。

    结构不变量（ADR-0014 §D-20.4）：`paper_period > 0`——观察期必须有长度。
    本约束只管符号，不回答"观察期该多长"（Phase 4）或"该由谁定义"（Q-6）。
    """

    _FIELDS_SINCE = {"degradation_thresholds_exact": ADR_0052_VERSION}

    _EXACT_SIBLINGS = (("degradation_thresholds", "degradation_thresholds_exact"),)

    paper_period: PositiveDuration
    paper_acceptance_rule: str = Field(min_length=1)
    degradation_thresholds: FrozenMapping[str, float] = Field(
        default_factory=dict, validate_default=True
    )
    #: 逐键的精确兄弟映射：键集与浮点映射相同、逐键派生（ADR-0052 §1）。
    degradation_thresholds_exact: FrozenMapping[str, ExactDecimal] | None = Field(
        default=None, exclude_if=omit_none
    )


class CapacityParams(Contract):
    """对应 Constitution C-R5：容量与冲击（ADR-0052 §2；只有字段与结构范围，没有数值）。

    每个字段可选（缺失的项由 G4 按 ADR-0041 §1 处理：`param:` 或 `INCONCLUSIVE`），但给出
    `capacity` 就至少给一项——空块不是配置。`impact_model` 必须是研究代码已实现的方法名
    （运行时由 G4 拒绝未实现的方法；契约层只约束非空）。
    """

    _MODEL_SINCE = ADR_0052_VERSION  # the whole model is new in 2.1.0

    min_capacity: PositiveExact | None = Field(default=None, exclude_if=omit_none)
    max_participation_rate: UnitPositiveExact | None = Field(default=None, exclude_if=omit_none)
    impact_coefficient: NonNegativeExact | None = Field(default=None, exclude_if=omit_none)
    impact_model: Annotated[str, Field(min_length=1)] | None = Field(
        default=None, exclude_if=omit_none
    )

    @model_validator(mode="after")
    def _not_empty(self) -> CapacityParams:
        fields = (
            self.min_capacity,
            self.max_participation_rate,
            self.impact_coefficient,
            self.impact_model,
        )
        if all(value is None for value in fields):
            raise ValueError("capacity 至少给出一个字段；不配置容量时省略 capacity")
        return self


class CrossAssetParams(Contract):
    """对应 Constitution C-R3：声明范围内跨资产一致性（ADR-0052 §2）。"""

    _MODEL_SINCE = ADR_0052_VERSION  # the whole model is new in 2.1.0

    min_positive_fraction: UnitExact


class Provenance(Contract):
    """Step 2 冻结依据。"""

    calibration_report: str | None = None
    approval_adr: str | None = None
    previous_version: str | None = None


class ValidationProfile(ExactBacked, VersionedSpec):
    """按"标的 × 周期 × 研究类别"给出的版本化阈值集合。

    不可变性：`status = frozen` 的版本永不修改；修正 = 新版本（Constitution C-A5）。
    完整性：五类门槛必须齐全，否则 Profile 无效（Constitution C-A7）。
    """

    _FIELDS_SINCE = {
        "inconclusive_bands_exact": ADR_0052_VERSION,
        "capacity": ADR_0052_VERSION,
        "cross_asset": ADR_0052_VERSION,
    }

    kind: Literal[Kind.PROFILE] = Kind.PROFILE
    status: ProfileStatus = ProfileStatus.DRAFT
    scope: ProfileScope
    data_split: DataSplitParams
    sample_size: SampleSizeParams
    significance: SignificanceParams
    benchmark: BenchmarkParams
    parameter_stability: ParameterStabilityParams
    cost_stress: CostStressParams
    _EXACT_SIBLINGS = (("inconclusive_bands", "inconclusive_bands_exact"),)

    lifecycle: LifecycleParams
    inconclusive_bands: FrozenMapping[str, float] = Field(
        default_factory=dict, validate_default=True
    )
    provenance: Provenance = Provenance()
    #: 逐键的精确兄弟映射（ADR-0052 §1）。
    inconclusive_bands_exact: FrozenMapping[str, ExactDecimal] | None = Field(
        default=None, exclude_if=omit_none
    )
    #: C-R5 容量（ADR-0052 §2）；缺失时 G4 沿用 `param:capacity.*` 或 `INCONCLUSIVE`。
    capacity: CapacityParams | None = Field(default=None, exclude_if=omit_none)
    #: C-R3 跨资产一致性（ADR-0052 §2）；缺失时 G4 沿用 `param:cross_asset.min_positive_fraction`。
    cross_asset: CrossAssetParams | None = Field(default=None, exclude_if=omit_none)

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
