"""知识、假设、实验与验证记录。

对应 docs/architecture/02-domain.md §2、06-experiment.md、07-validation.md。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import Contract, Kind, Ref, UtcDatetime, VersionedSpec
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
    """复现元组（06-experiment.md §2，冻结）。缺任一项则不得进入 Validation。"""

    hypothesis_ref: Ref
    dataset_snapshots: tuple[DatasetRef, ...] = Field(min_length=1)
    code_commit: str = Field(min_length=7)
    plugin_versions: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    param_search_space: dict[str, tuple[str | int | float | bool, ...]] = Field(default_factory=dict)
    seeds: tuple[int, ...] = ()
    environment_lock: str = Field(min_length=1)
    constitution_version: str = Field(min_length=1)
    validation_profile_version: str = Field(min_length=1)
    validation_profile_hash: str = Field(min_length=1)
    profile_selection: dict[str, str] = Field(default_factory=dict)
    split_spec: str = Field(min_length=1)
    cost_model_ref: Ref
    llm_calls: tuple[LlmCall, ...] = ()

    @model_validator(mode="after")
    def _hypothesis_kind(self) -> ReproducibilityTuple:
        if self.hypothesis_ref.kind is not Kind.HYPOTHESIS:
            raise ValueError("hypothesis_ref 必须指向 hypothesis")
        return self

    @property
    def experiment_hash(self) -> str:
        return self.content_hash()


class ExperimentSpec(VersionedSpec):
    """对一个 Hypothesis 的完整、可执行、可复现的检验规格。"""

    kind: Kind = Kind.EXPERIMENT
    repro: ReproducibilityTuple
    strategy: Ref | None = None
    risk_policy: Ref | None = None
    outcome: Ref | None = None


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
    """ExperimentSpec 的一次执行。"""

    run_id: str = Field(min_length=1)
    experiment: Ref
    repro: ReproducibilityTuple
    state: RunState = RunState.REGISTERED
    trace_id: str | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None

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
    """按 Constitution + Validation Profile 执行的判定结果。"""

    report_id: str = Field(min_length=1)
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
        if any(gate.verdict is Verdict.FAIL for gate in self.gates) and self.verdict is Verdict.PASS:
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
    """RETIRED 的追加式退役记录（07-validation.md §4.2）。RETIRED ≠ FAILED。"""

    subject_ref: Ref
    retirement_reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    active_from: UtcDatetime | None = None
    active_to: UtcDatetime | None = None
    execution_mode: str | None = None
    lessons: str | None = None
    recorded_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
