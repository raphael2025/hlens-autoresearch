"""ADR-0053: ``VALIDATION → FAILED`` in the research loop (technical failures during validation).

Only the causes of ADR-0053 §2 move a subject to FAILED — a failed ``G0.reproducibility`` /
``G0.signal_determinism`` gate (``NOT_REPRODUCIBLE``) or a run error in the subject's own code
(``RUN_ERRORED``) — always through the ``LifecycleGuard`` with the ADR-0053 §3 evidence.
Infrastructure errors (the shared backtester, the validator itself, memory / storage faults) file
their FailureRecord and leave the lifecycle unchanged. The strategy / backtester wrappers below are
TEST ONLY fault injections around the real loop components.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.worker import RoundStatus
from apps.worker.loop import FORBIDDEN_TARGETS
from core.contracts.strategy import (
    BacktestProvider,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    StrategyInputError,
    StrategyProvider,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.domain.research import FailureRecord, GateResult, Verdict
from core.errors import LifecycleViolation, ReasonCode
from core.lifecycle.strategy import LifecycleState
from research.loop import ResearchMemory
from research.loop.stages import (
    NOT_REPRODUCIBLE_GATES,
    validation_failed_evidence,
    validation_failed_refusal,
)
from tests import factories
from tests.research.loop import loop_fixtures as fx

ROUND_0 = "loop_round:synthetic_loop:0"
ROUND_1 = "loop_round:synthetic_loop:1"


class FaultyStrategy:
    """TEST ONLY: the real strategy, made non-reproducible or failing on demand."""

    def __init__(self, inner: StrategyProvider) -> None:
        self._inner = inner
        self.calls = 0
        self.flip_from_call: int | None = None
        self.error: BaseException | None = None

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._inner.descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        answer = self._inner.target_positions(request)
        if self.flip_from_call is None or self.calls < self.flip_from_call:
            return answer
        # a different (still contract-valid) answer to the same request: not reproducible
        return StrategyResult.build(request, self.descriptor, map(_flipped, answer.positions))


def _flipped(position: TargetPosition) -> TargetPosition:
    if position.inputs_used == 0:
        return position
    weight = -position.target_weight if position.target_weight else Decimal("0.5")
    return position.model_copy(update={"target_weight": weight})


class FaultyBacktester:
    """TEST ONLY: the real backtester, raising from a given call on (an infrastructure fault)."""

    def __init__(self, inner: BacktestProvider) -> None:
        self._inner = inner
        self.calls = 0
        self.fail_from_call: int | None = None

    @property
    def descriptor(self) -> BacktestProviderDescriptor:
        return self._inner.descriptor

    def run(self, request: BacktestRequest) -> BacktestResult:
        self.calls += 1
        if self.fail_from_call is not None and self.calls >= self.fail_from_call:
            raise RuntimeError("TEST ONLY: backtest storage unavailable")
        return self._inner.run(request)


def _build(tmp: Path) -> tuple[Any, ResearchMemory, FaultyStrategy, FaultyBacktester]:
    base = fx.wiring(evolution=False)
    candidate = base.strategies[0]
    strategy = FaultyStrategy(candidate.strategy)
    backtester = FaultyBacktester(base.backtester)
    wiring = replace(
        base, strategies=(replace(candidate, strategy=strategy),), backtester=backtester
    )
    loop, memory, _ = fx.build(tmp, fx.config(lookbacks=(60,), loop_wiring=wiring))
    return loop, memory, strategy, backtester


def _memory_summary(record: Any) -> Any:
    return next(stage for stage in record.stages if stage.name == "memory").summary


def _only_trial(memory: ResearchMemory) -> Any:
    [hypothesis] = memory.ledger.hypotheses
    return hypothesis


def _assert_no_route_beyond_oos(records: Any) -> None:
    for record in records:
        assert all(t.to_state not in FORBIDDEN_TARGETS for t in record.transitions)
        assert all(t.approved_by is None for t in record.transitions)


# ------------------------------------------------------------------------ the loop, end to end


def test_a_non_reproducible_trial_is_marked_failed_with_the_adr_evidence(tmp_path: Path) -> None:
    loop, memory, strategy, _ = _build(tmp_path)
    strategy.flip_from_call = 2  # call 1 = the experiment run, call 2 = the validator's re-run
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    hypothesis = _only_trial(memory)
    [result] = memory.validations
    assert result.report is not None and result.report.verdict is Verdict.FAIL
    [failure] = memory.failures.records()
    assert failure.terminal_state == "FAILED"
    assert failure.reason_code is ReasonCode.NOT_REPRODUCIBLE
    assert failure.gate_id in NOT_REPRODUCIBLE_GATES
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.FAILED
    moved = record.transitions[-1]
    assert (moved.from_state, moved.to_state) == (LifecycleState.VALIDATION, LifecycleState.FAILED)
    assert moved.evidence == (
        f"validation_report:{result.report.report_id}",
        f"failure_record:{failure.content_hash()}",
        ROUND_0,
    )
    assert moved.approved_by is None
    assert _memory_summary(record)["technical_failures_lifecycle_unchanged"] == []
    _assert_no_route_beyond_oos([record])
    # terminal: never re-evaluated, whatever the next round brings
    strategy.flip_from_call = None
    [later] = loop.run_unattended(1)
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.FAILED
    assert later.transitions == ()


def test_a_re_evaluation_erroring_in_the_subjects_code_is_marked_failed(tmp_path: Path) -> None:
    loop, memory, strategy, _ = _build(tmp_path)
    [first] = loop.run_unattended(1)
    hypothesis = _only_trial(memory)
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.VALIDATION
    strategy.error = StrategyInputError("TEST ONLY: the strategy's own code raised")
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    trial = memory.trials[-1]
    assert trial.attempt == ROUND_1 and trial.subject_fault
    assert trial.error == "StrategyInputError: TEST ONLY: the strategy's own code raised"
    failure = memory.failures.records()[-1]
    assert (failure.terminal_state, failure.reason_code) == ("FAILED", ReasonCode.RUN_ERRORED)
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.FAILED
    [moved] = record.transitions
    assert (moved.from_state, moved.to_state) == (LifecycleState.VALIDATION, LifecycleState.FAILED)
    assert moved.evidence == (
        f"run:{trial.run.run_id}",
        f"failure_record:{failure.content_hash()}",
        ROUND_1,
    )
    assert _memory_summary(record)["technical_failures_lifecycle_unchanged"] == []
    _assert_no_route_beyond_oos([first, record])


def test_a_contract_violating_answer_of_the_subject_is_its_own_run_error(tmp_path: Path) -> None:
    """ADR-0053 §2: a provider answer that ``check_answers`` refuses is the subject's error."""
    loop, memory, strategy, _ = _build(tmp_path)
    loop.run_unattended(1)
    hypothesis = _only_trial(memory)
    answer_descriptor = strategy.descriptor

    def incomplete_answer(request: StrategyRequest) -> StrategyResult:
        answer = FaultyStrategy.target_positions(strategy, request)
        # a well-formed answer that misses the last decision time: refused by check_answers
        return StrategyResult.build(request, answer_descriptor, answer.positions[:-1])

    strategy.target_positions = incomplete_answer  # type: ignore[method-assign]
    [record] = loop.run_unattended(1)
    trial = memory.trials[-1]
    assert trial.subject_fault and (trial.error or "").startswith("ValueError:")
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.FAILED
    assert record.transitions[-1].to_state is LifecycleState.FAILED


@pytest.mark.parametrize(
    "fault",
    ["backtester", "memory_error_in_strategy", "os_error_in_strategy"],
)
def test_an_infrastructure_error_on_re_evaluation_does_not_mark_it_failed(
    tmp_path: Path, fault: str
) -> None:
    loop, memory, strategy, backtester = _build(tmp_path)
    loop.run_unattended(1)
    hypothesis = _only_trial(memory)
    if fault == "backtester":
        backtester.fail_from_call = backtester.calls + 1
    elif fault == "memory_error_in_strategy":
        strategy.error = MemoryError("TEST ONLY: out of memory")
    else:
        strategy.error = OSError("TEST ONLY: network unreachable")
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    trial = memory.trials[-1]
    assert trial.attempt == ROUND_1 and not trial.completed and not trial.subject_fault
    failure = memory.failures.records()[-1]
    assert (failure.terminal_state, failure.reason_code) == ("FAILED", ReasonCode.RUN_ERRORED)
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.VALIDATION
    assert record.transitions == ()
    assert _memory_summary(record)["technical_failures_lifecycle_unchanged"] == [
        str(hypothesis.ref)
    ]


def test_a_validator_error_does_not_mark_the_subject_failed(tmp_path: Path) -> None:
    """ADR-0053 §2: the validator's own error (``report is None``) is infrastructure."""
    loop, memory, _, backtester = _build(tmp_path)
    backtester.fail_from_call = 2  # the experiment's backtest works, the validator's re-run fails
    [record] = loop.run_unattended(1)
    hypothesis = _only_trial(memory)
    [result] = memory.validations
    assert result.report is None and result.error is not None
    [failure] = memory.failures.records()
    assert (failure.terminal_state, failure.reason_code) == ("FAILED", ReasonCode.RUN_ERRORED)
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.VALIDATION
    assert [t.to_state for t in record.transitions] == [
        LifecycleState.CANDIDATE,
        LifecycleState.VALIDATION,
    ]
    assert _memory_summary(record)["technical_failures_lifecycle_unchanged"] == [
        str(hypothesis.ref)
    ]


# ------------------------------------------------------------------ the allowed causes (§2 / §3)


def _record(
    reason: ReasonCode, *, state: str = "FAILED", gate_id: str | None = None
) -> FailureRecord:
    return FailureRecord(
        subject_ref=factories.hypothesis_ref(),
        terminal_state=state,
        reason_code=reason,
        gate_id=gate_id,
        evidence=("test-only",),
        recorded_at=fx.T0,
    )


def _report(gate_id: str, verdict: Verdict = Verdict.FAIL) -> Any:
    gate = GateResult(gate_id=gate_id, metric="test_only", value=0.0, verdict=verdict)
    return factories.validation_report(verdict, gates=(gate,))


def test_the_allowed_causes_build_exactly_the_adr_evidence() -> None:
    for gate_id in sorted(NOT_REPRODUCIBLE_GATES):
        record = _record(ReasonCode.NOT_REPRODUCIBLE, gate_id=gate_id)
        report = _report(gate_id)
        assert validation_failed_refusal(record, report=report) is None
        assert validation_failed_evidence(record, ROUND_0, report=report) == (
            f"validation_report:{report.report_id}",
            f"failure_record:{record.content_hash()}",
            ROUND_0,
        )
    record = _record(ReasonCode.RUN_ERRORED)
    assert validation_failed_evidence(record, ROUND_1, run_id="r-1", subject_fault=True) == (
        "run:r-1",
        f"failure_record:{record.content_hash()}",
        ROUND_1,
    )


@pytest.mark.parametrize(
    ("record", "kwargs"),
    [
        # a statistical / robustness FAIL is REJECTED, never FAILED
        (
            _record(
                ReasonCode.NOT_SIGNIFICANT_AFTER_MTC, state="REJECTED", gate_id="G3.significance"
            ),
            {"report": _report("G3.significance")},
        ),
        # a technical failure outside the reproducibility class
        (_record(ReasonCode.CONTRACT_VIOLATION), {"run_id": "r-1", "subject_fault": True}),
        # NOT_REPRODUCIBLE without the report, at another gate, or at a gate that did not fail
        (_record(ReasonCode.NOT_REPRODUCIBLE, gate_id="G0.reproducibility"), {}),
        (
            _record(ReasonCode.NOT_REPRODUCIBLE, gate_id="G0.bindings"),
            {"report": _report("G0.bindings")},
        ),
        (
            _record(ReasonCode.NOT_REPRODUCIBLE, gate_id="G0.reproducibility"),
            {"report": _report("G0.signal_determinism")},
        ),
        # RUN_ERRORED found by a validation gate, or not caused by the subject (infrastructure)
        (
            _record(ReasonCode.RUN_ERRORED, gate_id="G0.run_state"),
            {"report": _report("G0.run_state")},
        ),
        (_record(ReasonCode.RUN_ERRORED), {"run_id": "r-1", "subject_fault": False}),
    ],
    ids=[
        "statistical-fail",
        "contract-violation",
        "no-report",
        "other-g0-gate",
        "gate-not-failed",
        "g0-run-state",
        "infrastructure",
    ],
)
def test_disallowed_causes_are_refused(record: FailureRecord, kwargs: dict[str, Any]) -> None:
    assert validation_failed_refusal(
        record,
        report=kwargs.get("report"),
        subject_fault=kwargs.get("subject_fault", False),
    )
    with pytest.raises(LifecycleViolation, match="ADR-0053"):
        validation_failed_evidence(record, ROUND_0, **kwargs)


def test_the_evidence_needs_the_round_and_the_errored_run() -> None:
    record = _record(ReasonCode.RUN_ERRORED)
    with pytest.raises(LifecycleViolation, match="round"):
        validation_failed_evidence(record, "round-0", run_id="r-1", subject_fault=True)
    with pytest.raises(LifecycleViolation, match="errored run"):
        validation_failed_evidence(record, ROUND_0, subject_fault=True)
