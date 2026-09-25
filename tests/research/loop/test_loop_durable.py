"""One state directory makes the composed research loop restart-safe (ADR-0049 implementation
note, durable composition, 2026-09-26).

Scenario (every number TEST ONLY, from ``loop_fixtures``; see its docstring): one lookback-60
knowledge hypothesis, 4-day rounds, research window days 0-3 and the sealed OOS day 3-4, an
explicit unseal budget for the family (G5 runs in round 0), evolution every round and the scripted
LLM. After round 0 a human approves the LLM draft ``h_llm_0`` (no strategy condition: it errors in
round 1 and files a FailureRecord). So after three rounds every file of the state directory holds
state: audit, memory checkpoints, trial ledger, sealed-OOS ledger, lineage, reviews, failures.
"""

from __future__ import annotations

import gc
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from apps.worker import LoopAuditLog, ResearchLoop
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import (
    LoopStateInconsistent,
    OosUnsealBudget,
    ResearchMemory,
    ReviewQueue,
    SyntheticLoopConfig,
    build_synthetic_loop,
    open_synthetic_loop,
)
from research.loop.durable import (
    AUDIT_FILE,
    FAILURES_FILE,
    LEDGER_FILE,
    LINEAGE_FILE,
    MEMORY_FILE,
    REVIEWS_FILE,
    SEALED_OOS_FILE,
)
from research.loop.memory import REVIEW_APPROVED
from research.persistence import AppendOnlyJournal, JournalCorrupted
from research.strategies.failure_registry import FailureRegistry
from tests.research.loop import loop_fixtures as fx

ROUNDS = 3
DRAFT = "h_llm_0@1.0.0"
REVIEWER = "test-human"
LLM_LOOKBACKS = (None, 240, 1440)
ALL_FILES = (
    AUDIT_FILE,
    MEMORY_FILE,
    LEDGER_FILE,
    SEALED_OOS_FILE,
    LINEAGE_FILE,
    REVIEWS_FILE,
    FAILURES_FILE,
)


def _config(seed: int = 11) -> SyntheticLoopConfig:
    unseal = OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: REVIEWER})
    return fx.config(
        seed=seed,
        lookbacks=(60,),
        days_per_round=4,
        profile=fx.loop_profile(boundary_day=3),
        loop_wiring=fx.wiring(evolution=True, oos_unseal=unseal),
    )


def _llm(consumed: int = 0) -> ScriptedLLMProvider:
    """The scripted LLM, resumed after ``consumed`` calls (its position is not loop state)."""
    outputs = [fx.llm_output(i, lookback) for i, lookback in enumerate(LLM_LOOKBACKS)]
    return ScriptedLLMProvider(outputs[consumed:], clock=lambda: fx.T0)


def _open(state_dir: Path, *, consumed: int = 0, seed: int = 11) -> Any:
    return open_synthetic_loop(
        _config(seed),
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=_llm(consumed),
    )


@dataclass(frozen=True)
class Outcome:
    """What must survive a restart unchanged (compared between runs)."""

    record_hashes: list[str]
    total_usage: Any
    trial_log: list[Any]
    family_trials: int
    lifecycle: list[tuple[str, list[tuple[str, str, str, tuple[str, ...]]]]]
    sealed: tuple[Any, bool, int]
    failures: list[str]
    approvals: list[Any]
    lineage: list[str]


def _outcome(loop: ResearchLoop, memory: ResearchMemory) -> Outcome:
    return Outcome(
        record_hashes=[r.record_hash for r in loop.audit.records],
        total_usage=loop.total_usage,
        trial_log=list(memory.ledger.trial_log),
        family_trials=memory.ledger.trials(fx.FAMILY),
        lifecycle=sorted(
            (
                str(h.subject),
                [
                    (t.from_state.value, t.to_state.value, t.reason, tuple(t.evidence))
                    for t in h.transitions
                ],
            )
            for h in loop.guard.histories
        ),
        sealed=(
            memory.oos_ledger.get(fx.FAMILY),
            memory.oos_ledger.is_evaluated(fx.FAMILY),
            memory.oos_ledger.count(),
        ),
        failures=[r.content_hash() for r in memory.failures.records()],
        approvals=list(memory.reviews.approvals),
        lineage=[s.content_hash() for s in memory.lineage],
    )


@pytest.fixture(scope="module")
def uninterrupted(tmp_path_factory: pytest.TempPathFactory) -> Outcome:
    """The same scenario without a state directory (all in memory, never restarted)."""
    tmp = tmp_path_factory.mktemp("uninterrupted")
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    loop = build_synthetic_loop(
        _config(), provider=RandomWalkMarket(), bus=InMemoryEventBus(), memory=memory, llm=_llm()
    )
    loop.run_unattended(1)
    memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    loop.run_unattended(ROUNDS - 1)
    return _outcome(loop, memory)


@pytest.fixture(scope="module")
def restarted(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Outcome]:
    """Round 0, a human approval, every object dropped, rebuilt from the directory, rounds 1-2."""
    state_dir = tmp_path_factory.mktemp("restarted") / "state"
    first = _open(state_dir)
    first.loop.run_unattended(1)
    first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)  # a human, between rounds
    del first
    gc.collect()
    second = _open(state_dir, consumed=1)
    assert len(second.loop.audit.records) == 1
    assert second.memory.reviews.approvals[0].reviewer == REVIEWER
    records = second.loop.run_unattended(ROUNDS - 1)
    assert [r.round_index for r in records] == [1, 2]
    return state_dir, _outcome(second.loop, second.memory)


def _copy(state_dir: Path, tmp_path: Path) -> Path:
    target = tmp_path / "state"
    shutil.copytree(state_dir, target)
    return target


def _drop_trailing_lines(path: Path, count: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) > count
    path.write_text("".join(lines[:-count]), encoding="utf-8")


def _rechain(path: Path, edit: Any) -> None:
    """Rewrite a journal with ``edit(type, payload)`` applied and a **valid** new hash chain
    (what an attacker who knows the format can do; the file alone then looks untouched)."""
    entries = AppendOnlyJournal(path).entries
    path.unlink()
    rewritten = AppendOnlyJournal(path)
    for entry in entries:
        rewritten.append(entry.type, edit(entry.type, json.loads(json.dumps(entry.payload))))


# -------------------------------------------------------------------------------- restart e2e


def test_a_restarted_loop_ends_exactly_like_an_uninterrupted_one(
    restarted: tuple[Path, Outcome], uninterrupted: Outcome
) -> None:
    state_dir, outcome = restarted
    assert outcome == uninterrupted
    assert len(outcome.record_hashes) == ROUNDS
    # the scenario really exercised every stateful part
    assert outcome.sealed[0] is not None and outcome.sealed[0].approved_by == REVIEWER
    assert outcome.sealed[1:] == (True, 1)
    assert outcome.failures and outcome.lineage and outcome.approvals
    states = {subject: moves[-1][1] for subject, moves in outcome.lifecycle}
    assert states[f"hypothesis:{DRAFT}"] == LifecycleState.FAILED.value
    assert LifecycleState.OOS.value in states.values()
    assert all((state_dir / name).stat().st_size > 0 for name in ALL_FILES)


def test_reopening_restores_the_memory_later_rounds_read(restarted: tuple[Path, Outcome]) -> None:
    state_dir, outcome = restarted
    reopened = _open(state_dir, consumed=ROUNDS)
    memory = reopened.memory
    assert _outcome(reopened.loop, memory) == outcome
    assert [m.market_hash for m in memory.markets] == [
        r.stages[0].summary["market_hash"] for r in reopened.loop.audit.records
    ]
    assert len(memory.trials) == sum(
        len(next(s for s in r.stages if s.name == "experiment").summary["experiments"])
        for r in reopened.loop.audit.records
    )
    # a restored validation points at the restored trial object (re-evaluation relies on it)
    assert all(any(v.outcome is t for t in memory.trials) for v in memory.validations)
    assert all(t.inputs is None and t.trial is None for t in memory.trials)
    assert {str(c.spec.ref) for c in memory.strategies.values()} >= {
        row["child"] for row in memory.offspring
    }


def test_state_dir_none_is_unchanged_and_exactly_one_source_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        build_synthetic_loop(_config(), provider=RandomWalkMarket(), bus=InMemoryEventBus())
    with pytest.raises(ValueError, match="exactly one"):
        build_synthetic_loop(
            _config(),
            provider=RandomWalkMarket(),
            bus=InMemoryEventBus(),
            memory=ResearchMemory(failures=FailureRegistry(tmp_path / "f.jsonl")),
            state_dir=tmp_path / "state",
        )
    assert not (tmp_path / "state").exists()


# -------------------------------------------------------------------------- cross-check refusals


def test_a_deleted_ledger_file_is_refused(restarted: tuple[Path, Outcome], tmp_path: Path) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    (state_dir / LEDGER_FILE).unlink()
    with pytest.raises(LoopStateInconsistent, match="trial_ledger .*truncated or deleted"):
        _open(state_dir, consumed=ROUNDS)


def test_an_audit_ahead_of_the_ledger_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    """The audit and checkpoints of three rounds next to the ledger as it was after round 0."""
    state_dir = _copy(restarted[0], tmp_path)
    memory_lines = (state_dir / MEMORY_FILE).read_text(encoding="utf-8").splitlines()
    round_0 = json.loads(memory_lines[1])["payload"]["heads"]["trial_ledger"]["seq"]
    ledger = state_dir / LEDGER_FILE
    lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
    assert 0 < round_0 < len(lines)  # later rounds registered more trials
    ledger.write_text("".join(lines[:round_0]), encoding="utf-8")
    assert len(AppendOnlyJournal(ledger).entries) == round_0  # a valid chain on its own
    with pytest.raises(LoopStateInconsistent, match="trial_ledger .*audit is ahead"):
        _open(state_dir, consumed=ROUNDS)
    # the round-0 ledger of another run of the same configuration is not this history either
    # (its lines carry that run's registration times): refused as rewritten
    older = tmp_path / "older"
    _open(older).loop.run_unattended(1)
    shutil.copyfile(older / LEDGER_FILE, ledger)
    with pytest.raises(LoopStateInconsistent, match="trial_ledger does not match"):
        _open(state_dir, consumed=ROUNDS)


def test_a_tampered_review_approval_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    path = state_dir / REVIEWS_FILE
    original = path.read_text(encoding="utf-8")
    # 1. edited in place: the journal's own chain breaks
    path.write_text(original.replace(f'"{REVIEWER}"', '"mallory"'), encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        _open(state_dir, consumed=ROUNDS)
    # 2. edited and re-chained: the file is valid alone, the checkpoints and the audit disagree
    path.write_text(original, encoding="utf-8")

    def swap(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "reviewer": "mallory"} if kind == REVIEW_APPROVED else payload

    _rechain(path, swap)
    assert ReviewQueue(path).approvals[0].reviewer == "mallory"
    with pytest.raises(LoopStateInconsistent, match="reviews does not match"):
        _open(state_dir, consumed=ROUNDS)
    # 3. an approval re-chained as an automation identity is refused by the queue itself
    path.write_text(original, encoding="utf-8")
    _rechain(
        path,
        lambda kind, payload: (
            {**payload, "reviewer": "research_loop:synthetic_loop"}
            if kind == REVIEW_APPROVED
            else payload
        ),
    )
    with pytest.raises(JournalCorrupted, match="automation"):
        _open(state_dir, consumed=ROUNDS)


@pytest.mark.parametrize(
    ("name", "lines", "match"),
    [
        (AUDIT_FILE, 1, "interrupted"),  # the last round's record line: started, not recorded
        (AUDIT_FILE, 2, "audit records 2 round"),  # a clean, shorter audit
        (MEMORY_FILE, 1, "memory checkpoint holds 2"),
        (LEDGER_FILE, 1, "trial_ledger .*truncated"),
        (SEALED_OOS_FILE, 1, "sealed_oos .*truncated"),
        (LINEAGE_FILE, 1, "lineage .*truncated"),
        (REVIEWS_FILE, 1, "reviews .*truncated"),
        (FAILURES_FILE, 1, "failures .*truncated"),
    ],
)
def test_whole_trailing_lines_removed_from_one_file_are_detected(
    restarted: tuple[Path, Outcome], tmp_path: Path, name: str, lines: int, match: str
) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    path = state_dir / name
    _drop_trailing_lines(path, lines)
    if name not in (AUDIT_FILE, FAILURES_FILE):
        AppendOnlyJournal(path)  # the shorter file alone is a valid chain
    with pytest.raises(LoopStateInconsistent, match=match):
        _open(state_dir, consumed=ROUNDS)


def test_truncating_every_file_consistently_is_the_documented_limit(
    restarted: tuple[Path, Outcome], uninterrupted: Outcome, tmp_path: Path
) -> None:
    """Every file cut back to the end of round 0 (plus the later human approval) is a valid
    earlier history: it opens, and continuing it reproduces the uninterrupted run. Detecting such
    a rollback needs an anchor outside the directory (see ``research.loop.durable``)."""
    state_dir = _copy(restarted[0], tmp_path)
    memory_lines = (state_dir / MEMORY_FILE).read_text(encoding="utf-8").splitlines(keepends=True)
    heads = json.loads(memory_lines[1])["payload"]["heads"]  # the checkpoint of round 0
    keep = {
        MEMORY_FILE: 2,  # header + round 0
        AUDIT_FILE: 2,  # started + recorded round 0
        LEDGER_FILE: heads["trial_ledger"]["seq"],
        SEALED_OOS_FILE: heads["sealed_oos"]["seq"],
        LINEAGE_FILE: heads["lineage"]["seq"],
        REVIEWS_FILE: heads["reviews"]["seq"] + 1,  # + the approval made after round 0
        FAILURES_FILE: heads["failures"]["count"],
    }
    for name, count in keep.items():
        path = state_dir / name
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text("".join(lines[:count]), encoding="utf-8")
    reopened = _open(state_dir, consumed=1)
    assert len(reopened.loop.audit.records) == 1
    reopened.loop.run_unattended(ROUNDS - 1)
    assert _outcome(reopened.loop, reopened.memory) == uninterrupted


def test_an_interrupted_round_is_refused(restarted: tuple[Path, Outcome], tmp_path: Path) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    LoopAuditLog(state_dir / AUDIT_FILE).begin_round("synthetic_loop", ROUNDS)
    with pytest.raises(LoopStateInconsistent, match="interrupted"):
        _open(state_dir, consumed=ROUNDS)


def test_another_configuration_is_refused(restarted: tuple[Path, Outcome], tmp_path: Path) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    with pytest.raises(LoopStateInconsistent, match="another loop configuration"):
        _open(state_dir, consumed=ROUNDS, seed=12)


def test_state_without_its_memory_checkpoint_file_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    (state_dir / MEMORY_FILE).unlink()
    with pytest.raises(LoopStateInconsistent, match="memory checkpoint file is missing"):
        _open(state_dir, consumed=ROUNDS)


def test_a_tampered_checkpoint_delta_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    """A checkpoint re-chained with another trial summary disagrees with the hashed audit."""
    state_dir = _copy(restarted[0], tmp_path)

    def edit(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "round_memory" and payload["round_index"] == 0:
            payload["delta"]["trials"][0]["summary"]["net_return"] = "0.5"
        return payload

    _rechain(state_dir / MEMORY_FILE, edit)
    with pytest.raises(LoopStateInconsistent, match="round 0 is inconsistent"):
        _open(state_dir, consumed=ROUNDS)


# ------------------------------------------------------------------------------- review queue


def test_a_journaled_review_queue_replays_drafts_approvals_and_takes(
    restarted: tuple[Path, Outcome],
) -> None:
    queue = ReviewQueue(restarted[0] / REVIEWS_FILE)
    [approval] = queue.approvals
    assert (approval.key, approval.reviewer) == (DRAFT, REVIEWER)
    assert queue.taken == frozenset({DRAFT})
    assert queue.pending == ("h_llm_1@1.0.0", "h_llm_2@1.0.0")
    assert queue.reviewed_untaken() == ()
    with pytest.raises(ValueError, match="already approved"):
        queue.approve(DRAFT, reviewer="someone-else")
