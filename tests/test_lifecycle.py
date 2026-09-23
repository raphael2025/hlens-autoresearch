"""生命周期状态机测试（ADR-0006）。

验收标准："状态机只允许定义的转移（测试覆盖）"。
本文件中的期望转移表是**独立于实现重新写出**的，以便实现被悄悄改动时测试会失败。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from core.domain.base import Kind, Ref
from core.domain.research import RunState
from core.errors import LifecycleViolation
from core.lifecycle.experiment_run import validate_run_transition
from core.lifecycle.strategy import (
    ALLOWED_TRANSITIONS,
    FAILURE_REGISTRY_STATES,
    RETIREMENT_STATES,
    TERMINAL_STATES,
    AuthorizationRecord,
    ExecutionMode,
    ExecutionModeChange,
    LifecycleHistory,
    LifecycleState as S,
    LifecycleTransition,
    RiskGateRecord,
    validate_transition,
)

SUBJECT = Ref(kind=Kind.STRATEGY, name="s_example", version="1.0.0")

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


def _transition(
    from_state: S, to_state: S, approved_by: str | None = "raphael"
) -> LifecycleTransition:
    return LifecycleTransition(
        subject=SUBJECT,
        from_state=from_state,
        to_state=to_state,
        reason="test",
        triggered_by="test",
        approved_by=approved_by,
    )


def test_transition_table_matches_adr_0006() -> None:
    assert set(ALLOWED_TRANSITIONS) == EXPECTED_TRANSITIONS


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
    for pair in [(S.OOS, S.PAPER), (S.PRODUCTION_CANDIDATE, S.ACTIVE), (S.REVALIDATION, S.ACTIVE),
                 (S.ACTIVE, S.RETIRED), (S.PAPER, S.RETIRED)]:
        with pytest.raises(LifecycleViolation):
            validate_transition(*pair, approved_by=None)
        validate_transition(*pair, approved_by="raphael")


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


def test_full_happy_path_to_active() -> None:
    history = LifecycleHistory(subject=SUBJECT)
    for from_state, to_state in [
        (S.IDEA, S.CANDIDATE),
        (S.CANDIDATE, S.VALIDATION),
        (S.VALIDATION, S.OOS),
        (S.OOS, S.PAPER),
        (S.PAPER, S.PRODUCTION_CANDIDATE),
        (S.PRODUCTION_CANDIDATE, S.ACTIVE),
    ]:
        history = history.append(_transition(from_state, to_state))
    assert history.current_state is S.ACTIVE
    assert len(history.transitions) == 6


def _mode_change(**overrides: object) -> ExecutionModeChange:
    payload: dict[str, object] = {
        "subject": SUBJECT,
        "state": S.ACTIVE,
        "from_mode": ExecutionMode.SIMULATED,
        "to_mode": ExecutionMode.LIVE,
        "risk_gate": RiskGateRecord(gate_id="rg-1", passed=True),
        "authorization": AuthorizationRecord(
            authorized_by="raphael",
            risk_budget="test",
            valid_until=datetime(2030, 1, 1, tzinfo=UTC),
        ),
        "live_execution_enabled": True,
    }
    payload.update(overrides)
    return ExecutionModeChange(**payload)  # type: ignore[arg-type]


def test_live_is_blocked_before_phase_13() -> None:
    with pytest.raises(ValidationError):
        _mode_change(live_execution_enabled=False)


def test_live_requires_risk_gate_and_authorization() -> None:
    with pytest.raises(ValidationError):
        _mode_change(risk_gate=None)
    with pytest.raises(ValidationError):
        _mode_change(risk_gate=RiskGateRecord(gate_id="rg-1", passed=False))
    with pytest.raises(ValidationError):
        _mode_change(authorization=None)
    assert _mode_change().to_mode is ExecutionMode.LIVE


def test_execution_mode_only_applies_to_active() -> None:
    with pytest.raises(ValidationError):
        _mode_change(state=S.PAPER)


def test_simulated_mode_needs_no_gate() -> None:
    change = _mode_change(
        from_mode=ExecutionMode.LIVE,
        to_mode=ExecutionMode.SIMULATED,
        risk_gate=None,
        authorization=None,
        live_execution_enabled=False,
    )
    assert change.to_mode is ExecutionMode.SIMULATED


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
