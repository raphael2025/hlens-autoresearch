"""知识、假设、实验与验证记录。

对应 docs/architecture/02-domain.md §2、06-experiment.md、07-validation.md。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import (
    ContentHash,
    Contract,
    FrozenMapping,
    Kind,
    PluginKey,
    Ref,
    RefKey,
    UtcDatetime,
    VersionedSpec,
    validate_ref_keyed_hashes,
)
from core.domain.execution import ExecutionMode
from core.domain.selection import ProfileSelection
from core.domain.specs import DatasetRef
from core.errors import ReasonCode

__all__ = [
    "EvidenceLevel",
    "ExperimentRun",
    "ExperimentSpec",
    "FailureRecord",
    "GateResult",
    "Hypothesis",
    "HypothesisOrigin",
    "KnowledgeItem",
    "KnowledgeStatus",
    "LlmCall",
    "ReproducibilityTuple",
    "RetirementRecord",
    "RunState",
    "ValidationReport",
    "Verdict",
]


class EvidenceLevel(StrEnum):
    """知识条目的证据等级（docs/research/knowledge-base.md）。"""

    E0_ANECDOTE = "E0"
    E1_EXAMPLE = "E1"
    E2_IN_SAMPLE = "E2"
    E3_OUT_OF_SAMPLE = "E3"
    E4_REPLICATED = "E4"


class KnowledgeStatus(StrEnum):
    UNVERIFIED = "unverified"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


class HypothesisOrigin(StrEnum):
    HUMAN = "human"
    KNOWLEDGE = "knowledge"
    COMBINATION = "combination"
    LLM = "llm"


class KnowledgeItem(VersionedSpec):
    """公开来源中的**待检验主张**，不是已验证结论。"""

    kind: Kind = Kind.KNOWLEDGE
    source: str = Field(min_length=1)
    license: str
    claim: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    evidence_level: EvidenceLevel
    status: KnowledgeStatus = KnowledgeStatus.UNVERIFIED
    links: tuple[Ref, ...] = ()


class Hypothesis(VersionedSpec):
    """可证伪的陈述；运行前预登记且不可变（Constitution A1 / A2）。"""

    kind: Kind = Kind.HYPOTHESIS
    family_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    expected_direction: str = Field(min_length=1)
    minimum_meaningful_effect: str = Field(min_length=1)
    origin: HypothesisOrigin
    origin_refs: tuple[Ref, ...] = ()


class LlmCall(Contract):
    """LLM 调用记录：LLM 只产出数据，永不裁决（09-security.md §3）。"""

    provider: str
    model: str
    prompt_hash: str
    input_hash: str
    output_hash: str


class ReproducibilityTuple(Contract):
    """复现元组（06-experiment.md §2，冻结）。缺任一项则不得进入 Validation。

    ADR-0009 §1：策略 / 风控 / Outcome 引用在元组**之内**，因此引用不同策略的实验
    不再得到相同的 `experiment_hash`。三个引用均**必须显式提供**，不适用时明确填 `null`，
    不以缺省掩盖遗漏。

    ADR-0009 §2：实际 `seeds` 保留在元组与哈希内。相同完整规格（含相同 seeds）重复运行
    得到同一 `experiment_hash`（复现检查）；不同 seeds = 新的不可变规格变体 = 新哈希，
    族内关联由 `hypothesis_family_id` 与 trial 计数承担。**不引入**任何种子派生 DSL。
    """

    hypothesis_ref: Ref
    strategy_ref: Ref | None
    risk_policy_ref: Ref | None
    outcome_ref: Ref | None
    dataset_snapshots: tuple[DatasetRef, ...] = Field(min_length=1)
    code_commit: str = Field(min_length=7)
    plugin_versions: FrozenMapping[PluginKey, ContentHash] = Field(
        default_factory=dict, validate_default=True
    )
    dependency_hashes: FrozenMapping[RefKey, ContentHash]
    params: FrozenMapping[str, str | int | float | bool] = Field(
        default_factory=dict, validate_default=True
    )
    param_search_space: FrozenMapping[str, tuple[str | int | float | bool, ...]] = Field(
        default_factory=dict, validate_default=True
    )
    seeds: tuple[int, ...] = ()
    environment_lock: str = Field(min_length=1)
    constitution_version: str = Field(min_length=1)
    validation_profile_version: str = Field(min_length=1)
    validation_profile_hash: str = Field(min_length=1)
    profile_selection: ProfileSelection
    split_spec: str = Field(min_length=1)
    cost_model_ref: Ref
    llm_calls: tuple[LlmCall, ...] = ()

    @model_validator(mode="after")
    def _reference_kinds(self) -> ReproducibilityTuple:
        expected: tuple[tuple[str, Ref | None, Kind], ...] = (
            ("hypothesis_ref", self.hypothesis_ref, Kind.HYPOTHESIS),
            ("strategy_ref", self.strategy_ref, Kind.STRATEGY),
            ("risk_policy_ref", self.risk_policy_ref, Kind.RISK),
            ("outcome_ref", self.outcome_ref, Kind.OUTCOME),
            ("cost_model_ref", self.cost_model_ref, Kind.COST_MODEL),
        )
        for field_name, ref, kind in expected:
            if ref is not None and ref.kind is not kind:
                raise ValueError(f"{field_name} 必须指向 {kind.value}")
        return self

    @model_validator(mode="after")
    def _direct_dependencies_are_bound(self) -> ReproducibilityTuple:
        """直接引用的内容绑定：所有非空引用必须出现在 `dependency_hashes`，缺一即拒绝。

        Profile 与选择规则由各自的专用哈希字段绑定；Dataset 按已冻结的快照身份引用。
        这只是**必要条件**：传递依赖闭包的解析与校验是 Runner / Registry 的义务
        （ADR-0009 §6），本契约不声称已验证完整依赖图。
        """
        validate_ref_keyed_hashes(self.dependency_hashes, "dependency_hashes")
        required = {
            str(ref)
            for ref in (
                self.hypothesis_ref,
                self.strategy_ref,
                self.risk_policy_ref,
                self.outcome_ref,
                self.cost_model_ref,
            )
            if ref is not None
        }
        missing = sorted(required - set(self.dependency_hashes))
        if missing:
            raise ValueError(f"dependency_hashes 未覆盖以下直接引用：{missing}")
        return self

    @property
    def experiment_hash(self) -> str:
        """复现元组规范化 JSON 的 SHA-256（06-experiment.md §3，定义文字不变）。

        覆盖面在 v2 变宽，因此与 v1 的同名值**不可比较**。
        """
        return self.content_hash()


class ExperimentSpec(VersionedSpec):
    """对一个 Hypothesis 的完整、可执行、可复现的检验规格。

    策略 / 风控 / Outcome 只在 `repro` 中存放一份，这里只提供**派生只读属性**
    （ADR-0009 §1）：同一信息两处存放且可能不一致的结构已被消除，
    JSON 与 Schema 也不重复发出这些派生字段。
    """

    kind: Kind = Kind.EXPERIMENT
    repro: ReproducibilityTuple

    @property
    def strategy(self) -> Ref | None:
        return self.repro.strategy_ref

    @property
    def risk_policy(self) -> Ref | None:
        return self.repro.risk_policy_ref

    @property
    def outcome(self) -> Ref | None:
        return self.repro.outcome_ref

    @property
    def experiment_hash(self) -> str:
        return self.repro.experiment_hash


class RunState(StrEnum):
    """ExperimentRun 的执行状态机（06-experiment.md §5）。"""

    REGISTERED = "REGISTERED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERRORED = "ERRORED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"


class ExperimentRun(Contract):
    """ExperimentSpec 的一次执行。

    `run_id` 是**每次尝试唯一的不透明标识**，由 Runner 生成（ADR-0009 §3）：
    本契约**不冻结其生成算法**，也不把它定义为内容哈希。
    `run.repro.experiment_hash` 与所引用 Spec 的一致性校验属 Runner / Registry 义务
    （契约层拿不到 Spec 实例）。
    """

    run_id: str = Field(min_length=1)
    experiment: Ref
    repro: ReproducibilityTuple
    state: RunState = RunState.REGISTERED
    trace_id: str | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _experiment_kind(self) -> ExperimentRun:
        if self.experiment.kind is not Kind.EXPERIMENT:
            raise ValueError("experiment 必须指向 experiment")
        return self

    @model_validator(mode="after")
    def _local_time_order(self) -> ExperimentRun:
        """两者都存在时 `started_at ≤ finished_at`（ADR-0011 D-17.5）。

        这是**记录内部**的一致性；Run 的时间与生命周期转移、报告、监控事件之间的
        全局因果顺序属于未来 Runner / Control Plane，契约层不做跨对象校验。
        """
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at 不得早于 started_at")
        return self

    @property
    def experiment_hash(self) -> str:
        return self.repro.experiment_hash


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class GateResult(Contract):
    """单个验证门的结构化结果（07-validation.md §2）。

    `threshold_source` 指向 Validation Profile 中的字段路径：阈值不得写死在流水线里。
    """

    gate_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: float
    threshold: float | None = None
    threshold_source: str | None = None
    verdict: Verdict

    @model_validator(mode="after")
    def _threshold_needs_source(self) -> GateResult:
        if self.threshold is not None and not self.threshold_source:
            raise ValueError("阈值必须声明来源 Profile 字段（ADR-0007）")
        return self


class ValidationReport(Contract):
    """按 Constitution + Validation Profile 执行的判定结果。

    `run_id` 与 `experiment_hash` 同时存在，形成 spec ↔ run ↔ report 的可核验链
    （ADR-0009 §5）。`report_id` 是外部赋予的标识，**不是**结果内容身份；
    结果内容身份应由未来内容寻址的结果 manifest 提供，不在本轮实现（ADR-0009 §4）。
    `run_id` 与 `report_id` 都**没有**被排除出内容哈希。
    """

    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    subject: Ref
    experiment_hash: str = Field(min_length=1)
    constitution_version: str = Field(min_length=1)
    validation_profile_version: str = Field(min_length=1)
    validation_profile_hash: str = Field(min_length=1)
    gates: tuple[GateResult, ...] = Field(min_length=1)
    verdict: Verdict
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _verdict_consistent(self) -> ValidationReport:
        has_failing_gate = any(gate.verdict is Verdict.FAIL for gate in self.gates)
        if has_failing_gate and self.verdict is Verdict.PASS:
            raise ValueError("存在 FAIL 的门时，整体判定不得为 PASS")
        return self


class FailureRecord(Contract):
    """REJECTED / FAILED 的追加式记录（07-validation.md §4.1）。RETIRED 不在此处。"""

    subject_ref: Ref
    terminal_state: str
    reason_code: ReasonCode
    gate_id: str | None = None
    evidence: tuple[str, ...] = ()
    hypothesis_family_id: str | None = None
    lessons: str | None = None
    recorded_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _only_failure_states(self) -> FailureRecord:
        if self.terminal_state not in {"REJECTED", "FAILED"}:
            raise ValueError(
                "Failure Registry 只记录 REJECTED / FAILED；RETIRED 使用 RetirementRecord"
            )
        return self


class RetirementRecord(Contract):
    """RETIRED 的追加式退役记录（07-validation.md §4.2）。RETIRED ≠ FAILED。

    `execution_mode` 复用共享的 `ExecutionMode` 枚举（`core/domain/execution.py`，
    ADR-0011 D-17.5）：同一概念不再用自由字符串表达，也只有一个权威定义。
    """

    subject_ref: Ref
    retirement_reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    active_from: UtcDatetime | None = None
    active_to: UtcDatetime | None = None
    execution_mode: ExecutionMode | None = None
    lessons: str | None = None
    recorded_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _active_period_is_ordered(self) -> RetirementRecord:
        """两者都存在时 `active_from ≤ active_to`（ADR-0011 D-17.5）。"""
        if (
            self.active_from is not None
            and self.active_to is not None
            and self.active_to < self.active_from
        ):
            raise ValueError("active_to 不得早于 active_from")
        return self
