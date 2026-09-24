"""Strategy Lifecycle v2 状态机（ADR-0006，Accepted 2026-09-23）。

顺序：IDEA → CANDIDATE → VALIDATION → OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE
      → DEGRADED → REVALIDATION → ACTIVE / RETIRED

关键规则：
* DEGRADED 永远不能直接转为 ACTIVE，必须经 REVALIDATION。
* RETIRED ≠ FAILED：RETIRED 进退役记录，REJECTED / FAILED 进 Failure Registry。
* LIVE 不是状态，而是 ACTIVE 的 `execution_mode`；Phase 13 之前只允许 SIMULATED —— 该红线由
  未来 Control Plane 的可信配置与人类授权执行，**不**由本模块的 DTO 自证（ADR-0011 D-17.4）。
* 每次转移只追加、可审计。

`ExecutionMode` 的权威定义在 `core/domain/execution.py`（Domain 不得依赖本模块），
这里只重导出，公共导入路径 `core.lifecycle.strategy.ExecutionMode` 保持不变。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import Contract, FrozenMapping, Ref, UtcDatetime
from core.domain.execution import ExecutionMode
from core.errors import LifecycleViolation

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AuthorizationRecord",
    "ExecutionMode",
    "ExecutionModeChange",
    "FAILURE_REGISTRY_STATES",
    "HUMAN_APPROVAL_TRANSITIONS",
    "LifecycleHistory",
    "LifecycleState",
    "LifecycleTransition",
    "RETIREMENT_STATES",
    "TERMINAL_STATES",
    "RiskGateRecord",
    "validate_transition",
]


class LifecycleState(StrEnum):
    IDEA = "IDEA"
    CANDIDATE = "CANDIDATE"
    VALIDATION = "VALIDATION"
    OOS = "OOS"
    PAPER = "PAPER"
    PRODUCTION_CANDIDATE = "PRODUCTION_CANDIDATE"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    REVALIDATION = "REVALIDATION"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


S = LifecycleState

#: 唯一允许的转移集合。**不得**在此之外新增转移（ADR-0006 §3 第 1 条）。
ALLOWED_TRANSITIONS: frozenset[tuple[LifecycleState, LifecycleState]] = frozenset(
    {
        (S.IDEA, S.CANDIDATE),
        (S.CANDIDATE, S.VALIDATION),
        (S.VALIDATION, S.OOS),
        (S.OOS, S.PAPER),
        (S.PAPER, S.PRODUCTION_CANDIDATE),
        (S.PRODUCTION_CANDIDATE, S.ACTIVE),
        (S.ACTIVE, S.DEGRADED),
        (S.DEGRADED, S.REVALIDATION),
        (S.REVALIDATION, S.ACTIVE),
        (S.REVALIDATION, S.RETIRED),
        (S.ACTIVE, S.RETIRED),
        (S.PAPER, S.RETIRED),
        (S.IDEA, S.REJECTED),
        (S.CANDIDATE, S.FAILED),
        (S.VALIDATION, S.REJECTED),
        (S.OOS, S.REJECTED),
        (S.PAPER, S.REJECTED),
        (S.PRODUCTION_CANDIDATE, S.REJECTED),
    }
)

#: 终态。重试 = 新版本的新 CANDIDATE。
TERMINAL_STATES: frozenset[LifecycleState] = frozenset({S.RETIRED, S.REJECTED, S.FAILED})

#: 进入 Failure Registry 的终态（RETIRED 不在其中）。
FAILURE_REGISTRY_STATES: frozenset[LifecycleState] = frozenset({S.REJECTED, S.FAILED})

#: 进入退役记录的终态。
RETIREMENT_STATES: frozenset[LifecycleState] = frozenset({S.RETIRED})

#: 需要人工批准的转移（ADR-0006 §3 第 6 条；细节调整见 Q-4）。
#: `REVALIDATION → RETIRED` 由 ADR-0011 D-17.1 纳入：失败的 revalidation report 是**证据**，
#: 不是执行者；退役是不可逆终态，必须由人在审阅报告后签字。这只改批准集合，不增删任何边。
HUMAN_APPROVAL_TRANSITIONS: frozenset[tuple[LifecycleState, LifecycleState]] = frozenset(
    {
        (S.OOS, S.PAPER),
        (S.PRODUCTION_CANDIDATE, S.ACTIVE),
        (S.REVALIDATION, S.ACTIVE),
        (S.REVALIDATION, S.RETIRED),
        (S.ACTIVE, S.RETIRED),
        (S.PAPER, S.RETIRED),
    }
)


class LifecycleTransition(Contract):
    """一次生命周期转移记录：只追加，永不修改（ADR-0006 §3 第 4 条）。"""

    subject: Ref
    from_state: LifecycleState
    to_state: LifecycleState
    reason: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()
    triggered_by: str = Field(min_length=1)
    approved_by: str | None = None
    occurred_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))


def validate_transition(
    from_state: LifecycleState,
    to_state: LifecycleState,
    *,
    approved_by: str | None = None,
) -> None:
    """校验一次转移；非法转移或缺批准时抛 `LifecycleViolation`。"""
    if from_state in TERMINAL_STATES:
        raise LifecycleViolation(f"{from_state} 是终态，不得再转移；重试请创建新版本的 CANDIDATE")
    if (from_state, to_state) not in ALLOWED_TRANSITIONS:
        raise LifecycleViolation(f"不允许的转移：{from_state} → {to_state}（ADR-0006 §3）")
    if (from_state, to_state) in HUMAN_APPROVAL_TRANSITIONS and not approved_by:
        raise LifecycleViolation(f"{from_state} → {to_state} 需要人工批准（approved_by）")


class RiskGateRecord(Contract):
    """实盘前的独立 Risk Gate 检查（与 RiskProvider 分离）。

    `subject` 绑定被检查的对象（ADR-0011 D-17.3）：属于另一个对象的检查不得被挂到
    一次 `ExecutionModeChange` 上。检查**内容**与风险预算单位不在契约层定义。
    """

    subject: Ref
    gate_id: str = Field(min_length=1)
    passed: bool
    limits: FrozenMapping[str, str] = Field(default_factory=dict, validate_default=True)
    checked_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthorizationRecord(Contract):
    """实盘授权记录（ADR-0006 §2）。

    `subject` 绑定被授权对象，授权窗口必须为正（ADR-0011 D-17.3）。
    `authorized_by` **是否真的有权批准**由未来的授权服务核验，契约层只校验证据结构。
    """

    subject: Ref
    authorized_by: str = Field(min_length=1)
    risk_budget: str = Field(min_length=1)
    valid_until: UtcDatetime
    authorized_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _window_is_positive(self) -> AuthorizationRecord:
        if self.valid_until <= self.authorized_at:
            raise ValueError("授权窗口无效：valid_until 必须晚于 authorized_at")
        return self


class ExecutionModeChange(Contract):
    """`execution_mode` 变更的独立审计事件。

    切到 LIVE 需要：对象处于 ACTIVE、通过独立 Risk Gate、有授权记录，且两份证据都属于
    本次变更的 `subject`、授权覆盖变更时刻、Risk Gate 不晚于变更（ADR-0011 D-17.3）。

    **诚实边界**（ADR-0011 D-17.4）：契约层只校验可核验的**证据结构**，
    **不再**拒绝 `to_mode = LIVE`。"Phase 13 之前禁止真实生产交易"
    （ADR-0006 §3 第 5 条、CLAUDE.md H10）依然有效，但它的执行点在未来 Control Plane 的
    可信配置与授权服务以及人类授权，不在这个 DTO —— 一个载荷无法自证自己的运行环境。
    被删除的 `live_execution_enabled` 是自报字段，从未提供过真实防护。
    """

    subject: Ref
    state: LifecycleState
    from_mode: ExecutionMode
    to_mode: ExecutionMode
    risk_gate: RiskGateRecord | None = None
    authorization: AuthorizationRecord | None = None
    occurred_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _evidence_is_consistent(self) -> ExecutionModeChange:
        if self.state is not LifecycleState.ACTIVE:
            raise ValueError("execution_mode 只适用于 ACTIVE 状态（ADR-0006 §2）")
        if self.from_mode is self.to_mode:
            raise ValueError("execution_mode 变更事件必须真的改变模式：from_mode 与 to_mode 相同")
        if self.to_mode is not ExecutionMode.LIVE:
            return self
        if self.risk_gate is None or not self.risk_gate.passed:
            raise ValueError("切换到 LIVE 需要通过独立 Risk Gate")
        if self.authorization is None:
            raise ValueError("切换到 LIVE 需要明确的授权记录")
        # 比较目标身份而非全结构相等：信封版本不同的同一目标仍是同一对象（ADR-0018 §D-26.5）。
        if self.risk_gate.subject.target_identity() != self.subject.target_identity():
            raise ValueError("Risk Gate 的 subject 必须与本次变更的 subject 一致")
        if self.authorization.subject.target_identity() != self.subject.target_identity():
            raise ValueError("授权记录的 subject 必须与本次变更的 subject 一致")
        if not (
            self.authorization.authorized_at <= self.occurred_at <= self.authorization.valid_until
        ):
            raise ValueError("授权必须覆盖变更时刻：authorized_at ≤ occurred_at ≤ valid_until")
        if self.risk_gate.checked_at > self.occurred_at:
            raise ValueError("Risk Gate 不得晚于它所批准的变更")
        return self


class LifecycleHistory(Contract):
    """某个对象的完整生命周期历史：只追加，可审计。"""

    subject: Ref
    transitions: tuple[LifecycleTransition, ...] = ()

    @property
    def current_state(self) -> LifecycleState:
        if not self.transitions:
            return LifecycleState.IDEA
        return self.transitions[-1].to_state

    def append(self, transition: LifecycleTransition) -> LifecycleHistory:
        """校验后返回**新的**历史对象（原对象不可变）。"""
        if transition.subject.target_identity() != self.subject.target_identity():
            raise LifecycleViolation("转移记录的 subject 与历史不一致")
        if transition.from_state is not self.current_state:
            raise LifecycleViolation(
                f"转移起点 {transition.from_state} 与当前状态 {self.current_state} 不一致"
            )
        validate_transition(
            transition.from_state, transition.to_state, approved_by=transition.approved_by
        )
        return self.model_copy(update={"transitions": (*self.transitions, transition)})

    @model_validator(mode="after")
    def _chain_is_valid(self) -> LifecycleHistory:
        """直接构造的校验强度必须与 `append` 一致（ADR-0011 D-17.2）。

        除状态链外，还要求：每条转移都属于本历史的 `subject`；`occurred_at` 非递减
        （允许相等——同一时刻的批量登记是可能的，本契约不为此发明更细的排序规则）。
        """
        state = LifecycleState.IDEA
        previous_at: datetime | None = None
        for transition in self.transitions:
            if transition.subject.target_identity() != self.subject.target_identity():
                raise LifecycleViolation("转移记录的 subject 与历史不一致")
            if transition.from_state is not state:
                raise ValueError(f"历史链断裂：{state} 之后出现 {transition.from_state}")
            if previous_at is not None and transition.occurred_at < previous_at:
                raise LifecycleViolation(
                    f"生命周期历史的时间不得回退：{transition.occurred_at} 早于 {previous_at}"
                )
            validate_transition(
                transition.from_state, transition.to_state, approved_by=transition.approved_by
            )
            state = transition.to_state
            previous_at = transition.occurred_at
        return self
