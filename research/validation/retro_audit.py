"""Retro-audit: re-run the current rules over earlier decisions and REPORT the differences.

Phase 8 (roadmap: "对已晋升对象的回溯审计"; prohibition: "新规则追溯使已拒绝对象通过"), ADR-0041.

For every ``AuditSubject`` (its current lifecycle state, the ``ValidationReport`` its lifecycle
decision was based on, and a callable that re-validates it under the **current** Constitution /
Profile / pipeline) the audit records a per-gate diff and a recommended action. It performs no
lifecycle transition and writes nothing: acting on a finding is the Control Plane's decision
(07-validation.md §3; DEGRADED → REVALIDATION needs its own evidence and approval).

The one-way rule is enforced, not merely documented:

- a subject that was **rejected** (lifecycle ``REJECTED`` / ``FAILED``, or a recorded verdict of
  ``FAIL``) keeps ``effective_verdict = FAIL`` whatever the current rules say. When they would now
  pass, the finding says so (``would_now_pass``) and its action is ``STAYS_REJECTED``: a retry is a
  new CANDIDATE version, never a flip of the old record (07-validation.md §3 rules);
- ``AuditFinding`` refuses to be constructed with any other effective verdict for such a subject
  (``RetroAuditViolation``), so no caller can build a flipping finding by hand;
- for a promoted subject the effective verdict is the stricter of recorded and current, so new
  rules can only flag it (``FLAG_FOR_REVALIDATION`` / ``REVIEW_INCONCLUSIVE``), never upgrade it.

Constitution changes act forward only (07-validation.md §1): the audit is a report, not a verdict.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from core.domain.base import Ref
from core.domain.research import ValidationReport, Verdict
from core.lifecycle.strategy import LifecycleState

__all__ = [
    "PROMOTED_STATES",
    "REJECTED_STATES",
    "AuditAction",
    "AuditFinding",
    "AuditSubject",
    "GateDiff",
    "RetroAuditReport",
    "RetroAuditViolation",
    "retro_audit",
]

#: States reached only through a passed validation (07-validation.md §3).
PROMOTED_STATES: Final = frozenset(
    {
        LifecycleState.OOS,
        LifecycleState.PAPER,
        LifecycleState.PRODUCTION_CANDIDATE,
        LifecycleState.ACTIVE,
        LifecycleState.DEGRADED,
        LifecycleState.REVALIDATION,
    }
)
REJECTED_STATES: Final = frozenset({LifecycleState.REJECTED, LifecycleState.FAILED})
_SEVERITY: Final = {Verdict.PASS: 0, Verdict.INCONCLUSIVE: 1, Verdict.FAIL: 2}


class RetroAuditViolation(ValueError):
    """An audit result would let new rules pass a previously rejected object."""


class AuditAction(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    FLAG_FOR_REVALIDATION = "FLAG_FOR_REVALIDATION"
    REVIEW_INCONCLUSIVE = "REVIEW_INCONCLUSIVE"
    STAYS_REJECTED = "STAYS_REJECTED"
    TERMINAL_NO_CHANGE = "TERMINAL_NO_CHANGE"


@dataclass(frozen=True)
class AuditSubject:
    subject: Ref
    lifecycle_state: LifecycleState
    recorded: ValidationReport
    rerun: Callable[[], ValidationReport]


@dataclass(frozen=True)
class GateDiff:
    gate_id: str
    recorded: Verdict | None
    current: Verdict | None
    recorded_value: float | None
    current_value: float | None
    recorded_threshold_source: str | None
    current_threshold_source: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_id": self.gate_id,
            "recorded": None if self.recorded is None else self.recorded.value,
            "current": None if self.current is None else self.current.value,
            "recorded_value": self.recorded_value,
            "current_value": self.current_value,
            "recorded_threshold_source": self.recorded_threshold_source,
            "current_threshold_source": self.current_threshold_source,
        }


def _was_rejected(state: LifecycleState, recorded: Verdict) -> bool:
    return state in REJECTED_STATES or recorded is Verdict.FAIL


@dataclass(frozen=True)
class AuditFinding:
    subject: Ref
    lifecycle_state: LifecycleState
    recorded_verdict: Verdict
    current_verdict: Verdict
    effective_verdict: Verdict
    action: AuditAction
    gate_diffs: tuple[GateDiff, ...]
    recorded_profile_hash: str
    current_profile_hash: str

    def __post_init__(self) -> None:
        if _was_rejected(self.lifecycle_state, self.recorded_verdict):
            if self.effective_verdict is not Verdict.FAIL:
                raise RetroAuditViolation(
                    f"{self.subject}: new rules must never make a rejected object pass"
                )
            if self.action is not AuditAction.STAYS_REJECTED:
                raise RetroAuditViolation(f"{self.subject}: a rejected object stays rejected")
        elif _SEVERITY[self.effective_verdict] < _SEVERITY[self.recorded_verdict]:
            raise RetroAuditViolation(f"{self.subject}: an audit can only tighten a verdict")

    @property
    def changed(self) -> bool:
        return bool(self.gate_diffs) or self.recorded_verdict is not self.current_verdict

    @property
    def would_now_pass(self) -> bool:
        """The current rules alone would pass it (reported; never acted on for a rejection)."""
        return self.current_verdict is Verdict.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "subject": str(self.subject),
            "lifecycle_state": self.lifecycle_state.value,
            "recorded_verdict": self.recorded_verdict.value,
            "current_verdict": self.current_verdict.value,
            "effective_verdict": self.effective_verdict.value,
            "would_now_pass": self.would_now_pass,
            "action": self.action.value,
            "changed": self.changed,
            "recorded_profile_hash": self.recorded_profile_hash,
            "current_profile_hash": self.current_profile_hash,
            "gate_diffs": [diff.to_dict() for diff in self.gate_diffs],
        }


def _diffs(recorded: ValidationReport, current: ValidationReport) -> tuple[GateDiff, ...]:
    before = {gate.gate_id: gate for gate in recorded.gates}
    after = {gate.gate_id: gate for gate in current.gates}
    diffs: list[GateDiff] = []
    for gate_id in sorted(before.keys() | after.keys()):
        old, new = before.get(gate_id), after.get(gate_id)
        same = (
            old is not None
            and new is not None
            and (old.verdict, old.value, old.threshold, old.threshold_source)
            == (new.verdict, new.value, new.threshold, new.threshold_source)
        )
        if same:
            continue
        diffs.append(
            GateDiff(
                gate_id=gate_id,
                recorded=None if old is None else old.verdict,
                current=None if new is None else new.verdict,
                recorded_value=None if old is None else old.value,
                current_value=None if new is None else new.value,
                recorded_threshold_source=None if old is None else old.threshold_source,
                current_threshold_source=None if new is None else new.threshold_source,
            )
        )
    return tuple(diffs)


def _audit_one(item: AuditSubject) -> AuditFinding:
    target = item.subject.target_identity()
    if item.recorded.subject.target_identity() != target:
        raise ValueError(f"the recorded report is not about {item.subject}")
    current = item.rerun()
    if current.subject.target_identity() != target:
        raise ValueError(f"the re-run report is not about {item.subject}")
    recorded_verdict = item.recorded.verdict
    if _was_rejected(item.lifecycle_state, recorded_verdict):
        effective, action = Verdict.FAIL, AuditAction.STAYS_REJECTED
    else:
        effective = max(recorded_verdict, current.verdict, key=_SEVERITY.__getitem__)
        if item.lifecycle_state is LifecycleState.RETIRED:
            action = AuditAction.TERMINAL_NO_CHANGE
        elif item.lifecycle_state not in PROMOTED_STATES:
            action = AuditAction.NO_CHANGE
        elif current.verdict is Verdict.FAIL:
            action = AuditAction.FLAG_FOR_REVALIDATION
        elif current.verdict is Verdict.INCONCLUSIVE:
            action = AuditAction.REVIEW_INCONCLUSIVE
        else:
            action = AuditAction.NO_CHANGE
    return AuditFinding(
        subject=item.subject,
        lifecycle_state=item.lifecycle_state,
        recorded_verdict=recorded_verdict,
        current_verdict=current.verdict,
        effective_verdict=effective,
        action=action,
        gate_diffs=_diffs(item.recorded, current),
        recorded_profile_hash=item.recorded.validation_profile_hash,
        current_profile_hash=current.validation_profile_hash,
    )


@dataclass(frozen=True)
class RetroAuditReport:
    audited_at: datetime
    rules: str
    findings: tuple[AuditFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "audited_at": self.audited_at.isoformat(),
            "rules": self.rules,
            "subjects": len(self.findings),
            "flagged_for_revalidation": sum(
                f.action is AuditAction.FLAG_FOR_REVALIDATION for f in self.findings
            ),
            "rejected_that_would_now_pass": sum(
                f.action is AuditAction.STAYS_REJECTED and f.would_now_pass for f in self.findings
            ),
            "findings": [finding.to_dict() for finding in self.findings],
            "note": "report only: no lifecycle transition was performed",
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, indent=2)


def retro_audit(
    subjects: Sequence[AuditSubject], *, audited_at: datetime, rules: str
) -> RetroAuditReport:
    """Audit every subject under the current rules (``rules`` names them, e.g. Profile refs)."""
    if audited_at.tzinfo is None:
        raise ValueError("audited_at must be timezone-aware UTC")
    return RetroAuditReport(
        audited_at=audited_at,
        rules=rules,
        findings=tuple(_audit_one(item) for item in subjects),
    )
