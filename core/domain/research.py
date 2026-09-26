"""知识、假设、实验与验证记录。

对应 docs/architecture/02-domain.md §2、06-experiment.md、07-validation.md。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import Field, model_validator

from core.domain.base import (
    SEMVER_PATTERN,
    ContentBlobRef,
    ContentHash,
    Contract,
    ExactBacked,
    ExactDecimal,
    FrozenMapping,
    GitOid,
    Kind,
    PluginKey,
    Ref,
    RefKey,
    UtcDatetime,
    VersionedSpec,
    omit_none,
    validate_ref_keyed_hashes,
)
from core.domain.execution import ExecutionMode
from core.domain.selection import ProfileSelection
from core.domain.specs import DatasetRef
from core.errors import ReasonCode

#: The minor that introduced this module's ADR-0052 fields (never under 2.0.0).
ADR_0052_VERSION: Final = "2.1.0"

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
    "derive_verdict",
    "require_unique_gate_ids",
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

    kind: Literal[Kind.KNOWLEDGE] = Kind.KNOWLEDGE
    source: str = Field(min_length=1)
    license: str
    claim: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    evidence_level: EvidenceLevel
    status: KnowledgeStatus = KnowledgeStatus.UNVERIFIED
    links: tuple[Ref, ...] = ()


class Hypothesis(VersionedSpec):
    """可证伪的陈述；运行前预登记且不可变（Constitution A1 / A2）。"""

    kind: Literal[Kind.HYPOTHESIS] = Kind.HYPOTHESIS
    family_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    conditions: tuple[str, ...] = ()
    expected_direction: str = Field(min_length=1)
    minimum_meaningful_effect: str = Field(min_length=1)
    origin: HypothesisOrigin
    origin_refs: tuple[Ref, ...] = ()


class LlmCall(Contract):
    """LLM 调用记录：LLM 只产出数据，永不裁决（09-security.md §3）。

    ADR-0016 §D-18.2：`prompt` / `input` / `output` 三项**全部必填**，且都是
    `ContentBlobRef`（取回引用 + 内容哈希）。一次 LLM 调用总是有提示、有输入、有输出；
    允许其中任何一项缺失，等于允许记录一次无法复核的调用。原先的三个自由字符串
    `prompt_hash` / `input_hash` / `output_hash` 被它们取代——哈希仍在，位置在
    `ContentBlobRef.sha256`，并第一次带上了取回路径与格式约束。

    `called_at` **显式必填且没有默认值**：若默认成 `datetime.now(UTC)`，那么稍后构造 DTO
    的时刻就会冒充调用时刻，而审计记录无法分辨两者。宁可拒绝缺 `called_at` 的载荷，
    也不要一个看起来合法、实则错误的调用时间。

    **诚实边界（ADR-0016 §D-18.3）**：本模型完成的是**登记结构**。在存储层就位并能取回
    内容之前，`06-experiment.md` §2 的"完整输入输出"要求**仍未满足**：URI 可取回性、
    取回内容与 `sha256` 是否一致、登记内容是否不可覆盖、一次实验是否登记了**所有**
    发生过的调用，全部是存储层 / Registry / Runner 的未实现义务。本模型不自报验证结果。
    """

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt: ContentBlobRef
    input: ContentBlobRef
    output: ContentBlobRef
    called_at: UtcDatetime


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
    #: 完整 Git OID（40 / 64 位小写十六进制）：短 SHA 不是可审计的代码身份（ADR-0015 §D-21.2）。
    code_commit: GitOid
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
    #: "锁文件哈希 + Python 版本 + 平台"的**复合描述**；结构化表达后续另定（ADR-0015 §D-21.3）。
    environment_lock: str = Field(min_length=1)
    constitution_version: str = Field(pattern=SEMVER_PATTERN)
    #: Profile 绑定 = 已校验的引用 + 内容哈希，不再是自由字符串（ADR-0015 §D-22.2）。
    validation_profile: Ref
    validation_profile_hash: ContentHash
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
            ("validation_profile", self.validation_profile, Kind.PROFILE),
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

    kind: Literal[Kind.EXPERIMENT] = Kind.EXPERIMENT
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


class GateResult(ExactBacked):
    """单个验证门的结构化结果（07-validation.md §2）。

    `threshold_source` 指向 Validation Profile 中的字段路径：阈值不得写死在流水线里。

    精确表示（ADR-0052 §1，D-FLOAT）：可选的 `value_exact` / `threshold_exact`。存在时浮点字段
    必须恰为 `float(精确值)`（否则拒绝），哈希载荷排除对应浮点字段，判定由产生方用精确值比较；
    不存在时省略出载荷，旧载荷哈希逐位不变。一个带阈值的门要么两者都精确、要么都不精确
    （混合表示的比较不可复核，拒绝）。浮点字段弃用，保留到下一次因其他原因发生的 major。
    """

    _FIELDS_SINCE = {"value_exact": ADR_0052_VERSION, "threshold_exact": ADR_0052_VERSION}

    _EXACT_SIBLINGS = (("value", "value_exact"), ("threshold", "threshold_exact"))

    gate_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: float
    threshold: float | None = None
    threshold_source: str | None = None
    verdict: Verdict
    value_exact: ExactDecimal | None = Field(default=None, exclude_if=omit_none)
    threshold_exact: ExactDecimal | None = Field(default=None, exclude_if=omit_none)

    @model_validator(mode="after")
    def _exact_representation_is_whole(self) -> GateResult:
        """带阈值的门：`value_exact` 与 `threshold_exact` 同时存在或同时缺失（ADR-0052 §1）。"""
        if self.threshold_exact is not None and self.value_exact is None:
            raise ValueError("threshold_exact 需要 value_exact：精确阈值只能与精确值比较")
        if self.value_exact is not None and self.threshold is not None:
            if self.threshold_exact is None:
                raise ValueError(
                    "value_exact 与浮点阈值混用：带阈值的精确门必须给出 threshold_exact"
                )
        return self

    @model_validator(mode="after")
    def _threshold_and_source_are_paired(self) -> GateResult:
        """`threshold` 与 `threshold_source` 同时存在或同时缺失（ADR-0013 D-19.3）。

        有阈值必须声明非空来源（ADR-0007 的既有语义，空白来源仍然拒绝）；
        反向同样成立：声明了任何非 `None` 的来源却没有阈值，也是不完整的门记录。
        无阈值的纯报告项两者都留空。

        **不校验来源路径的真实性**：`threshold_source` 是否真的指向所绑定 Profile
        版本中的字段、其值是否等于 `threshold`，只有持有 Profile 实例的验证服务能核验；
        契约层拿不到该实例，因此这里只是格式配对，不是来源已核实。
        """
        if self.threshold is not None and not self.threshold_source:
            raise ValueError("阈值必须声明来源 Profile 字段（ADR-0007）")
        if self.threshold_source is not None and self.threshold is None:
            raise ValueError("声明了 threshold_source 就必须给出 threshold（ADR-0013 D-19.3）")
        return self


def derive_verdict(gates: Sequence[GateResult]) -> Verdict:
    """门结果集合 → 整体判定的**全函数**（ADR-0013 D-19.1）。

    任一 `FAIL` → `FAIL`；否则任一 `INCONCLUSIVE` → `INCONCLUSIVE`；否则 `PASS`。
    三种情形覆盖了所有可能的门结果集合，因此这是一个确定性的全函数。

    这是"证据不足不得等同于 PASS"的可执行形式：任何想让判定偏离本函数的理由
    （证据不足、数据质量、样本量、人工保留意见、外部事件）都必须**物化为报告内的一个门**，
    不得通过直接设置 `verdict` 表达。
    """
    verdicts = {gate.verdict for gate in gates}
    if Verdict.FAIL in verdicts:
        return Verdict.FAIL
    if Verdict.INCONCLUSIVE in verdicts:
        return Verdict.INCONCLUSIVE
    return Verdict.PASS


def require_unique_gate_ids(gates: Sequence[GateResult], label: str) -> None:
    """同一集合内 `gate_id` 不得重复（ADR-0013 D-19.2）。

    重复的门意味着同一检查有两个结果，`derive_verdict` 将不再良定义。
    """
    seen = [gate.gate_id for gate in gates]
    duplicates = sorted({gate_id for gate_id in seen if seen.count(gate_id) > 1})
    if duplicates:
        raise ValueError(f"{label} 存在重复的 gate_id：{duplicates}")


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
    experiment_hash: ContentHash
    constitution_version: str = Field(pattern=SEMVER_PATTERN)
    validation_profile: Ref
    validation_profile_hash: ContentHash
    gates: tuple[GateResult, ...] = Field(min_length=1)
    verdict: Verdict
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _profile_kind(self) -> ValidationReport:
        """`validation_profile` 必须指向 `profile`（ADR-0015 §D-22.2）。

        **不校验**该版本是否已登记、是否 frozen，也不校验它与 Run / 元数据的绑定是否一致：
        契约层拿不到另外两个实例与 Registry，这些属 Control Plane。
        """
        if self.validation_profile.kind is not Kind.PROFILE:
            raise ValueError("validation_profile 必须指向 profile")
        return self

    @model_validator(mode="after")
    def _verdict_is_the_gate_function(self) -> ValidationReport:
        """`verdict` 必须**精确等于** `derive_verdict(gates)`（ADR-0013 D-19.1）。

        这是双向检查，不是"不得为 PASS"这类单向检查：全部门 PASS 却判 FAIL、
        证据不足却判 PASS，都同样被拒绝。
        """
        require_unique_gate_ids(self.gates, "ValidationReport.gates")
        expected = derive_verdict(self.gates)
        if self.verdict is not expected:
            raise ValueError(
                f"整体判定必须等于门结果的确定性函数：gates ⇒ {expected.value}，"
                f"但 verdict = {self.verdict.value}；报告外的理由必须物化为一个门"
            )
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
