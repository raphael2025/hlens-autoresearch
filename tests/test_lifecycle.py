"""生命周期状态机测试（ADR-0006、ADR-0011）。

验收标准："状态机只允许定义的转移（测试覆盖）"。
本文件中的期望转移表是**独立于实现重新写出**的，以便实现被悄悄改动时测试会失败。

ADR-0011（D-17）补充的不变量也在这里验收：不可逆退役需要人工批准、历史主体一致且时间
不回退、LIVE 证据必须同主体且在授权窗口内、Run 与退役记录的局部时间顺序。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from core.domain.base import Kind, Ref
from core.domain.research import ExperimentRun, RetirementRecord, RunState
from core.errors import LifecycleViolation
from core.lifecycle.experiment_run import validate_run_transition
from core.lifecycle.strategy import (
    ALLOWED_TRANSITIONS,
    FAILURE_REGISTRY_STATES,
    HUMAN_APPROVAL_TRANSITIONS,
    RETIREMENT_STATES,
    TERMINAL_STATES,
    AuthorizationRecord,
    ExecutionMode,
    ExecutionModeChange,
    LifecycleHistory,
    LifecycleTransition,
    RiskGateRecord,
    validate_transition,
)
from core.lifecycle.strategy import (
    LifecycleState as S,
)
from tests import factories

SUBJECT = Ref(kind=Kind.STRATEGY, name="s_example", version="1.0.0")
OTHER_SUBJECT = Ref(kind=Kind.STRATEGY, name="s_other", version="1.0.0")

#: 生命周期事件的固定测试时间（授权窗口 = [AUTHORIZED_AT, VALID_UNTIL]）。
AUTHORIZED_AT = datetime(2026, 1, 1, tzinfo=UTC)
CHANGED_AT = datetime(2026, 1, 15, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 2, 1, tzinfo=UTC)

#: ADR-0006 状态机的独立副本（C-1 选项 B：PAPER 在 PRODUCTION_CANDIDATE 之前）。
EXPECTED_TRANSITIONS = {
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

#: 需要人工批准的转移（ADR-0006 §3 第 6 条 + ADR-0011 D-17.1 补上的 REVALIDATION → RETIRED）。
EXPECTED_HUMAN_APPROVAL = {
    (S.OOS, S.PAPER),
    (S.PRODUCTION_CANDIDATE, S.ACTIVE),
    (S.REVALIDATION, S.ACTIVE),
    (S.REVALIDATION, S.RETIRED),
    (S.ACTIVE, S.RETIRED),
    (S.PAPER, S.RETIRED),
}


def _transition(
    from_state: S,
    to_state: S,
    approved_by: str | None = "raphael",
    *,
    subject: Ref = SUBJECT,
    occurred_at: datetime = AUTHORIZED_AT,
    evidence: tuple[str, ...] = ("test-evidence:lifecycle",),
) -> LifecycleTransition:
    return LifecycleTransition(
        subject=subject,
        from_state=from_state,
        to_state=to_state,
        reason="test",
        evidence=evidence,
        triggered_by="test",
        approved_by=approved_by,
        occurred_at=occurred_at,
    )


def test_transition_table_matches_adr_0006() -> None:
    assert set(ALLOWED_TRANSITIONS) == EXPECTED_TRANSITIONS


def test_human_approval_set_is_a_subset_of_the_transition_table() -> None:
    """ADR-0011 只改批准集合，不得增删任何边。"""
    assert set(HUMAN_APPROVAL_TRANSITIONS) == EXPECTED_HUMAN_APPROVAL
    assert set(HUMAN_APPROVAL_TRANSITIONS) <= EXPECTED_TRANSITIONS


def test_no_transition_outside_the_table_is_accepted() -> None:
    for from_state, to_state in itertools.product(S, S):
        if (from_state, to_state) in EXPECTED_TRANSITIONS:
            continue
        with pytest.raises(LifecycleViolation):
            validate_transition(from_state, to_state, approved_by="raphael")


def test_degraded_cannot_go_directly_to_active() -> None:
    """ADR-0006 §3 第 1 条：必须经 REVALIDATION。"""
    with pytest.raises(LifecycleViolation):
        validate_transition(S.DEGRADED, S.ACTIVE, approved_by="raphael")
    validate_transition(S.DEGRADED, S.REVALIDATION)
    validate_transition(S.REVALIDATION, S.ACTIVE, approved_by="raphael")


def test_paper_precedes_production_candidate() -> None:
    """C-1 选项 B。"""
    validate_transition(S.OOS, S.PAPER, approved_by="raphael")
    validate_transition(S.PAPER, S.PRODUCTION_CANDIDATE)
    with pytest.raises(LifecycleViolation):
        validate_transition(S.PRODUCTION_CANDIDATE, S.PAPER, approved_by="raphael")


def test_human_approval_required_for_gated_transitions() -> None:
    for pair in sorted(EXPECTED_HUMAN_APPROVAL):
        with pytest.raises(LifecycleViolation):
            validate_transition(*pair, approved_by=None)
        validate_transition(*pair, approved_by="raphael")


def test_failed_revalidation_cannot_retire_without_a_human() -> None:
    """ADR-0011 D-17.1：失败的 revalidation report 是证据，不是执行者。"""
    with pytest.raises(LifecycleViolation):
        validate_transition(S.REVALIDATION, S.RETIRED, approved_by=None)
    with pytest.raises(LifecycleViolation):
        validate_transition(S.REVALIDATION, S.RETIRED, approved_by="")
    validate_transition(S.REVALIDATION, S.RETIRED, approved_by="raphael")


def test_history_rejects_unapproved_retirement_from_revalidation() -> None:
    """同一条边在 append 与直接构造两条路径上都必须要求批准。"""
    history = LifecycleHistory(
        subject=SUBJECT,
        transitions=(
            _transition(S.IDEA, S.CANDIDATE),
            _transition(S.CANDIDATE, S.VALIDATION),
            _transition(S.VALIDATION, S.OOS),
            _transition(S.OOS, S.PAPER),
            _transition(S.PAPER, S.PRODUCTION_CANDIDATE),
            _transition(S.PRODUCTION_CANDIDATE, S.ACTIVE),
            _transition(S.ACTIVE, S.DEGRADED),
            _transition(S.DEGRADED, S.REVALIDATION),
        ),
    )
    with pytest.raises(LifecycleViolation):
        history.append(_transition(S.REVALIDATION, S.RETIRED, approved_by=None))
    with pytest.raises(LifecycleViolation):
        LifecycleHistory(
            subject=SUBJECT,
            transitions=(*history.transitions, _transition(S.REVALIDATION, S.RETIRED, None)),
        )
    retired = history.append(_transition(S.REVALIDATION, S.RETIRED, approved_by="raphael"))
    assert retired.current_state is S.RETIRED


def test_terminal_states_are_terminal() -> None:
    assert TERMINAL_STATES == {S.RETIRED, S.REJECTED, S.FAILED}
    for terminal, target in itertools.product(TERMINAL_STATES, S):
        with pytest.raises(LifecycleViolation):
            validate_transition(terminal, target, approved_by="raphael")


def test_retired_is_not_a_failure_state() -> None:
    """RETIRED ≠ FAILED（ADR-0006 §3 第 2 条）。"""
    assert FAILURE_REGISTRY_STATES == {S.REJECTED, S.FAILED}
    assert RETIREMENT_STATES == {S.RETIRED}
    assert not FAILURE_REGISTRY_STATES & RETIREMENT_STATES


def test_history_is_append_only_and_validated() -> None:
    history = LifecycleHistory(subject=SUBJECT)
    assert history.current_state is S.IDEA
    grown = history.append(_transition(S.IDEA, S.CANDIDATE))
    assert history.transitions == ()  # 原对象不变
    assert grown.current_state is S.CANDIDATE
    with pytest.raises(LifecycleViolation):
        grown.append(_transition(S.IDEA, S.CANDIDATE))  # 起点与当前状态不符


def test_history_rejects_broken_chain_at_construction() -> None:
    with pytest.raises(ValidationError):
        LifecycleHistory(
            subject=SUBJECT,
            transitions=(_transition(S.IDEA, S.CANDIDATE), _transition(S.VALIDATION, S.OOS)),
        )


def test_history_rejects_a_foreign_subject_at_construction() -> None:
    """ADR-0011 D-17.2：直接构造与 append 同样强度地校验主体归属。"""
    foreign = _transition(S.IDEA, S.CANDIDATE, subject=OTHER_SUBJECT)
    with pytest.raises(LifecycleViolation):
        LifecycleHistory(subject=SUBJECT, transitions=(foreign,))
    with pytest.raises(LifecycleViolation):
        LifecycleHistory(subject=SUBJECT).append(foreign)
    with pytest.raises(LifecycleViolation):
        LifecycleHistory(
            subject=SUBJECT,
            transitions=(
                _transition(S.IDEA, S.CANDIDATE),
                _transition(S.CANDIDATE, S.VALIDATION, subject=OTHER_SUBJECT),
            ),
        )


def test_history_rejects_time_going_backwards_but_allows_equal_times() -> None:
    """ADR-0011 D-17.2：`occurred_at` 非递减；同一时刻的批量登记允许存在。"""
    earlier = AUTHORIZED_AT
    later = AUTHORIZED_AT + timedelta(hours=1)
    with pytest.raises(LifecycleViolation):
        LifecycleHistory(
            subject=SUBJECT,
            transitions=(
                _transition(S.IDEA, S.CANDIDATE, occurred_at=later),
                _transition(S.CANDIDATE, S.VALIDATION, occurred_at=earlier),
            ),
        )
    equal = LifecycleHistory(
        subject=SUBJECT,
        transitions=(
            _transition(S.IDEA, S.CANDIDATE, occurred_at=earlier),
            _transition(S.CANDIDATE, S.VALIDATION, occurred_at=earlier),
        ),
    )
    assert equal.current_state is S.VALIDATION
    forward = equal.append(_transition(S.VALIDATION, S.OOS, occurred_at=later))
    assert forward.current_state is S.OOS


def test_full_happy_path_to_active() -> None:
    history = LifecycleHistory(subject=SUBJECT)
    for index, (from_state, to_state) in enumerate(
        [
            (S.IDEA, S.CANDIDATE),
            (S.CANDIDATE, S.VALIDATION),
            (S.VALIDATION, S.OOS),
            (S.OOS, S.PAPER),
            (S.PAPER, S.PRODUCTION_CANDIDATE),
            (S.PRODUCTION_CANDIDATE, S.ACTIVE),
        ]
    ):
        history = history.append(
            _transition(from_state, to_state, occurred_at=AUTHORIZED_AT + timedelta(days=index))
        )
    assert history.current_state is S.ACTIVE
    assert len(history.transitions) == 6


def _risk_gate(**overrides: object) -> RiskGateRecord:
    payload: dict[str, object] = {
        "subject": SUBJECT,
        "gate_id": "rg-1",
        "passed": True,
        "checked_at": CHANGED_AT,
    }
    payload.update(overrides)
    return RiskGateRecord(**payload)  # type: ignore[arg-type]


def _authorization(**overrides: object) -> AuthorizationRecord:
    payload: dict[str, object] = {
        "subject": SUBJECT,
        "authorized_by": "raphael",
        "risk_budget": "test",
        "authorized_at": AUTHORIZED_AT,
        "valid_until": VALID_UNTIL,
    }
    payload.update(overrides)
    return AuthorizationRecord(**payload)  # type: ignore[arg-type]


def _mode_change(**overrides: object) -> ExecutionModeChange:
    payload: dict[str, object] = {
        "subject": SUBJECT,
        "state": S.ACTIVE,
        "from_mode": ExecutionMode.SIMULATED,
        "to_mode": ExecutionMode.LIVE,
        "risk_gate": _risk_gate(),
        "authorization": _authorization(),
        "occurred_at": CHANGED_AT,
    }
    payload.update(overrides)
    return ExecutionModeChange(**payload)  # type: ignore[arg-type]


def test_live_requires_risk_gate_and_authorization() -> None:
    with pytest.raises(ValidationError):
        _mode_change(risk_gate=None)
    with pytest.raises(ValidationError):
        _mode_change(risk_gate=_risk_gate(passed=False))
    with pytest.raises(ValidationError):
        _mode_change(authorization=None)
    assert _mode_change().to_mode is ExecutionMode.LIVE


def test_self_reported_live_execution_flag_no_longer_exists() -> None:
    """ADR-0011 D-17.4：载荷不能自证运行环境；旧字段被 `extra="forbid"` 拒绝。

    **诚实边界**：契约层因此不再拒绝 `to_mode = LIVE`。Phase 13 红线的执行点在
    Control Plane 与人类授权，不在 DTO；被删除的自报字段从来没有提供过真实防护。
    """
    assert "live_execution_enabled" not in ExecutionModeChange.model_fields
    with pytest.raises(ValidationError):
        _mode_change(live_execution_enabled=True)
    with pytest.raises(ValidationError):
        _mode_change(live_execution_enabled=False)


def test_live_evidence_must_belong_to_the_same_subject() -> None:
    """ADR-0011 D-17.3：别的策略的 Risk Gate / 授权不得挂到本次变更上。"""
    with pytest.raises(ValidationError):
        _mode_change(risk_gate=_risk_gate(subject=OTHER_SUBJECT))
    with pytest.raises(ValidationError):
        _mode_change(authorization=_authorization(subject=OTHER_SUBJECT))
    with pytest.raises(ValidationError):
        _mode_change(subject=OTHER_SUBJECT)


def test_authorization_window_must_cover_the_change() -> None:
    """窗口两端都算覆盖；窗口之外拒绝（Risk Gate 时间同步前移，以隔离失败原因）。"""
    at_start = _mode_change(
        occurred_at=AUTHORIZED_AT, risk_gate=_risk_gate(checked_at=AUTHORIZED_AT)
    )
    assert at_start.occurred_at == AUTHORIZED_AT
    at_end = _mode_change(occurred_at=VALID_UNTIL, risk_gate=_risk_gate(checked_at=VALID_UNTIL))
    assert at_end.occurred_at == VALID_UNTIL

    too_early = AUTHORIZED_AT - timedelta(seconds=1)
    with pytest.raises(ValidationError, match="授权必须覆盖变更时刻"):
        _mode_change(occurred_at=too_early, risk_gate=_risk_gate(checked_at=too_early))
    with pytest.raises(ValidationError, match="授权必须覆盖变更时刻"):
        _mode_change(
            occurred_at=VALID_UNTIL + timedelta(seconds=1),
            risk_gate=_risk_gate(checked_at=VALID_UNTIL),
        )


def test_authorization_window_must_be_positive() -> None:
    """ADR-0011 D-17.3：零长度与倒挂的授权窗口都无效。"""
    with pytest.raises(ValidationError):
        _authorization(valid_until=AUTHORIZED_AT)
    with pytest.raises(ValidationError):
        _authorization(valid_until=AUTHORIZED_AT - timedelta(seconds=1))
    assert _authorization().valid_until > _authorization().authorized_at


def test_risk_gate_must_not_be_later_than_the_change() -> None:
    assert (
        _mode_change(risk_gate=_risk_gate(checked_at=AUTHORIZED_AT)).to_mode is ExecutionMode.LIVE
    )
    with pytest.raises(ValidationError):
        _mode_change(risk_gate=_risk_gate(checked_at=CHANGED_AT + timedelta(seconds=1)))


def test_no_op_mode_change_is_rejected() -> None:
    """ADR-0011 D-17.3：变更事件必须真的改变模式。"""
    with pytest.raises(ValidationError):
        _mode_change(from_mode=ExecutionMode.LIVE, to_mode=ExecutionMode.LIVE)
    with pytest.raises(ValidationError):
        _mode_change(
            from_mode=ExecutionMode.SIMULATED,
            to_mode=ExecutionMode.SIMULATED,
            risk_gate=None,
            authorization=None,
        )


def test_execution_mode_only_applies_to_active() -> None:
    with pytest.raises(ValidationError):
        _mode_change(state=S.PAPER)


def test_simulated_mode_needs_no_gate() -> None:
    change = _mode_change(
        from_mode=ExecutionMode.LIVE,
        to_mode=ExecutionMode.SIMULATED,
        risk_gate=None,
        authorization=None,
    )
    assert change.to_mode is ExecutionMode.SIMULATED


def test_experiment_run_requires_started_before_finished() -> None:
    """ADR-0011 D-17.5：记录内部的时间顺序，不是跨对象时间线。"""
    with pytest.raises(ValidationError):
        factories.experiment_run(started_at=CHANGED_AT, finished_at=AUTHORIZED_AT)
    same = factories.experiment_run(started_at=CHANGED_AT, finished_at=CHANGED_AT)
    assert same.finished_at == same.started_at
    only_started = factories.experiment_run(started_at=CHANGED_AT)
    assert only_started.finished_at is None
    only_finished = factories.experiment_run(finished_at=CHANGED_AT)
    assert only_finished.started_at is None
    assert ExperimentRun.model_fields["finished_at"].default is None


def _retirement(**overrides: object) -> RetirementRecord:
    payload: dict[str, object] = {
        "subject_ref": SUBJECT,
        "retirement_reason": "superseded",
    }
    payload.update(overrides)
    return RetirementRecord(**payload)  # type: ignore[arg-type]


def test_retirement_record_active_period_is_ordered() -> None:
    with pytest.raises(ValidationError):
        _retirement(active_from=CHANGED_AT, active_to=AUTHORIZED_AT)
    assert _retirement(active_from=AUTHORIZED_AT, active_to=CHANGED_AT).active_to == CHANGED_AT
    assert _retirement(active_from=CHANGED_AT, active_to=CHANGED_AT).active_from == CHANGED_AT
    assert _retirement(active_from=CHANGED_AT).active_to is None
    assert _retirement(active_to=CHANGED_AT).active_from is None


def test_retirement_execution_mode_uses_the_lifecycle_enum() -> None:
    """ADR-0011 D-17.5：同一概念只有一个枚举，不再是自由字符串。"""
    assert _retirement(execution_mode="SIMULATED").execution_mode is ExecutionMode.SIMULATED
    assert _retirement(execution_mode=ExecutionMode.LIVE).execution_mode is ExecutionMode.LIVE
    assert _retirement().execution_mode is None
    for bad in ("simulated", "PAPER", "live", "whatever"):
        with pytest.raises(ValidationError):
            _retirement(execution_mode=bad)


def test_experiment_run_state_machine_is_separate() -> None:
    validate_run_transition(RunState.REGISTERED, RunState.QUEUED)
    validate_run_transition(RunState.RUNNING, RunState.ERRORED)
    with pytest.raises(LifecycleViolation):
        validate_run_transition(RunState.REGISTERED, RunState.COMPLETED)
    with pytest.raises(LifecycleViolation):
        validate_run_transition(RunState.VALIDATED, RunState.RUNNING)


def test_paper_period_is_not_a_threshold_here() -> None:
    """生命周期模块不得内置时长阈值；时长属于 Validation Profile（ADR-0007）。"""
    import core.lifecycle.strategy as module

    source = module.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()
    assert "timedelta(" not in text, "生命周期状态机中不应出现时长常量"
    assert timedelta(days=1) > timedelta(0)  # 仅确认 import 可用
