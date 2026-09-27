"""生命周期证据的最小结构（ADR-0019，D-27）。

ADR-0006 §3 第 4 条：每条转移记录包含证据引用。因此 `LifecycleTransition.evidence`
必须显式提供、至少一项，且每项去除首尾空白后非空——适用于**全部**合法转移，
不区分晋升、拒绝、失败、劣化或退役。

刻意**不**做的事（D-27.2、D-27.3）：不要求 `approved_by != triggered_by`（自报字符串无法证明
职责分离，且 Q-5 未决）；不校验证据是否存在、是否支持结论（属 Registry / Control Plane）。
证据字符串的格式也不冻结——本文件里的证据串只是测试数据。
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.domain.base import Kind, Ref
from core.domain.research import FailureRecord, RetirementRecord
from core.errors import LifecycleViolation, ReasonCode
from core.lifecycle.strategy import (
    ALLOWED_TRANSITIONS,
    HUMAN_APPROVAL_TRANSITIONS,
    LifecycleHistory,
    LifecycleState,
    LifecycleTransition,
    validate_transition,
)

REPO = Path(__file__).resolve().parents[1]
SUBJECT = Ref(kind=Kind.STRATEGY, name="s_evidence", version="1.0.0")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
EVIDENCE = ("report:test-evidence",)

EDGES = tuple(sorted(ALLOWED_TRANSITIONS))
EDGE_IDS = tuple(f"{a.value}->{b.value}" for a, b in EDGES)


def _payload(
    from_state: LifecycleState = LifecycleState.IDEA,
    to_state: LifecycleState = LifecycleState.CANDIDATE,
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "subject": SUBJECT,
        "from_state": from_state,
        "to_state": to_state,
        "reason": "test",
        "triggered_by": "test-runner",
        "approved_by": "raphael" if (from_state, to_state) in HUMAN_APPROVAL_TRANSITIONS else None,
        "occurred_at": T0,
        "evidence": EVIDENCE,
    }
    payload.update(overrides)
    return payload


def _json_payload(**overrides: object) -> str:
    payload = json.loads(LifecycleTransition(**_payload()).model_dump_json())  # type: ignore[arg-type]
    payload.update(overrides)
    return json.dumps(payload)


def _path_to(state: LifecycleState) -> list[tuple[LifecycleState, LifecycleState]]:
    """从 IDEA 出发到 `state` 的一条合法边序列（BFS，只走 ALLOWED_TRANSITIONS）。"""
    queue: deque[tuple[LifecycleState, list[tuple[LifecycleState, LifecycleState]]]] = deque(
        [(LifecycleState.IDEA, [])]
    )
    seen = {LifecycleState.IDEA}
    while queue:
        current, path = queue.popleft()
        if current is state:
            return path
        for a, b in EDGES:
            if a is current and b not in seen:
                seen.add(b)
                queue.append((b, [*path, (a, b)]))
    raise AssertionError(f"{state} 从 IDEA 不可达")


# ======================================================================================
# 矩阵 1：空证据在 Python 与 JSON 两条入口都被拒绝；缺失证据被拒绝
# ======================================================================================


def test_empty_evidence_is_rejected_in_python() -> None:
    with pytest.raises(ValidationError):
        LifecycleTransition(**_payload(evidence=()))  # type: ignore[arg-type]


def test_empty_evidence_is_rejected_in_json() -> None:
    with pytest.raises(ValidationError):
        LifecycleTransition.model_validate_json(_json_payload(evidence=[]))


def test_missing_evidence_is_rejected_in_python() -> None:
    payload = _payload()
    del payload["evidence"]
    with pytest.raises(ValidationError):
        LifecycleTransition(**payload)  # type: ignore[arg-type]


def test_missing_evidence_is_rejected_in_json() -> None:
    payload = json.loads(_json_payload())
    del payload["evidence"]
    with pytest.raises(ValidationError):
        LifecycleTransition.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("edge", EDGES, ids=EDGE_IDS)
def test_every_allowed_edge_rejects_empty_evidence(
    edge: tuple[LifecycleState, LifecycleState],
) -> None:
    """矩阵 1 / 3：约束对每一条合法边都成立，不只晋升边。"""
    with pytest.raises(ValidationError):
        LifecycleTransition(**_payload(*edge, evidence=()))  # type: ignore[arg-type]


# ======================================================================================
# 矩阵 2：空串或纯空白证据项被拒绝（在既有 str_strip_whitespace 之后判断）
# ======================================================================================


@pytest.mark.parametrize(
    "evidence",
    (("",), ("   ",), ("\t\n",), ("report:ok", ""), ("report:ok", "  ")),
    ids=("empty", "spaces", "tab-newline", "valid-then-empty", "valid-then-blank"),
)
def test_blank_evidence_items_are_rejected(evidence: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        LifecycleTransition(**_payload(evidence=evidence))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LifecycleTransition.model_validate_json(_json_payload(evidence=list(evidence)))


def test_evidence_items_keep_the_existing_whitespace_stripping() -> None:
    transition = LifecycleTransition(**_payload(evidence=("  report:r-1  ",)))  # type: ignore[arg-type]
    assert transition.evidence == ("report:r-1",)


# ======================================================================================
# 矩阵 3：每一条合法边在非空证据下被接受（含在历史中实际走到该边）
# ======================================================================================


@pytest.mark.parametrize("edge", EDGES, ids=EDGE_IDS)
def test_every_allowed_edge_accepts_non_empty_evidence(
    edge: tuple[LifecycleState, LifecycleState],
) -> None:
    from_state, to_state = edge
    steps = [*_path_to(from_state), edge]
    transitions = tuple(
        LifecycleTransition(**_payload(a, b, occurred_at=T0 + timedelta(hours=i)))  # type: ignore[arg-type]
        for i, (a, b) in enumerate(steps)
    )
    history = LifecycleHistory(subject=SUBJECT, transitions=transitions)
    assert history.current_state is to_state
    validate_transition(from_state, to_state, approved_by=transitions[-1].approved_by)


# ======================================================================================
# 矩阵 4：LifecycleHistory 直接构造与 append 都拿不到空证据的转移
# ======================================================================================


def test_history_construction_rejects_a_transition_with_empty_evidence() -> None:
    payload = {
        "subject": json.loads(SUBJECT.model_dump_json()),
        "transitions": [json.loads(_json_payload(evidence=[]))],
    }
    with pytest.raises(ValidationError):
        LifecycleHistory.model_validate(payload)
    with pytest.raises(ValidationError):
        LifecycleHistory.model_validate_json(json.dumps(payload))


def test_history_append_cannot_receive_a_transition_with_empty_evidence() -> None:
    history = LifecycleHistory(subject=SUBJECT)
    with pytest.raises(ValidationError):
        history.append(LifecycleTransition(**_payload(evidence=())))  # type: ignore[arg-type]


# ======================================================================================
# 矩阵 5 ~ 8：不做自报职责分离；既有批准规则不变；证据存在性不在契约层
# ======================================================================================


def test_same_person_triggering_and_approving_is_still_accepted() -> None:
    """矩阵 5（D-27.2）：两个载荷字符串相等不是违规，也不能被契约层判定。"""
    transition = LifecycleTransition(
        **_payload(
            LifecycleState.OOS,
            LifecycleState.PAPER,
            triggered_by="raphael",
            approved_by="raphael",
        )  # type: ignore[arg-type]
    )
    history = LifecycleHistory(
        subject=SUBJECT,
        transitions=(
            *(
                LifecycleTransition(**_payload(a, b))  # type: ignore[arg-type]
                for a, b in _path_to(LifecycleState.OOS)
            ),
            transition,
        ),
    )
    assert history.current_state is LifecycleState.PAPER


def test_human_approval_is_still_required_with_evidence() -> None:
    """矩阵 6：证据不能替代人工批准。"""
    with pytest.raises(LifecycleViolation):
        validate_transition(LifecycleState.OOS, LifecycleState.PAPER, approved_by=None)
    history = LifecycleHistory(
        subject=SUBJECT,
        transitions=tuple(
            LifecycleTransition(**_payload(a, b))  # type: ignore[arg-type]
            for a, b in _path_to(LifecycleState.OOS)
        ),
    )
    with pytest.raises(LifecycleViolation):
        history.append(
            LifecycleTransition(
                **_payload(LifecycleState.OOS, LifecycleState.PAPER, approved_by=None)  # type: ignore[arg-type]
            )
        )


def test_evidence_pointing_nowhere_is_accepted_at_the_contract_layer() -> None:
    """矩阵 8（D-27.3）：存在性与内容是否支持结论属 Registry，契约层不核验。"""
    transition = LifecycleTransition(**_payload(evidence=("report:does-not-exist",)))  # type: ignore[arg-type]
    assert transition.evidence == ("report:does-not-exist",)


def test_other_models_evidence_is_not_tightened() -> None:
    """明确不做：FailureRecord / RetirementRecord 的 evidence 保持原约束。"""
    assert (
        FailureRecord(
            subject_ref=SUBJECT, terminal_state="REJECTED", reason_code=ReasonCode.HUMAN_VETO
        ).evidence
        == ()
    )
    assert RetirementRecord(subject_ref=SUBJECT, retirement_reason="test").evidence == ()


# ======================================================================================
# 矩阵 9：导出 Schema 表达 required + minItems: 1 + 元素 minLength: 1
# ======================================================================================


def _assert_evidence_schema(schema: dict[str, object]) -> None:
    assert "evidence" in schema.get("required", []), "evidence 必须是 required"  # type: ignore[operator]
    evidence = schema["properties"]["evidence"]  # type: ignore[index]
    assert evidence.get("minItems") == 1, evidence
    assert evidence.get("items", {}).get("minLength") == 1, evidence
    assert "default" not in evidence, evidence


def test_transition_schema_expresses_the_evidence_minimum() -> None:
    _assert_evidence_schema(LifecycleTransition.model_json_schema(mode="serialization"))
    committed = json.loads(
        (REPO / "schemas" / "LifecycleTransition.schema.json").read_text(encoding="utf-8")
    )
    _assert_evidence_schema(committed)


def test_history_schema_embeds_the_same_evidence_minimum() -> None:
    committed = json.loads(
        (REPO / "schemas" / "LifecycleHistory.schema.json").read_text(encoding="utf-8")
    )
    _assert_evidence_schema(committed["$defs"]["LifecycleTransition"])


def test_validation_to_failed_needs_evidence_and_the_contract_does_not_read_it() -> None:
    """ADR-0053 §3：新边与所有边一样要求至少一项非空证据（ADR-0019）；证据的**内容**
    （报告 / Run、FailureRecord 哈希、轮次）由自动触发者执行，契约层不校验（D-27.3）。"""
    edge = (LifecycleState.VALIDATION, LifecycleState.FAILED)
    assert edge in ALLOWED_TRANSITIONS and edge not in HUMAN_APPROVAL_TRANSITIONS
    for evidence in ((), ("",), ("  ",)):
        with pytest.raises(ValidationError):
            LifecycleTransition(**_payload(*edge, evidence=evidence))  # type: ignore[arg-type]
    accepted = LifecycleTransition(
        **_payload(  # type: ignore[arg-type]
            *edge,
            evidence=(
                "validation_report:r-1",
                "failure_record:" + "0" * 64,
                "loop_round:test:0",
            ),
        )
    )
    assert accepted.approved_by is None
