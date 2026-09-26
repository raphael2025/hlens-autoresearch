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
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest

from apps.worker import LoopAuditLog, LoopBudget, ResearchLoop
from apps.worker.loop import JOB_TOPIC, ROUND_TOPIC
from core.contracts.event_bus import BusMessage
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import FileEventBus, InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import (
    DurableLoop,
    FileAnchor,
    LoopStateInconsistent,
    OosUnsealBudget,
    ResearchMemory,
    ReviewQueue,
    SyntheticLoopConfig,
    build_synthetic_loop,
    check_round_bus,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.loop.compose import BUS_DIR
from research.loop.durable import (
    AUDIT_FILE,
    BETWEEN_ROUNDS,
    FAILURES_FILE,
    LEDGER_FILE,
    LINEAGE_FILE,
    MEMORY_FILE,
    REVIEWS_FILE,
    SEALED_OOS_FILE,
    MemoryCheckpoint,
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


def _unseal() -> OosUnsealBudget:
    return OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: REVIEWER})


def _config(
    seed: int = 11,
    *,
    budget: LoopBudget = fx.TEST_ONLY_BUDGET,
    unseal: OosUnsealBudget | None | Literal["default"] = "default",
) -> SyntheticLoopConfig:
    return fx.config(
        seed=seed,
        budget=budget,
        lookbacks=(60,),
        days_per_round=4,
        profile=fx.loop_profile(boundary_day=3),
        loop_wiring=fx.wiring(
            evolution=True, oos_unseal=_unseal() if unseal == "default" else unseal
        ),
    )


def _llm(consumed: int = 0) -> ScriptedLLMProvider:
    """The scripted LLM, resumed after ``consumed`` calls (its position is not loop state)."""
    outputs = [fx.llm_output(i, lookback) for i, lookback in enumerate(LLM_LOOKBACKS)]
    return ScriptedLLMProvider(outputs[consumed:], clock=lambda: fx.T0)


def _open(
    state_dir: Path,
    *,
    consumed: int = 0,
    seed: int = 11,
    config: SyntheticLoopConfig | None = None,
    anchor: Path | None = None,
) -> Any:
    return open_synthetic_loop(
        config or _config(seed),
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=_llm(consumed),
        anchor=anchor,
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


def _truncate_to_round_0(state_dir: Path) -> None:
    """Cut every file consistently back to the end of round 0 plus the later human approval (and
    its between-rounds checkpoint)."""
    memory_lines = (state_dir / MEMORY_FILE).read_text(encoding="utf-8").splitlines(keepends=True)
    heads = json.loads(memory_lines[1])["payload"]["heads"]  # the checkpoint of round 0
    assert json.loads(memory_lines[2])["type"] == BETWEEN_ROUNDS  # the approval after round 0
    keep = {
        MEMORY_FILE: 3,  # header + round 0 + the approval's between-rounds checkpoint
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
    earlier history: without an anchor it opens, and continuing it reproduces the uninterrupted
    run. Detecting such a rollback needs an anchor outside the directory (see
    ``research.loop.durable`` and the anchored tests below)."""
    state_dir = _copy(restarted[0], tmp_path)
    _truncate_to_round_0(state_dir)
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


# ------------------------------------------------------------- budgets bound to the directory


@pytest.mark.parametrize(
    ("config", "what"),
    [
        (
            lambda: _config(budget=replace(fx.TEST_ONLY_BUDGET, max_trials_total=1000)),
            "the loop budget",
        ),
        (
            lambda: _config(
                budget=replace(fx.TEST_ONLY_BUDGET, max_compute_seconds=Decimal(10**9))
            ),
            "the loop budget",
        ),
        (
            lambda: _config(budget=replace(fx.TEST_ONLY_BUDGET, max_trials_per_round=1)),
            "the loop budget",  # smaller: any change is refused, not only a raise
        ),
        (
            lambda: _config(
                unseal=OosUnsealBudget(max_unsealings=2, approved_families={fx.FAMILY: REVIEWER})
            ),
            "the sealed-OOS unseal budget",
        ),
        (
            lambda: _config(
                unseal=OosUnsealBudget(
                    max_unsealings=1,
                    approved_families={fx.FAMILY: REVIEWER, "another_family": REVIEWER},
                )
            ),
            "the sealed-OOS unseal budget",
        ),
        (
            lambda: _config(
                unseal=OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: "someone"})
            ),
            "the sealed-OOS unseal budget",
        ),
        (lambda: _config(unseal=None), "the sealed-OOS unseal budget"),
    ],
    ids=[
        "bigger-trials-total",
        "bigger-compute",
        "smaller-trials-per-round",
        "bigger-unseal-quota",
        "added-approved-family",
        "other-approver",
        "unseal-removed",
    ],
)
def test_reopening_with_another_budget_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path, config: Any, what: str
) -> None:
    """A budget is bound to its state directory: raising it is a human decision (new dir)."""
    state_dir = _copy(restarted[0], tmp_path)
    before = {name: (state_dir / name).read_bytes() for name in ALL_FILES}
    with pytest.raises(LoopStateInconsistent, match=f"{what}.*NEW state_dir or loop_id"):
        _open(state_dir, consumed=ROUNDS, config=config())
    assert {name: (state_dir / name).read_bytes() for name in ALL_FILES} == before


def test_reopening_with_an_identical_budget_is_accepted(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    same = _config(
        budget=LoopBudget.from_config(fx.TEST_ONLY_BUDGET.payload()),
        unseal=OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: REVIEWER}),
    )
    reopened = _open(state_dir, consumed=ROUNDS, config=same)
    assert len(reopened.loop.audit.records) == ROUNDS


def test_the_fingerprint_binds_the_exact_cadence() -> None:
    base = _config()
    for delta in (timedelta(milliseconds=500), timedelta(microseconds=1)):
        shifted = replace(base, cadence=base.cadence + delta)
        # the former whole-second fingerprint could not tell them apart
        assert int(shifted.cadence.total_seconds()) == int(base.cadence.total_seconds())
        assert loop_fingerprint(shifted) != loop_fingerprint(base)
    assert loop_fingerprint(replace(base)) == loop_fingerprint(base)


def test_the_fingerprint_binds_decision_steps_and_g4_parameters() -> None:
    """Everything that changes what a round computes is part of the state directory's identity."""
    base = _config()
    wiring = base.wiring
    variants = [
        replace(wiring, decision_step=wiring.decision_step + timedelta(microseconds=1)),
        replace(wiring, decision_warmup=wiring.decision_warmup + timedelta(minutes=1)),
        replace(wiring, sealed_decision_step=timedelta(minutes=7)),
        replace(wiring, initial_equity=wiring.initial_equity + 1),
        replace(wiring, robustness=replace(wiring.robustness, cscv_partitions=6)),
    ]
    for changed in variants:
        assert loop_fingerprint(replace(base, wiring=changed)) != loop_fingerprint(base)


# ---------------------------------------------------------------------------- external anchor


@pytest.fixture(scope="module")
def anchored(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Outcome]:
    """The restart scenario with an external anchor (a file outside the state directory)."""
    root = tmp_path_factory.mktemp("anchored")
    state_dir, anchor = root / "state", root / "anchor.jsonl"
    first = _open(state_dir, anchor=anchor)
    first.loop.run_unattended(1)
    first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    del first
    gc.collect()
    second = _open(state_dir, consumed=1, anchor=anchor)
    second.loop.run_unattended(ROUNDS - 1)
    return state_dir, anchor, _outcome(second.loop, second.memory)


def _copy_anchored(anchored: tuple[Path, Path, Outcome], tmp_path: Path) -> tuple[Path, Path]:
    anchor = tmp_path / "anchor.jsonl"
    shutil.copyfile(anchored[1], anchor)
    return _copy(anchored[0], tmp_path), anchor


def test_an_anchored_restart_ends_like_an_uninterrupted_one(
    anchored: tuple[Path, Path, Outcome], uninterrupted: Outcome
) -> None:
    state_dir, anchor, outcome = anchored
    assert outcome == uninterrupted  # the anchor changes nothing a round does
    heads = [json.loads(line)["payload"] for line in anchor.read_text("utf-8").splitlines()]
    # opened (0), round 0 (1), the approval (still 1), reopened (no new line), rounds 1 and 2
    assert [head["rounds"] for head in heads] == [0, 1, 1, 2, 3]
    assert [head["memory_seq"] for head in heads] == [1, 2, 3, 4, 5]
    head = FileAnchor(anchor).load()
    assert head is not None and head.audit_head == outcome.record_hashes[-1]
    assert head.memory_head == AppendOnlyJournal(state_dir / MEMORY_FILE).head_hash


def test_a_consistent_truncation_is_refused_with_an_anchor(
    anchored: tuple[Path, Path, Outcome], tmp_path: Path
) -> None:
    state_dir, anchor = _copy_anchored(anchored, tmp_path)
    _truncate_to_round_0(state_dir)
    with pytest.raises(LoopStateInconsistent, match="behind its anchor"):
        _open(state_dir, consumed=1, anchor=anchor)
    # without the anchor the same directory opens: the documented limit
    assert len(_open(state_dir, consumed=1).loop.audit.records) == 1


def test_a_deleted_state_directory_is_refused_with_an_anchor(
    anchored: tuple[Path, Path, Outcome], tmp_path: Path
) -> None:
    _, anchor = _copy_anchored(anchored, tmp_path)
    with pytest.raises(LoopStateInconsistent, match="holds 0 recorded round.*behind its"):
        _open(tmp_path / "fresh", anchor=anchor)


def test_a_diverged_history_is_refused_by_the_anchor(
    anchored: tuple[Path, Path, Outcome], restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    """Another run of the same configuration: same audit hashes, other ledger lines."""
    _, anchor = _copy_anchored(anchored, tmp_path)
    other = _copy(restarted[0], tmp_path / "other")
    with pytest.raises(LoopStateInconsistent, match="diverged from its external anchor"):
        _open(other, consumed=ROUNDS, anchor=anchor)


def test_a_lost_or_misplaced_anchor_is_refused(
    anchored: tuple[Path, Path, Outcome], tmp_path: Path
) -> None:
    state_dir, _ = _copy_anchored(anchored, tmp_path)
    with pytest.raises(LoopStateInconsistent, match="anchor holds no head"):
        _open(state_dir, consumed=ROUNDS, anchor=tmp_path / "empty_anchor.jsonl")
    with pytest.raises(ValueError, match="inside the state directory"):
        _open(state_dir, consumed=ROUNDS, anchor=state_dir / "anchor.jsonl")


def test_the_file_anchor_never_moves_back(
    anchored: tuple[Path, Path, Outcome], tmp_path: Path
) -> None:
    _, path = _copy_anchored(anchored, tmp_path)
    anchor = FileAnchor(path)
    head = anchor.load()
    assert head is not None and head.rounds == ROUNDS
    anchor.publish(head)  # the same head again: a no-op
    with pytest.raises(LoopStateInconsistent, match="never moves back"):
        anchor.publish(replace(head, rounds=1))
    for behind in (
        replace(head, memory_seq=head.memory_seq - 1),  # an earlier memory line
        replace(head, memory_head="0" * 64),  # sideways: same line, other content
        replace(head, memory_seq=head.memory_seq + 1, rounds=head.rounds - 1),  # fewer rounds
    ):
        with pytest.raises(LoopStateInconsistent, match="never moves back or sideways"):
            anchor.publish(behind)
    assert anchor.load() == head
    assert len(AppendOnlyJournal(path).entries) == 5  # nothing was appended


# ---------------------------------------------------------------- approvals between rounds


def _between_rounds_lines(state_dir: Path) -> list[Any]:
    return [
        e.payload
        for e in AppendOnlyJournal(state_dir / MEMORY_FILE).entries
        if e.type == BETWEEN_ROUNDS
    ]


def test_a_between_rounds_approval_is_checkpointed_at_once(
    restarted: tuple[Path, Outcome],
) -> None:
    """The legitimate approval of the restart scenario has its own checkpoint line, which names
    the review journal line of the approval; the restart still equals the uninterrupted run
    (``test_a_restarted_loop_ends_exactly_like_an_uninterrupted_one``)."""
    state_dir = restarted[0]
    [line] = _between_rounds_lines(state_dir)
    reviews = AppendOnlyJournal(state_dir / REVIEWS_FILE).entries
    approval = reviews[line["action"]["seq"] - 1]
    assert approval.type == REVIEW_APPROVED and approval.hash == line["action"]["hash"]
    assert (line["action"]["key"], line["action"]["reviewer"]) == (DRAFT, REVIEWER)
    assert line["rounds"] == 1 and line["heads"]["reviews"]["seq"] == approval.seq
    memory = AppendOnlyJournal(state_dir / MEMORY_FILE).entries
    assert [e.type for e in memory[1:]] == [
        "round_memory",
        BETWEEN_ROUNDS,
        "round_memory",
        "round_memory",
    ]


@pytest.fixture
def approved_after_last_round(
    anchored: tuple[Path, Path, Outcome], tmp_path: Path
) -> tuple[Path, Path]:
    """The anchored directory after its three rounds, plus a human approval of ``h_llm_1``."""
    state_dir, anchor = _copy_anchored(anchored, tmp_path)
    before = FileAnchor(anchor).load()
    reopened = _open(state_dir, consumed=ROUNDS, anchor=anchor)
    reopened.memory.reviews.approve("h_llm_1@1.0.0", reviewer=REVIEWER)
    head = FileAnchor(anchor).load()
    assert before is not None and head is not None
    # the anchor moved up at once: one memory line further, same round count
    assert (head.rounds, head.memory_seq) == (before.rounds, before.memory_seq + 1)
    assert head.heads["reviews"]["seq"] == before.heads["reviews"]["seq"] + 1
    return state_dir, anchor


def test_an_approval_after_the_last_round_survives_a_restart(
    approved_after_last_round: tuple[Path, Path],
) -> None:
    state_dir, anchor = approved_after_last_round
    reopened = _open(state_dir, consumed=ROUNDS, anchor=anchor)
    assert [a.key for a in reopened.memory.reviews.approvals] == [DRAFT, "h_llm_1@1.0.0"]
    assert [a.key for a in _open(state_dir, consumed=ROUNDS).memory.reviews.approvals] == [
        DRAFT,
        "h_llm_1@1.0.0",
    ]


def test_dropping_an_approval_is_refused(approved_after_last_round: tuple[Path, Path]) -> None:
    """Its between-rounds checkpoint points past the end of the review journal."""
    state_dir, anchor = approved_after_last_round
    _drop_trailing_lines(state_dir / REVIEWS_FILE, 1)
    with pytest.raises(LoopStateInconsistent, match="reviews .*truncated"):
        _open(state_dir, consumed=ROUNDS)
    with pytest.raises(LoopStateInconsistent, match="reviews .*behind its anchor"):
        _open(state_dir, consumed=ROUNDS, anchor=anchor)


def test_dropping_an_approval_and_its_checkpoint_is_refused_with_an_anchor(
    approved_after_last_round: tuple[Path, Path],
) -> None:
    state_dir, anchor = approved_after_last_round
    before = anchor.read_bytes()
    _drop_trailing_lines(state_dir / REVIEWS_FILE, 1)
    _drop_trailing_lines(state_dir / MEMORY_FILE, 1)
    with pytest.raises(LoopStateInconsistent, match="reviews .*behind its anchor"):
        _open(state_dir, consumed=ROUNDS, anchor=anchor)
    assert anchor.read_bytes() == before  # the anchor never moves back
    # without the anchor it is a valid shorter history: "not approved yet" (documented limit)
    reopened = _open(state_dir, consumed=ROUNDS)
    assert "h_llm_1@1.0.0" in reopened.memory.reviews.pending


def test_a_forged_approval_without_a_checkpoint_is_refused(
    restarted: tuple[Path, Outcome], anchored: tuple[Path, Path, Outcome], tmp_path: Path
) -> None:
    """An approval appended to the review journal past the queue of an opened state directory
    (a valid chain, a human identity, the right draft hashes) names no between-rounds checkpoint."""
    state_dir = _copy(restarted[0], tmp_path)
    ReviewQueue(state_dir / REVIEWS_FILE).approve("h_llm_1@1.0.0", reviewer="mallory")
    with pytest.raises(LoopStateInconsistent, match="no between-rounds checkpoint names"):
        _open(state_dir, consumed=ROUNDS)
    (tmp_path / "anchored").mkdir()
    state_dir, anchor = _copy_anchored(anchored, tmp_path / "anchored")
    ReviewQueue(state_dir / REVIEWS_FILE).approve("h_llm_1@1.0.0", reviewer="mallory")
    with pytest.raises(LoopStateInconsistent, match="no between-rounds checkpoint names"):
        _open(state_dir, consumed=ROUNDS, anchor=anchor)


def test_a_forged_approval_inside_an_earlier_round_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    """The between-rounds line removed and the approval kept (both files re-chained so every
    position still matches): an approval inside a round's range that no checkpoint names."""
    state_dir = _copy(restarted[0], tmp_path)
    path = state_dir / MEMORY_FILE
    entries = AppendOnlyJournal(path).entries
    path.unlink()
    rewritten = AppendOnlyJournal(path)
    for entry in entries:
        if entry.type != BETWEEN_ROUNDS:
            rewritten.append(entry.type, json.loads(json.dumps(entry.payload)))
    with pytest.raises(LoopStateInconsistent, match="no between-rounds checkpoint names"):
        _open(state_dir, consumed=ROUNDS)


def test_a_checkpoint_naming_another_line_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir = _copy(restarted[0], tmp_path)

    def edit(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == BETWEEN_ROUNDS:
            payload["action"]["reviewer"] = "mallory"
        return payload

    _rechain(state_dir / MEMORY_FILE, edit)
    with pytest.raises(LoopStateInconsistent, match="exactly the one human approval"):
        _open(state_dir, consumed=ROUNDS)


def test_an_approval_while_a_round_runs_is_refused(
    restarted: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir = _copy(restarted[0], tmp_path)
    reopened = _open(state_dir, consumed=ROUNDS)
    before = {name: (state_dir / name).read_bytes() for name in ALL_FILES}
    reopened.loop.audit.begin_round("synthetic_loop", ROUNDS)  # a round is running
    before[AUDIT_FILE] = (state_dir / AUDIT_FILE).read_bytes()
    with pytest.raises(ValueError, match="only be approved between rounds"):
        reopened.memory.reviews.approve("h_llm_1@1.0.0", reviewer=REVIEWER)
    assert {name: (state_dir / name).read_bytes() for name in ALL_FILES} == before
    assert reopened.memory.reviews.pending == ("h_llm_1@1.0.0", "h_llm_2@1.0.0")


def test_a_failed_between_rounds_checkpoint_stops_the_loop(
    restarted: tuple[Path, Outcome], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval is journaled but its checkpoint fails: the next round is not recorded (the
    loop stops) and the directory is refused on reopening — never silently accepted."""
    state_dir = _copy(restarted[0], tmp_path)
    reopened = _open(state_dir, consumed=ROUNDS)

    def fail(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(MemoryCheckpoint, "between_rounds", fail)
    with pytest.raises(OSError, match="disk full"):
        reopened.memory.reviews.approve("h_llm_1@1.0.0", reviewer=REVIEWER)
    monkeypatch.undo()
    with pytest.raises(RuntimeError, match="no between-rounds checkpoint names"):
        reopened.loop.run_unattended(1)
    assert len(reopened.loop.audit.records) == ROUNDS  # the round was not recorded
    with pytest.raises(LoopStateInconsistent):  # interrupted round, uncheckpointed approval
        _open(state_dir, consumed=ROUNDS)


# ------------------------------------------------------ durable bus (ADR-0044 file-backed note)


def test_a_file_bus_under_the_state_directory_survives_the_restart_unchanged(
    uninterrupted: Outcome, tmp_path: Path
) -> None:
    """The caller injects ``FileEventBus(state_dir / "bus")`` (no composition change): the rounds
    are exactly the in-memory-bus rounds, and the bus replays every round's record hash."""
    state_dir = tmp_path / "state"

    def open_with_file_bus(bus: FileEventBus, consumed: int) -> Any:
        return open_synthetic_loop(
            _config(),
            state_dir=state_dir,
            provider=RandomWalkMarket(),
            bus=bus,
            llm=_llm(consumed),
        )

    with FileEventBus(state_dir / "bus") as bus:
        first = open_with_file_bus(bus, 0)
        first.loop.run_unattended(1)
        first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    del first
    gc.collect()
    with FileEventBus(state_dir / "bus") as bus:
        second = open_with_file_bus(bus, 1)
        second.loop.run_unattended(ROUNDS - 1)
        outcome = _outcome(second.loop, second.memory)
    assert outcome == uninterrupted  # the bus is outside every hashed record
    with FileEventBus(state_dir / "bus") as bus:
        rounds = bus.poll("auditor", ROUND_TOPIC, 100)
    assert [m.payload["record_hash"] for m in rounds] == outcome.record_hashes


# ---------------------- automatic bus (ADR-0044 / ADR-0049 notes, durable jobs and bus wiring)

ROUND_LOG = Path(BUS_DIR) / "topics" / f"{ROUND_TOPIC}.jsonl"


class _Crash(BaseException):
    """A process death: neither the job runner nor the loop catches it."""


def _open_auto(state_dir: Path, *, consumed: int = 0) -> DurableLoop:
    """Durable mode without a caller's bus: the composition opens ``state_dir / "bus"``."""
    opened: DurableLoop = open_synthetic_loop(
        _config(), state_dir=state_dir, provider=RandomWalkMarket(), llm=_llm(consumed)
    )
    return opened


def _round_hashes(state_dir: Path) -> list[str]:
    with FileEventBus(state_dir / BUS_DIR) as bus:
        return [m.payload["record_hash"] for m in bus.poll("auditor", ROUND_TOPIC, 100)]


@pytest.fixture(scope="module")
def auto_bus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Outcome]:
    """``restarted`` with the automatic bus: round 0, the approval, the first loop dropped without
    ``close()`` (dropping the loop releases the bus lock), reopened, rounds 1-2."""
    state_dir = tmp_path_factory.mktemp("auto_bus") / "state"
    first = _open_auto(state_dir)
    assert first.owned_bus is not None and first.bus is first.owned_bus
    first.loop.run_unattended(1)
    first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    del first
    gc.collect()
    with _open_auto(state_dir, consumed=1) as second:
        second.loop.run_unattended(ROUNDS - 1)
        outcome = _outcome(second.loop, second.memory)
    return state_dir, outcome


def test_a_restart_with_the_automatic_bus_equals_the_uninterrupted_run(
    auto_bus: tuple[Path, Outcome], uninterrupted: Outcome
) -> None:
    state_dir, outcome = auto_bus
    assert outcome == uninterrupted
    assert (state_dir / BUS_DIR / "bus.json").is_file()
    assert _round_hashes(state_dir) == outcome.record_hashes
    with FileEventBus(state_dir / BUS_DIR) as bus:  # every round job was acknowledged
        assert bus.poll("research_loop_worker", JOB_TOPIC, 100) == ()
    with _open_auto(state_dir, consumed=ROUNDS) as reopened:  # consistent: opens, adds nothing
        assert len(reopened.loop.audit.records) == ROUNDS
    assert _round_hashes(state_dir) == outcome.record_hashes


def test_a_crash_between_the_audit_and_the_bus_is_caught_up_on_reopening(
    uninterrupted: Outcome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process dies after the audit recorded round 1 and before the bus saw it (and before its
    job was acknowledged): reopening publishes round 1 from the audit, settles its job from the
    audit, and the run continues exactly like the uninterrupted one."""
    state_dir = tmp_path / "state"
    first = _open_auto(state_dir)
    first.loop.run_unattended(1)
    first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    real_publish = FileEventBus.publish

    def die_on_round_one(bus: FileEventBus, message: Any) -> None:
        if message.topic == ROUND_TOPIC and message.key.endswith(":1"):
            raise _Crash
        real_publish(bus, message)

    monkeypatch.setattr(FileEventBus, "publish", die_on_round_one)
    with pytest.raises(_Crash):
        first.loop.run_unattended(1)
    monkeypatch.undo()
    first.close()  # the process is gone: the kernel drops its lock
    assert len(LoopAuditLog(state_dir / AUDIT_FILE).records) == 2
    assert len(_round_hashes(state_dir)) == 1  # the bus is one round behind the audit
    with FileEventBus(state_dir / BUS_DIR) as bus:
        assert len(bus.poll("research_loop_worker", JOB_TOPIC, 100)) == 1  # round 1's job

    with _open_auto(state_dir, consumed=2) as second:
        second.loop.run_unattended(ROUNDS - 2)
        outcome = _outcome(second.loop, second.memory)
    assert outcome == uninterrupted
    assert _round_hashes(state_dir) == outcome.record_hashes
    with FileEventBus(state_dir / BUS_DIR) as bus:
        assert bus.poll("research_loop_worker", JOB_TOPIC, 100) == ()


def test_a_bus_one_round_short_is_caught_up_and_two_rounds_short_is_refused(
    auto_bus: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir, outcome = auto_bus
    one_short = _copy(state_dir, tmp_path / "one")
    _drop_trailing_lines(one_short / ROUND_LOG, 1)
    with _open_auto(one_short, consumed=ROUNDS):
        pass
    assert _round_hashes(one_short) == outcome.record_hashes  # caught up from the audit

    two_short = _copy(state_dir, tmp_path / "two")
    _drop_trailing_lines(two_short / ROUND_LOG, 2)
    before = (two_short / ROUND_LOG).read_bytes()
    with pytest.raises(LoopStateInconsistent, match="2 rounds behind"):
        _open_auto(two_short, consumed=ROUNDS)
    assert (two_short / ROUND_LOG).read_bytes() == before  # nothing written on a refusal
    FileEventBus(two_short / BUS_DIR).close()  # the refused open released the lock


def test_a_bus_with_a_foreign_record_or_ahead_of_the_audit_is_refused(
    auto_bus: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir, outcome = auto_bus
    with FileEventBus(state_dir / BUS_DIR) as bus:
        real = bus.poll("auditor", ROUND_TOPIC, 100)
    loop_id = _config().loop_id

    foreign = _copy(state_dir, tmp_path / "foreign")
    shutil.rmtree(foreign / BUS_DIR)
    with FileEventBus(foreign / BUS_DIR) as bus:  # a valid bus holding another round 1
        bus.publish(real[0])
        bus.publish(
            BusMessage.build(
                ROUND_TOPIC,
                f"{loop_id}:1",
                {
                    "record_hash": "f" * 64,
                    "record": real[1].model_dump(mode="json")["payload"]["record"],
                },
            )
        )
        bus.publish(real[2])
    with pytest.raises(LoopStateInconsistent, match="foreign or reordered"):
        _open_auto(foreign, consumed=ROUNDS)

    reordered = _copy(state_dir, tmp_path / "reordered")
    shutil.rmtree(reordered / BUS_DIR)
    with FileEventBus(reordered / BUS_DIR) as bus:
        for message in (real[1], real[0], real[2]):
            bus.publish(message)
    with pytest.raises(LoopStateInconsistent, match="foreign or reordered"):
        _open_auto(reordered, consumed=ROUNDS)

    ahead = _copy(state_dir, tmp_path / "ahead")
    with FileEventBus(ahead / BUS_DIR) as bus:  # a round the audit never recorded
        bus.publish(BusMessage.build(ROUND_TOPIC, f"{loop_id}:3", {"record_hash": "e" * 64}))
    with pytest.raises(LoopStateInconsistent, match="ahead of the audit"):
        _open_auto(ahead, consumed=ROUNDS)

    missing = _copy(state_dir, tmp_path / "missing")
    shutil.rmtree(missing / BUS_DIR)  # a directory whose bus was lost (or never used)
    with pytest.raises(LoopStateInconsistent, match="3 rounds behind"):
        _open_auto(missing, consumed=ROUNDS)


def test_an_in_memory_loop_still_needs_a_bus(tmp_path: Path) -> None:
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    with pytest.raises(ValueError, match="needs a bus"):
        build_synthetic_loop(_config(), provider=RandomWalkMarket(), memory=memory)


def test_check_round_bus_is_exact_on_an_empty_audit() -> None:
    bus = InMemoryEventBus()
    assert check_round_bus(bus, "synthetic_loop", ()) == 0
    bus.publish(BusMessage.build(ROUND_TOPIC, "synthetic_loop:0", {"record_hash": "a" * 64}))
    with pytest.raises(LoopStateInconsistent, match="ahead"):
        check_round_bus(bus, "synthetic_loop", ())


# ------------------ injected buses (ADR-0049 implementation note, review fixes 3, 2026-09-26)


def _open_injected(state_dir: Path, bus: Any, *, consumed: int = ROUNDS) -> DurableLoop:
    """Durable mode with a caller's bus: cross-checked like the automatic one, left open."""
    opened: DurableLoop = open_synthetic_loop(
        _config(), state_dir=state_dir, provider=RandomWalkMarket(), bus=bus, llm=_llm(consumed)
    )
    assert opened.owned_bus is None and opened.bus is bus
    return opened


def _external_bus(auto_bus: tuple[Path, Outcome], tmp_path: Path) -> tuple[Path, Path]:
    """A copy of the state directory and, outside it, a copy of its bus (the caller's bus)."""
    state_dir = _copy(auto_bus[0], tmp_path)
    external = tmp_path / "external_bus"
    shutil.copytree(state_dir / BUS_DIR, external)
    return state_dir, external


def test_an_injected_file_bus_with_a_foreign_round_record_is_refused(
    auto_bus: tuple[Path, Outcome], tmp_path: Path
) -> None:
    state_dir, external = _external_bus(auto_bus, tmp_path)
    with FileEventBus(external) as bus:
        real = bus.poll("auditor", ROUND_TOPIC, 100)
    shutil.rmtree(external)
    round_log = external / "topics" / f"{ROUND_TOPIC}.jsonl"
    with FileEventBus(external) as bus:  # a valid bus whose round 1 is another record
        bus.publish(real[0])
        forged = real[1].model_dump(mode="json")["payload"]
        bus.publish(BusMessage.build(ROUND_TOPIC, real[1].key, {**forged, "record_hash": "f" * 64}))
        bus.publish(real[2])
        before = round_log.read_bytes()
        with pytest.raises(LoopStateInconsistent, match="foreign or reordered"):
            _open_injected(state_dir, bus)
        assert len(bus.poll("auditor", ROUND_TOPIC, 100)) == 3  # the caller's bus stays open
    assert round_log.read_bytes() == before  # nothing published on a refusal


def test_an_injected_file_bus_one_round_behind_is_caught_up_and_two_behind_refused(
    auto_bus: tuple[Path, Outcome], tmp_path: Path
) -> None:
    outcome = auto_bus[1]
    state_dir, external = _external_bus(auto_bus, tmp_path / "one")
    _drop_trailing_lines(external / "topics" / f"{ROUND_TOPIC}.jsonl", 1)
    with FileEventBus(external) as bus:
        _open_injected(state_dir, bus)
        hashes = [m.payload["record_hash"] for m in bus.poll("auditor", ROUND_TOPIC, 100)]
    assert hashes == outcome.record_hashes  # caught up from the audit

    state_dir, external = _external_bus(auto_bus, tmp_path / "two")
    _drop_trailing_lines(external / "topics" / f"{ROUND_TOPIC}.jsonl", 2)
    with FileEventBus(external) as bus, pytest.raises(LoopStateInconsistent, match="2 rounds"):
        _open_injected(state_dir, bus)


def test_an_injected_in_memory_bus_is_not_durable_and_gets_every_round_replayed(
    auto_bus: tuple[Path, Outcome], tmp_path: Path
) -> None:
    """An ``InMemoryEventBus`` starts empty in every process: the audit's rounds are replayed into
    it, in order, so it holds exactly the audit's rounds; a foreign message is still refused."""
    outcome = auto_bus[1]
    state_dir = _copy(auto_bus[0], tmp_path / "memory")
    bus = InMemoryEventBus()
    _open_injected(state_dir, bus)
    replayed = [m.payload["record_hash"] for m in bus.poll("auditor", ROUND_TOPIC, 100)]
    assert replayed == outcome.record_hashes
    _open_injected(state_dir, bus)  # consistent now: a second open adds nothing
    assert len(bus.poll("auditor", ROUND_TOPIC, 100)) == ROUNDS

    foreign = InMemoryEventBus()
    loop_id = _config().loop_id
    foreign.publish(BusMessage.build(ROUND_TOPIC, f"{loop_id}:0", {"record_hash": "f" * 64}))
    with pytest.raises(LoopStateInconsistent, match="foreign or reordered"):
        _open_injected(state_dir, foreign)
    assert len(foreign.poll("auditor", ROUND_TOPIC, 100)) == 1  # nothing replayed on a refusal
