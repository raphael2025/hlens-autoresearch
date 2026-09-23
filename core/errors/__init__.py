"""领域错误分类。

供 Failure Registry 的 `reason_code` 与契约校验使用（docs/architecture/02-domain.md §3、
docs/architecture/07-validation.md §4.1）。本模块只依赖标准库。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ContractViolation",
    "HlensError",
    "LeakageDetected",
    "LifecycleViolation",
    "ProfileViolation",
    "ReasonCode",
    "ReproducibilityFailure",
    "ReasonCategory",
]


class ReasonCategory(StrEnum):
    """reason_code 的大类。"""

    DATA = "data"
    LEAKAGE = "leakage"
    STATISTICS = "statistics"
    ROBUSTNESS = "robustness"
    REPRODUCIBILITY = "reproducibility"
    CONTRACT = "contract"
    HUMAN = "human"


class ReasonCode(StrEnum):
    """终态记录使用的原因码。

    REJECTED / FAILED 写入 Failure Registry；RETIRED 使用退役记录（07-validation.md §4.2）。
    """

    # data
    DATA_QUALITY = "DATA_QUALITY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    # leakage
    LEAKAGE_DETECTED = "LEAKAGE_DETECTED"
    LOOKAHEAD_INPUT = "LOOKAHEAD_INPUT"
    OUTCOME_USED_AS_INPUT = "OUTCOME_USED_AS_INPUT"
    # statistics
    NOT_SIGNIFICANT_AFTER_MTC = "NOT_SIGNIFICANT_AFTER_MTC"
    INSUFFICIENT_EFFECTIVE_SAMPLE = "INSUFFICIENT_EFFECTIVE_SAMPLE"
    BENCHMARK_NOT_BEATEN = "BENCHMARK_NOT_BEATEN"
    # robustness
    PARAM_UNSTABLE = "PARAM_UNSTABLE"
    STATE_CONCENTRATED = "STATE_CONCENTRATED"
    COST_KILLED = "COST_KILLED"
    OOS_DECAY = "OOS_DECAY"
    # reproducibility
    NOT_REPRODUCIBLE = "NOT_REPRODUCIBLE"
    RUN_ERRORED = "RUN_ERRORED"
    # contract
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    PROFILE_VIOLATION = "PROFILE_VIOLATION"
    LIFECYCLE_VIOLATION = "LIFECYCLE_VIOLATION"
    # human
    HUMAN_VETO = "HUMAN_VETO"
    DUPLICATE = "DUPLICATE"
    NOT_FALSIFIABLE = "NOT_FALSIFIABLE"

    @property
    def category(self) -> ReasonCategory:
        return _REASON_CATEGORY[self]


_REASON_CATEGORY: dict[ReasonCode, ReasonCategory] = {
    ReasonCode.DATA_QUALITY: ReasonCategory.DATA,
    ReasonCode.INSUFFICIENT_DATA: ReasonCategory.DATA,
    ReasonCode.LEAKAGE_DETECTED: ReasonCategory.LEAKAGE,
    ReasonCode.LOOKAHEAD_INPUT: ReasonCategory.LEAKAGE,
    ReasonCode.OUTCOME_USED_AS_INPUT: ReasonCategory.LEAKAGE,
    ReasonCode.NOT_SIGNIFICANT_AFTER_MTC: ReasonCategory.STATISTICS,
    ReasonCode.INSUFFICIENT_EFFECTIVE_SAMPLE: ReasonCategory.STATISTICS,
    ReasonCode.BENCHMARK_NOT_BEATEN: ReasonCategory.STATISTICS,
    ReasonCode.PARAM_UNSTABLE: ReasonCategory.ROBUSTNESS,
    ReasonCode.STATE_CONCENTRATED: ReasonCategory.ROBUSTNESS,
    ReasonCode.COST_KILLED: ReasonCategory.ROBUSTNESS,
    ReasonCode.OOS_DECAY: ReasonCategory.ROBUSTNESS,
    ReasonCode.NOT_REPRODUCIBLE: ReasonCategory.REPRODUCIBILITY,
    ReasonCode.RUN_ERRORED: ReasonCategory.REPRODUCIBILITY,
    ReasonCode.CONTRACT_VIOLATION: ReasonCategory.CONTRACT,
    ReasonCode.PROFILE_VIOLATION: ReasonCategory.CONTRACT,
    ReasonCode.LIFECYCLE_VIOLATION: ReasonCategory.CONTRACT,
    ReasonCode.HUMAN_VETO: ReasonCategory.HUMAN,
    ReasonCode.DUPLICATE: ReasonCategory.HUMAN,
    ReasonCode.NOT_FALSIFIABLE: ReasonCategory.HUMAN,
}


class HlensError(Exception):
    """所有领域错误的基类。"""

    reason_code: ReasonCode = ReasonCode.CONTRACT_VIOLATION


class ContractViolation(HlensError):
    """契约被违反（缺字段、非法引用、版本不兼容…）。"""


class LifecycleViolation(HlensError):
    """生命周期状态机不允许的转移或缺少必要批准（ADR-0006）。"""

    reason_code = ReasonCode.LIFECYCLE_VIOLATION


class ProfileViolation(HlensError):
    """违反 Validation Profile 的绑定 / 不可变 / 选择规则（ADR-0007）。"""

    reason_code = ReasonCode.PROFILE_VIOLATION


class LeakageDetected(HlensError):
    """检测到信息泄漏（Constitution 第三章）。"""

    reason_code = ReasonCode.LEAKAGE_DETECTED


class ReproducibilityFailure(HlensError):
    """复现元组不完整或重跑结果不一致（Constitution 第七章）。"""

    reason_code = ReasonCode.NOT_REPRODUCIBLE
