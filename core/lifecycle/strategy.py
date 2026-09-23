"""Strategy Lifecycle v2 状态机（ADR-0006，Accepted 2026-09-23）。

顺序：IDEA → CANDIDATE → VALIDATION → OOS → PAPER → PRODUCTION_CANDIDATE → ACTIVE
      → DEGRADED → REVALIDATION → ACTIVE / RETIRED

关键规则：
* DEGRADED 永远不能直接转为 ACTIVE，必须经 REVALIDATION。
* RETIRED ≠ FAILED：RETIRED 进退役记录，REJECTED / FAILED 进 Failure Registry。
* LIVE 不是状态，而是 ACTIVE 的 `execution_mode`；Phase 13 之前只允许 SIMULATED。
* 每次转移只追加、可审计。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from core.domain.base import Contract, Ref, UtcDatetime
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


class ExecutionMode(StrEnum):
    """ACTIVE 的属性，不是生命周期状态（ADR-0006 §2）。"""

    SIMULATED = "SIMULATED"
    LIVE = "LIVE"


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
HUMAN_APPROVAL_TRANSITIONS: frozenset[tuple[LifecycleState, LifecycleState]] = frozenset(
    {
        (S.OOS, S.PAPER),
        (S.PRODUCTION_CANDIDATE, S.ACTIVE),
        (S.REVALIDATION, S.ACTIVE),
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
    """实盘前的独立 Risk Gate 检查（与 RiskProvider 分离）。"""

    gate_id: str = Field(min_length=1)
    passed: bool
    limits: dict[str, str] = Field(default_factory=dict)
    checked_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthorizationRecord(Contract):
    """实盘授权记录（ADR-0006 §2）。"""

    authorized_by: str = Field(min_length=1)
    risk_budget: str = Field(min_length=1)
    valid_until: UtcDatetime
    authorized_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionModeChange(Contract):
    """`execution_mode` 变更的独立审计事件。

    切到 LIVE 需要：对象处于 ACTIVE、通过 Risk Gate、有授权记录，且运行环境已开放实盘
    （Phase 13 之前 `live_execution_enabled` 为 False）。
    """

    subject: Ref
    state: LifecycleState
    from_mode: ExecutionMode
    to_mode: ExecutionMode
    risk_gate: RiskGateRecord | None = None
    authorization: AuthorizationRecord | None = None
    live_execution_enabled: bool = False
    occurred_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _live_requires_gate(self) -> ExecutionModeChange:
        if self.state is not LifecycleState.ACTIVE:
            raise ValueError("execution_mode 只适用于 ACTIVE 状态（ADR-0006 §2）")
        if self.to_mode is ExecutionMode.LIVE:
            if not self.live_execution_enabled:
                raise ValueError("Phase 13 之前禁止 LIVE（ADR-0006 §3 第 5 条）")
            if self.risk_gate is None or not self.risk_gate.passed:
                raise ValueError("切换到 LIVE 需要通过独立 Risk Gate")
            if self.authorization is None:
                raise ValueError("切换到 LIVE 需要明确的授权记录")
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
        if transition.subject != self.subject:
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
        state = LifecycleState.IDEA
        for transition in self.transitions:
            if transition.from_state is not state:
                raise ValueError(f"历史链断裂：{state} 之后出现 {transition.from_state}")
            validate_transition(
                transition.from_state, transition.to_state, approved_by=transition.approved_by
            )
            state = transition.to_state
        return self
