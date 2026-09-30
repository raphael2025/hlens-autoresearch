"""ADR-0083: explicit, human-reviewed, durable retry admission of a failed experiment round (v6).

Scenario (every number TEST ONLY, from ``loop_fixtures``; see its docstring): one lookback-60
knowledge hypothesis, no evolution, the scripted LLM. Round 0's experiment stage is made to fail
(ADR-0070: the loop stops for human review); a human then admits a retry of that hypothesis
under fresh attempt keys. Crashes are simulated by making one step of the admission raise.

Several tests need two distinct, already-registered hypotheses to retry together (ADR-0083
"PM 决定" §4: one admission may never repeat a hypothesis, so a multi-item manifest test can no
longer list one hypothesis twice) — they use ``_fail_round_1_with_two_hypotheses`` /
``_fail_round_1_with_two_hypotheses_one_trial_each`` and a two-lookback config
(``_config_two_hypotheses`` / ``_open_two_hypotheses``) instead of the single-hypothesis scenario.
"""

from __future__ import annotations

import gc
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.worker import LoopBudget
from apps.worker.loop import LoopHalted, RoundStatus, StageStatus
from core.domain.research import Hypothesis
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.hypotheses import TrialLedger
from research.loop import (
    DurableLoop,
    FileAnchor,
    LoopStateInconsistent,
    SyntheticLoopConfig,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.loop.durable import (
    LEDGER_FILE,
    MEMORY_FILE,
    PLAN_ADMISSION_FILE,
    RETRY_ADMISSION,
    RETRY_DIR,
    DurableState,
    MemoryCheckpoint,
    open_state,
)
from research.loop.recovery_review import FailedRoundReviewPacket, failed_round_review_packet
from research.loop.retry_admission import RetryAdmissionError, RetryManifestItem
from research.loop.trials import ExperimentStage
from research.persistence import AppendOnlyJournal
from tests.research.loop import loop_fixtures as fx

REVIEWER = "test-human"
LLM_LOOKBACKS = (None, 240, 1440)


def _config(budget: LoopBudget = fx.TEST_ONLY_BUDGET) -> SyntheticLoopConfig:
    return fx.config(budget=budget, lookbacks=(60,), loop_wiring=fx.wiring(evolution=False))


def _llm(consumed: int) -> ScriptedLLMProvider:
    """The scripted LLM, resumed after ``consumed`` calls (its position is not loop state)."""
    outputs = [fx.llm_output(i, lookback) for i, lookback in enumerate(LLM_LOOKBACKS)]
    return ScriptedLLMProvider(outputs[consumed:], clock=lambda: fx.T0)


def _open(
    state_dir: Path,
    *,
    consumed: int = 0,
    retry: bool = True,
    budget: LoopBudget = fx.TEST_ONLY_BUDGET,
    anchor: Path | None = None,
    operator_identity: str | None = None,
) -> DurableLoop:
    return open_synthetic_loop(
        _config(budget),
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=_llm(consumed),
        anchor=anchor,
        operator_identity=operator_identity,
        enable_failed_round_retry=retry,
    )


def _fail_round_0(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    budget: LoopBudget = fx.TEST_ONLY_BUDGET,
    anchor: Path | None = None,
) -> DurableLoop:
    """Round 0 registers its hypothesis, then its experiment stage fails (ADR-0070 stop)."""
    durable = _open(state_dir, budget=budget, anchor=anchor)

    def died(*_: Any, **__: Any) -> Any:
        raise RuntimeError("TEST ONLY: the experiment infrastructure died")

    with monkeypatch.context() as patch:
        patch.setattr(ExperimentStage, "_trial", died)
        [record] = durable.loop.run_unattended(1)
    experiment = {stage.name: stage for stage in record.stages}["experiment"]
    assert experiment.status is StageStatus.FAILED
    assert durable.loop.recovery_required is not None
    return durable


def _config_two_hypotheses(budget: LoopBudget = fx.TEST_ONLY_BUDGET) -> SyntheticLoopConfig:
    return fx.config(budget=budget, lookbacks=(60, 240), loop_wiring=fx.wiring(evolution=False))


def _open_two_hypotheses(
    state_dir: Path,
    *,
    consumed: int = 0,
    budget: LoopBudget = fx.TEST_ONLY_BUDGET,
    anchor: Path | None = None,
) -> DurableLoop:
    """Like ``_open`` but for ``_config_two_hypotheses`` (its ``knowledge`` differs, and the
    fingerprint binds it — ``settings_fingerprint``, ``research.loop.compose``)."""
    return open_synthetic_loop(
        _config_two_hypotheses(budget),
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=_llm(consumed),
        anchor=anchor,
        enable_failed_round_retry=True,
    )


def _fail_round_1_with_two_hypotheses(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    budget: LoopBudget = fx.TEST_ONLY_BUDGET,
) -> tuple[DurableLoop, Hypothesis, Hypothesis]:
    """Round 0 registers the first knowledge hypothesis; round 1 registers the second (the
    accumulated research window still has room under ``max_new_hypotheses_per_round``) and
    re-evaluates the first (the window grew), then its experiment stage fails — two distinct,
    already-registered hypotheses to retry (ADR-0083 "PM 决定" §4: one admission may never repeat
    a hypothesis, so a multi-item manifest test now needs two of them, not one listed twice)."""
    durable = _open_two_hypotheses(state_dir, budget=budget)
    [first_record] = durable.loop.run_unattended(1)
    assert first_record.status is RoundStatus.COMPLETED
    [first] = durable.memory.ledger.hypotheses

    def died(*_: Any, **__: Any) -> Any:
        raise RuntimeError("TEST ONLY: the experiment infrastructure died")

    with monkeypatch.context() as patch:
        patch.setattr(ExperimentStage, "_trial", died)
        [record] = durable.loop.run_unattended(1)
    experiment = {stage.name: stage for stage in record.stages}["experiment"]
    assert experiment.status is StageStatus.FAILED
    assert durable.loop.recovery_required is not None
    [second] = [h for h in durable.memory.ledger.hypotheses if h.ref != first.ref]
    return durable, first, second


def _fail_round_1_with_two_hypotheses_one_trial_each(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, *, budget: LoopBudget
) -> tuple[DurableLoop, Hypothesis, Hypothesis]:
    """Round 0 covers the whole research window in one round (``days_per_round=6``), so its
    hypothesis reaches a definitive verdict at once (PASS, given the planted 60-minute effect) and
    moves to ``OOS`` — never re-evaluated; round 1 then registers only the second knowledge
    hypothesis, exactly one declared trial each round, before its experiment stage fails. Two
    distinct, already-registered hypotheses (ADR-0083 "PM 决定" §4), sized so a caller can pick a
    ``max_trials_per_round`` that a 1-trial round always fits but a 2-item retry manifest does
    not."""
    cfg = fx.config(
        budget=budget, lookbacks=(60, 240), loop_wiring=fx.wiring(evolution=False), days_per_round=6
    )
    durable = open_synthetic_loop(
        cfg,
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=_llm(0),
        enable_failed_round_retry=True,
    )
    [first_record] = durable.loop.run_unattended(1)
    assert first_record.status is RoundStatus.COMPLETED
    [first] = durable.memory.ledger.hypotheses
    # a definitive verdict (PASS -> OOS, given the planted 60-minute effect, or FAIL -> REJECTED)
    # on the whole window at once: either way it leaves VALIDATION and is never re-evaluated again
    assert durable.loop.guard.state_of(first.ref) in (LifecycleState.OOS, LifecycleState.REJECTED)

    def died(*_: Any, **__: Any) -> Any:
        raise RuntimeError("TEST ONLY: the experiment infrastructure died")

    with monkeypatch.context() as patch:
        patch.setattr(ExperimentStage, "_trial", died)
        [record] = durable.loop.run_unattended(1)
    experiment = {stage.name: stage for stage in record.stages}["experiment"]
    assert experiment.status is StageStatus.FAILED
    assert durable.loop.recovery_required is not None
    [second] = [h for h in durable.memory.ledger.hypotheses if h.ref != first.ref]
    return durable, first, second


def _state(durable: DurableLoop) -> DurableState:
    assert durable.durable_state is not None
    return durable.durable_state


def _packet(durable: DurableLoop) -> FailedRoundReviewPacket:
    packet = failed_round_review_packet(_state(durable))
    assert packet is not None
    return packet


def _hypothesis(durable: DurableLoop) -> Hypothesis:
    [hypothesis] = durable.memory.ledger.hypotheses
    return hypothesis


def _item(hypothesis: Hypothesis, attempt: str) -> RetryManifestItem:
    return RetryManifestItem(
        hypothesis.name, hypothesis.version, hypothesis.content_hash(), attempt
    )


def _retry_files(state_dir: Path) -> list[Path]:
    directory = state_dir / RETRY_DIR
    return sorted(directory.iterdir()) if directory.exists() else []


def _lines(path: Path) -> int:
    return len(AppendOnlyJournal(path).entries)


def _close(durable: DurableLoop) -> None:
    durable.close()
    gc.collect()


# --------------------------------------------------------------------------- refusals (no write)


def test_a_retry_without_the_review_packet_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    manifest = [_item(_hypothesis(durable), "retry-1")]
    ledger_lines = _lines(state_dir / LEDGER_FILE)
    for packet in (None, {"packet_version": "1.0.0"}):
        with pytest.raises(RetryAdmissionError, match="review packet"):
            durable.admit_failed_round_retry(packet=packet, reviewer=REVIEWER, manifest=manifest)
    assert _retry_files(state_dir) == []
    assert _lines(state_dir / LEDGER_FILE) == ledger_lines
    assert durable.loop.recovery_required is not None
    with pytest.raises(LoopHalted, match="human review"):
        durable.loop.submit_round(1)


@pytest.mark.parametrize(
    "reviewer", ["", " test-human", "system", "automation", "research_loop:synthetic_loop"]
)
def test_an_automated_or_empty_reviewer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviewer: str
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    with pytest.raises(RetryAdmissionError, match="reviewer"):
        durable.admit_failed_round_retry(
            packet=_packet(durable),
            reviewer=reviewer,
            manifest=[_item(_hypothesis(durable), "retry-1")],
        )
    assert _retry_files(state_dir) == []
    assert durable.loop.recovery_required is not None


def test_a_failed_round_is_admitted_once_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    packet, hypothesis = _packet(durable), _hypothesis(durable)
    durable.admit_failed_round_retry(
        packet=packet, reviewer=REVIEWER, manifest=[_item(hypothesis, "retry-1")]
    )
    with pytest.raises(LoopStateInconsistent, match="already has a retry admission"):
        _state(durable).admit_failed_round_retry(
            packet=packet, reviewer=REVIEWER, manifest=[_item(hypothesis, "retry-2")]
        )
    _close(durable)
    reopened = _open(state_dir, consumed=1)
    assert reopened.loop.recovery_required is not None  # stopped until explicitly resumed
    with pytest.raises(LoopStateInconsistent, match="already has a retry admission"):
        reopened.admit_failed_round_retry(
            packet=packet, reviewer=REVIEWER, manifest=[_item(hypothesis, "retry-3")]
        )
    assert len(_retry_files(state_dir)) == 1


def test_g1_an_unregistered_manifest_hypothesis_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    hypothesis = _hypothesis(durable)
    stranger = hypothesis.model_copy(update={"name": "h_never_registered"})
    ledger_lines = _lines(state_dir / LEDGER_FILE)
    with pytest.raises(RetryAdmissionError, match="never registered"):
        durable.admit_failed_round_retry(
            packet=_packet(durable),
            reviewer=REVIEWER,
            manifest=[_item(hypothesis, "retry-1"), _item(stranger, "retry-2")],
        )
    assert _retry_files(state_dir) == [] and _lines(state_dir / LEDGER_FILE) == ledger_lines


def test_g1_a_manifest_hash_that_differs_from_the_registration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    hypothesis = _hypothesis(durable)
    edited = hypothesis.model_copy(update={"statement": "an edited statement is a new version"})
    ledger_lines = _lines(state_dir / LEDGER_FILE)
    for item in (
        _item(edited, "retry-1"),
        RetryManifestItem(hypothesis.name, hypothesis.version, "0" * 64, "retry-1"),
    ):
        with pytest.raises(RetryAdmissionError, match="differs from the content"):
            durable.admit_failed_round_retry(
                packet=_packet(durable), reviewer=REVIEWER, manifest=[item]
            )
    assert _retry_files(state_dir) == [] and _lines(state_dir / LEDGER_FILE) == ledger_lines


def test_g2_a_retry_beyond_the_round_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0083 "PM 决定" §4: two distinct hypotheses (a manifest may never repeat one), each
    declaring exactly one trial a round — the retry manifest of both exceeds a cap of one."""
    state_dir = tmp_path / "state"
    cap_budget = replace(fx.TEST_ONLY_BUDGET, max_trials_per_round=1)
    durable, first, second = _fail_round_1_with_two_hypotheses_one_trial_each(
        state_dir, monkeypatch, budget=cap_budget
    )
    with pytest.raises(RetryAdmissionError, match="max_trials_per_round"):
        durable.admit_failed_round_retry(
            packet=_packet(durable),
            reviewer=REVIEWER,
            manifest=[_item(first, "retry-a"), _item(second, "retry-b")],
        )
    assert _retry_files(state_dir) == []


#: TEST ONLY: sized so ``_fail_round_1_with_two_hypotheses`` (3 trials spent: round 0's
#: registration, round 1's fresh registration and re-evaluation) leaves exactly 1 remaining.
NARROW_TOTAL_TWO = LoopBudget(
    max_trials_per_round=50,
    max_trials_total=4,
    max_llm_cost_units=fx.TEST_ONLY_BUDGET.max_llm_cost_units,
    max_compute_seconds=fx.TEST_ONLY_BUDGET.max_compute_seconds,
)


def test_g2_a_retry_beyond_the_remaining_hard_total_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0083 "PM 决定" §4: two distinct hypotheses (a manifest may never repeat one), sized so
    the remaining total is exactly 1 after the failed round."""
    state_dir = tmp_path / "state"
    durable, first, second = _fail_round_1_with_two_hypotheses(
        state_dir, monkeypatch, budget=NARROW_TOTAL_TWO
    )
    spent = durable.loop.total_usage.trials  # max(declared, actual), failed round included
    remaining = NARROW_TOTAL_TWO.max_trials_total - spent
    assert remaining == 1
    with pytest.raises(RetryAdmissionError, match="remain under max_trials_total"):
        durable.admit_failed_round_retry(
            packet=_packet(durable),
            reviewer=REVIEWER,
            manifest=[_item(first, "retry-a"), _item(second, "retry-b")],
        )
    assert _retry_files(state_dir) == []
    receipt = durable.admit_failed_round_retry(  # exactly the remaining total still fits
        packet=_packet(durable), reviewer=REVIEWER, manifest=[_item(first, "retry-a")]
    )
    assert receipt.failed_record_hash == durable.loop.audit.records[-1].record_hash


# ------------------------------------------------------------------------ the admitted retry


def test_an_admitted_retry_runs_once_and_the_trial_ledger_only_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    ledger = durable.memory.ledger
    hypothesis = _hypothesis(durable)
    failed_log = ledger.trial_log
    family_before = ledger.trials(fx.FAMILY)

    receipt = durable.admit_failed_round_retry(
        packet=_packet(durable), reviewer=REVIEWER, manifest=[_item(hypothesis, "retry-1")]
    )
    assert ledger.trial_log[: len(failed_log)] == failed_log  # the failed trial stays counted
    assert ledger.trials(fx.FAMILY) == family_before + 1
    assert durable.loop.recovery_required is None
    [journal] = _retry_files(state_dir)
    assert journal.name == f"{receipt.failed_record_hash}.jsonl"
    assert [e.type for e in AppendOnlyJournal(journal).entries] == ["retry_prepare", "retry_commit"]
    memory_lines = AppendOnlyJournal(state_dir / MEMORY_FILE).entries
    last = memory_lines[-1]
    assert (last.type, last.seq) == (RETRY_ADMISSION, receipt.checkpoint_seq)

    [record] = durable.loop.run_unattended(1)
    stages = {stage.name: stage for stage in record.stages}
    summary = stages["hypothesis"].summary
    assert summary is not None and summary["registered"] == []
    assert summary["retry_reevaluations"] == [
        {
            "hypothesis": str(hypothesis.ref),
            "hypothesis_hash": hypothesis.content_hash(),
            "attempt": "retry-1",
        }
    ]
    assert stages["experiment"].status is StageStatus.COMPLETED
    rows = (stages["experiment"].summary or {})["experiments"]
    assert [(row["origin"], row["attempt"]) for row in rows] == [("retry", "retry-1")]
    assert durable.memory.retry_reevaluations == []
    assert ledger.trials(fx.FAMILY) == family_before + 1  # the retry round registers nothing new
    _close(durable)

    reopened = _open(state_dir, consumed=1)
    assert reopened.memory.ledger.trial_log[: len(failed_log)] == failed_log
    assert reopened.memory.ledger.trials(fx.FAMILY) == family_before + 1
    assert reopened.memory.retry_reevaluations == []
    with pytest.raises(LoopStateInconsistent):
        reopened.resume_failed_round_retry()  # consumed: never run twice


def test_a_reopened_admission_waits_for_an_explicit_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    hypothesis = _hypothesis(durable)
    receipt = _state(durable).admit_failed_round_retry(  # durable only; this loop stays stopped
        packet=_packet(durable), reviewer=REVIEWER, manifest=[_item(hypothesis, "retry-1")]
    )
    _close(durable)
    reopened = _open(state_dir, consumed=1)
    assert reopened.loop.recovery_required is not None
    assert [(h, a) for h, a in reopened.memory.retry_reevaluations] == [(hypothesis, "retry-1")]
    assert reopened.resume_failed_round_retry() == receipt
    assert reopened.loop.recovery_required is None
    with pytest.raises(LoopHalted, match="no failed experiment round"):
        reopened.resume_failed_round_retry()


# ---------------------------------------------------------------------------- crash recovery


def test_a_crash_after_prepare_is_recovered_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also proves ADR-0083 "PM 决定" §4: the retry round's validation stage tells the two
    hypotheses' results apart by their own attempt key (``#attempt`` in the run/report identity),
    never conflating them, even though both complete in the very same round."""
    state_dir = tmp_path / "state"
    durable, first, second = _fail_round_1_with_two_hypotheses(state_dir, monkeypatch)
    packet = _packet(durable)
    manifest = [_item(first, "retry-1"), _item(second, "retry-2")]
    family_before = durable.memory.ledger.trials(fx.FAMILY)
    ledger_lines = _lines(state_dir / LEDGER_FILE)
    memory_lines = _lines(state_dir / MEMORY_FILE)
    original = TrialLedger.register_reevaluation
    calls: list[str] = []

    def dies_on_the_second(self: TrialLedger, candidate: Hypothesis, attempt: str) -> bool:
        calls.append(attempt)
        if len(calls) == 2:
            raise RuntimeError("TEST ONLY: the process died")
        return original(self, candidate, attempt)

    with monkeypatch.context() as patch:
        patch.setattr(TrialLedger, "register_reevaluation", dies_on_the_second)
        with pytest.raises(RuntimeError, match="process died"):
            durable.admit_failed_round_retry(packet=packet, reviewer=REVIEWER, manifest=manifest)
    [journal] = _retry_files(state_dir)
    assert [e.type for e in AppendOnlyJournal(journal).entries] == ["retry_prepare"]
    assert _lines(state_dir / LEDGER_FILE) == ledger_lines + 1
    with pytest.raises(LoopStateInconsistent, match="stopped after writing"):  # poisoned
        _state(durable).admit_failed_round_retry(
            packet=packet, reviewer=REVIEWER, manifest=manifest
        )
    assert durable.loop.recovery_required is not None
    _close(durable)

    with pytest.raises(LoopStateInconsistent, match="recovered exactly"):
        _open_two_hypotheses(state_dir, consumed=2)
    assert [e.type for e in AppendOnlyJournal(journal).entries] == [
        "retry_prepare",
        "retry_commit",
    ]
    assert _lines(state_dir / LEDGER_FILE) == ledger_lines + 2  # only the one missing line
    tail = AppendOnlyJournal(state_dir / MEMORY_FILE).entries
    assert len(tail) == memory_lines + 1 and tail[-1].type == RETRY_ADMISSION

    reopened = _open_two_hypotheses(state_dir, consumed=2)
    assert reopened.memory.ledger.trials(fx.FAMILY) == family_before + 2
    assert [(h.ref, a) for h, a in reopened.memory.retry_reevaluations] == [
        (first.ref, "retry-1"),
        (second.ref, "retry-2"),
    ]
    reopened.resume_failed_round_retry()
    [record] = reopened.loop.run_unattended(1)
    stages = {s.name: s for s in record.stages}
    assert stages["experiment"].status is StageStatus.COMPLETED
    rows = (stages["experiment"].summary or {})["experiments"]
    by_attempt = {row["attempt"]: row for row in rows}
    assert set(by_attempt) == {"retry-1", "retry-2"}
    assert {row["hypothesis"] for row in by_attempt.values()} == {
        str(first.ref),
        str(second.ref),
    }
    assert all(row["origin"] == "retry" for row in by_attempt.values())
    # the run/report identity is disambiguated by the attempt key, not only the round + hypothesis
    assert all(row["run_id"].endswith(f"#{attempt}") for attempt, row in by_attempt.items())
    assert stages["validation"].status is StageStatus.COMPLETED
    assert stages["validation"].summary is not None
    reports = stages["validation"].summary["reports"]
    assert {(r["hypothesis"], r["attempt"]) for r in reports} == {
        (str(first.ref), "retry-1"),
        (str(second.ref), "retry-2"),
    }


def test_a_crash_after_commit_before_the_checkpoint_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    hypothesis = _hypothesis(durable)
    ledger_lines = _lines(state_dir / LEDGER_FILE)
    memory_lines = _lines(state_dir / MEMORY_FILE)

    def died(*_: Any, **__: Any) -> Any:
        raise RuntimeError("TEST ONLY: the process died before its checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(MemoryCheckpoint, "retry_admission", died)
        with pytest.raises(RuntimeError, match="before its checkpoint"):
            durable.admit_failed_round_retry(
                packet=_packet(durable), reviewer=REVIEWER, manifest=[_item(hypothesis, "r1")]
            )
    [journal] = _retry_files(state_dir)
    assert len(AppendOnlyJournal(journal).entries) == 2
    assert _lines(state_dir / LEDGER_FILE) == ledger_lines + 1
    assert _lines(state_dir / MEMORY_FILE) == memory_lines
    _close(durable)

    with pytest.raises(LoopStateInconsistent, match="recovered exactly"):
        _open(state_dir, consumed=1)
    assert _lines(state_dir / LEDGER_FILE) == ledger_lines + 1  # nothing registered twice
    assert len(AppendOnlyJournal(journal).entries) == 2
    tail = AppendOnlyJournal(state_dir / MEMORY_FILE).entries
    assert len(tail) == memory_lines + 1 and tail[-1].type == RETRY_ADMISSION
    reopened = _open(state_dir, consumed=1)
    assert [a for _, a in reopened.memory.retry_reevaluations] == ["r1"]


def test_a_crash_after_the_checkpoint_only_catches_up_the_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, anchor = tmp_path / "state", tmp_path / "anchor.jsonl"
    durable = _fail_round_0(state_dir, monkeypatch, anchor=anchor)
    hypothesis = _hypothesis(durable)
    anchored = FileAnchor(anchor).load()

    def died(*_: Any, **__: Any) -> Any:
        raise RuntimeError("TEST ONLY: the anchor host was unreachable")

    with monkeypatch.context() as patch:
        patch.setattr(DurableState, "publish_anchor", died)
        with pytest.raises(RuntimeError, match="unreachable"):
            durable.admit_failed_round_retry(
                packet=_packet(durable), reviewer=REVIEWER, manifest=[_item(hypothesis, "r1")]
            )
    assert FileAnchor(anchor).load() == anchored
    _close(durable)
    reopened = _open(state_dir, consumed=1, anchor=anchor)  # no recovery write is needed
    head = FileAnchor(anchor).load()
    assert head is not None and anchored is not None and head.memory_seq == anchored.memory_seq + 1
    reopened.resume_failed_round_retry()
    assert reopened.loop.recovery_required is None


def test_a_deleted_or_foreign_retry_journal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    durable = _fail_round_0(state_dir, monkeypatch)
    durable.admit_failed_round_retry(
        packet=_packet(durable), reviewer=REVIEWER, manifest=[_item(_hypothesis(durable), "r1")]
    )
    _close(durable)
    [journal] = _retry_files(state_dir)
    foreign = state_dir / RETRY_DIR / "notes.txt"
    foreign.write_text("not a journal\n")
    with pytest.raises(LoopStateInconsistent, match="not a retry journal file"):
        _open(state_dir, consumed=1)
    foreign.unlink()
    saved = journal.read_bytes()
    journal.unlink()
    with pytest.raises(LoopStateInconsistent, match="retry"):
        _open(state_dir, consumed=1)
    journal.write_bytes(saved)
    _open(state_dir, consumed=1)


# ------------------------------------------------------------------ v3 / v4 / v5 are unchanged


def test_v4_and_v5_directories_hold_no_retry_state_and_are_never_migrated(
    tmp_path: Path,
) -> None:
    v4 = tmp_path / "v4"
    durable = _open(v4, retry=False)
    durable.loop.run_unattended(1)
    lines = AppendOnlyJournal(v4 / MEMORY_FILE).entries
    assert lines[0].payload["state_version"] == 4
    assert all("retry_admission" not in line.payload.get("heads", {}) for line in lines)
    assert AppendOnlyJournal(v4 / PLAN_ADMISSION_FILE).entries[0].payload["state_version"] == 4
    assert not (v4 / RETRY_DIR).exists()
    with pytest.raises(LoopStateInconsistent, match="v6"):
        _state(durable).admit_failed_round_retry(packet=None, reviewer=REVIEWER, manifest=[])
    _close(durable)
    with pytest.raises(LoopStateInconsistent, match="never migrated"):
        _open(v4, consumed=1, retry=True)
    _close(_open(v4, consumed=1, retry=False))

    v5, identity = tmp_path / "v5", "a" * 64
    _close(_open(v5, retry=False, operator_identity=identity))
    assert AppendOnlyJournal(v5 / MEMORY_FILE).entries[0].payload["state_version"] == 5
    assert not (v5 / RETRY_DIR).exists()
    with pytest.raises(LoopStateInconsistent, match="never migrated"):
        _open(v5, retry=True)
    with pytest.raises(ValueError, match="operator_identity"):
        _open(tmp_path / "both", retry=True, operator_identity=identity)

    v6 = tmp_path / "v6"
    _close(_open(v6))
    assert AppendOnlyJournal(v6 / MEMORY_FILE).entries[0].payload["state_version"] == 6
    # v6 keeps the v4 plan admission journal format
    assert AppendOnlyJournal(v6 / PLAN_ADMISSION_FILE).entries[0].payload["state_version"] == 4
    with pytest.raises(LoopStateInconsistent, match="never migrated"):
        _open(v6, retry=False)


def test_a_v3_directory_holds_no_retry_state_and_is_never_migrated(tmp_path: Path) -> None:
    state_dir = tmp_path / "v3"
    config = _config()

    def opened(version: int) -> DurableState:
        return open_state(
            state_dir,
            fingerprint=loop_fingerprint(config),
            strategies=config.wiring.strategies,
            provider=RandomWalkMarket(),
            provider_for=None,
            state_version=version,
        )

    state = opened(3)
    assert state.checkpoint.header().payload["state_version"] == 3
    assert not (state_dir / RETRY_DIR).exists() and not (state_dir / PLAN_ADMISSION_FILE).exists()
    with pytest.raises(LoopStateInconsistent, match="v6"):
        state.admit_failed_round_retry(packet=None, reviewer=REVIEWER, manifest=[])
    assert state.lock is not None
    state.lock.release()
    with pytest.raises(LoopStateInconsistent, match="never migrated"):
        opened(6)
    reopened = opened(3)
    assert reopened.lock is not None
    reopened.lock.release()
