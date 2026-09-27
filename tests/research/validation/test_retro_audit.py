"""Phase 8: the retro-audit reports differences and never lets new rules pass a rejected object.

Roadmap Phase 8 prohibition: "新规则追溯使已拒绝对象通过". Uses the TEST ONLY profiles of
``robustness_fixtures``; a "lax" and a "strict" variant stand for an older and a newer rule set.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from core.domain.research import ValidationReport, Verdict
from core.lifecycle.strategy import LifecycleState
from research.validation import build_report, run_robustness
from research.validation.retro_audit import (
    AuditAction,
    AuditFinding,
    AuditSubject,
    RetroAuditViolation,
    retro_audit,
)
from tests import factories
from tests.research.validation import robustness_fixtures as rf
from tests.research.validation.fixtures import context

AUDITED_AT = datetime(2026, 9, 25, tzinfo=UTC)
LAX = rf.G4_TEST_ONLY_PROFILE.model_copy(
    update={
        "name": "test_only_g4_lax",
        "significance": rf.G4_TEST_ONLY_PROFILE.significance.model_copy(
            update={"overfitting_threshold": 1.0}
        ),
    }
)
STRICT = rf.G4_TEST_ONLY_PROFILE


def _noise_report(profile: object, *, overfitting_only: bool = False) -> ValidationReport:
    """G4 on the best-of-N noise family (only the overfitting check: a one-rule change)."""
    _, trials = rf.noise_family(5)
    chosen = rf.best(trials)
    inp = rf.robustness_input(
        trials, chosen.params, {"variant": tuple(range(len(trials)))}, profile=profile
    )
    result = run_robustness(inp)
    gates = result.checks[0].gates if overfitting_only else result.gates
    return build_report(context(profile=profile), gates)  # type: ignore[arg-type]


def _subject(state: LifecycleState, recorded: ValidationReport, current: object) -> AuditSubject:
    return AuditSubject(
        subject=recorded.subject,
        lifecycle_state=state,
        recorded=recorded,
        rerun=lambda: current,  # type: ignore[arg-type,return-value]
    )


def test_a_rejected_object_stays_rejected_even_if_new_rules_would_pass_it() -> None:
    rejected = _noise_report(STRICT)
    assert rejected.verdict is Verdict.FAIL
    would_pass = factories.validation_report(Verdict.PASS, subject=rejected.subject)
    report = retro_audit(
        [_subject(LifecycleState.REJECTED, rejected, would_pass)],
        audited_at=AUDITED_AT,
        rules="test-only",
    )
    (finding,) = report.findings
    assert finding.would_now_pass
    assert finding.effective_verdict is Verdict.FAIL
    assert finding.action is AuditAction.STAYS_REJECTED
    assert report.to_dict()["rejected_that_would_now_pass"] == 1


@pytest.mark.parametrize("state", [LifecycleState.REJECTED, LifecycleState.FAILED])
def test_a_terminal_rejection_is_never_flipped_whatever_was_recorded(
    state: LifecycleState,
) -> None:
    recorded = factories.validation_report(Verdict.INCONCLUSIVE)
    current = factories.validation_report(Verdict.PASS)
    (finding,) = retro_audit(
        [_subject(state, recorded, current)], audited_at=AUDITED_AT, rules="t"
    ).findings
    assert (finding.effective_verdict, finding.action) == (
        Verdict.FAIL,
        AuditAction.STAYS_REJECTED,
    )


def test_a_flipping_finding_cannot_even_be_constructed() -> None:
    fields = {
        "subject": factories.strategy_ref(),
        "recorded_verdict": Verdict.FAIL,
        "current_verdict": Verdict.PASS,
        "gate_diffs": (),
        "recorded_profile_hash": factories.HASH_PROFILE,
        "current_profile_hash": factories.HASH_PROFILE,
    }
    with pytest.raises(RetroAuditViolation):
        AuditFinding(
            lifecycle_state=LifecycleState.REJECTED,
            effective_verdict=Verdict.PASS,
            action=AuditAction.NO_CHANGE,
            **fields,  # type: ignore[arg-type]
        )
    with pytest.raises(RetroAuditViolation):  # recorded FAIL, even outside a terminal state
        AuditFinding(
            lifecycle_state=LifecycleState.VALIDATION,
            effective_verdict=Verdict.PASS,
            action=AuditAction.NO_CHANGE,
            **fields,  # type: ignore[arg-type]
        )
    with pytest.raises(RetroAuditViolation):  # an audit can only tighten
        AuditFinding(
            lifecycle_state=LifecycleState.PAPER,
            effective_verdict=Verdict.PASS,
            action=AuditAction.NO_CHANGE,
            **{**fields, "recorded_verdict": Verdict.INCONCLUSIVE},  # type: ignore[arg-type]
        )


def test_a_promoted_object_failing_new_rules_is_flagged_with_gate_diffs() -> None:
    passed_before = _noise_report(LAX, overfitting_only=True)
    assert passed_before.verdict is Verdict.PASS
    fails_now = _noise_report(STRICT, overfitting_only=True)
    report = retro_audit(
        [_subject(LifecycleState.PAPER, passed_before, fails_now)],
        audited_at=AUDITED_AT,
        rules=f"{STRICT.ref}",
    )
    (finding,) = report.findings
    assert finding.action is AuditAction.FLAG_FOR_REVALIDATION
    assert finding.effective_verdict is Verdict.FAIL
    diff = next(d for d in finding.gate_diffs if d.gate_id == "G4.overfitting")
    assert (diff.recorded, diff.current) == (Verdict.PASS, Verdict.FAIL)
    assert finding.recorded_profile_hash != finding.current_profile_hash
    loaded = json.loads(report.to_json())
    assert loaded["flagged_for_revalidation"] == 1
    assert loaded["note"].startswith("report only")


def test_unchanged_and_inconclusive_promoted_objects() -> None:
    same = factories.validation_report(Verdict.PASS)
    unclear = factories.validation_report(Verdict.INCONCLUSIVE)
    findings = retro_audit(
        [
            _subject(LifecycleState.ACTIVE, same, same),
            _subject(LifecycleState.OOS, same, unclear),
            _subject(LifecycleState.RETIRED, same, unclear),
        ],
        audited_at=AUDITED_AT,
        rules="t",
    ).findings
    assert (findings[0].action, findings[0].changed) == (AuditAction.NO_CHANGE, False)
    assert findings[1].action is AuditAction.REVIEW_INCONCLUSIVE
    assert findings[1].effective_verdict is Verdict.INCONCLUSIVE
    assert findings[2].action is AuditAction.TERMINAL_NO_CHANGE


def test_the_rerun_must_be_about_the_same_subject() -> None:
    recorded = factories.validation_report(Verdict.PASS)
    other = factories.validation_report(Verdict.PASS, subject=factories.strategy_ref(name="x"))
    with pytest.raises(ValueError, match="re-run"):
        retro_audit(
            [_subject(LifecycleState.PAPER, recorded, other)], audited_at=AUDITED_AT, rules="t"
        )
