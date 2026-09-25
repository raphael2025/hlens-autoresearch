"""ADR-0049 smoke test: unattended rounds of the research loop on a synthetic market.

TEST ONLY: every budget, cost unit and model parameter here is an arbitrary, uncalibrated number
chosen to keep the test small; the Validation Profile is the Phase 4 ``TEST_ONLY_PROFILE``.
Synthetic results support no claim about real markets (roadmap Phase 9).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.worker import LoopBudget, ResearchLoop, RoundStatus, StageStatus
from apps.worker.loop import ROUND_TOPIC, STAGE_ORDER, STAGE_TOPIC
from core.contracts.synthetic import PlantedEffect, SyntheticMarketSpec
from core.domain.research import EvidenceLevel, HypothesisOrigin, KnowledgeItem
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import ResearchMemory, SyntheticLoopConfig, build_synthetic_loop
from research.strategies.failure_registry import FailureRegistry
from tests.research.validation.fixtures import TEST_ONLY_PROFILE

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
FAMILY = "loop_autocorrelation"

#: TEST ONLY budget.
TEST_ONLY_BUDGET = LoopBudget(
    max_trials_per_round=3,
    max_trials_total=10,
    max_llm_cost_units=Decimal(10),
    max_compute_seconds=Decimal(100),
)


def _knowledge(lag: int) -> KnowledgeItem:
    return KnowledgeItem(
        name=f"k_autocorr_lag_{lag}",
        version="1.0.0",
        created_at=EPOCH,
        source="test://synthetic",
        license="test-only",
        claim=f"minute returns carry sign information at lag {lag}",
        conditions=(f"lag_minutes = {lag}",),
        evidence_level=EvidenceLevel.E0_ANECDOTE,
    )


def _llm_output(index: int, lag: int | None) -> dict[str, Any]:
    return {
        "name": f"h_llm_{index}",
        "statement": f"LLM suggestion {index}",
        "expected_direction": "higher",
        "minimum_meaningful_effect": "declared before running",
        "conditions": [] if lag is None else [f"lag_minutes = {lag}"],
    }


def _config(budget: LoopBudget = TEST_ONLY_BUDGET, seed: int = 11) -> SyntheticLoopConfig:
    return SyntheticLoopConfig(
        loop_id="synthetic_loop",
        seed=seed,
        epoch=EPOCH,
        cadence=timedelta(hours=8),
        budget=budget,
        market=SyntheticMarketSpec(
            name="loop_market",
            version="1.0.0",
            symbol="SYN-USDT",
            start=EPOCH,
            minutes=1,
            seed=0,
            initial_price=Decimal(100),
            volatility=Decimal("0.001"),
            effects=(PlantedEffect(lag_minutes=1, strength=Decimal("0.3")),),
        ),
        minutes_per_round=400,
        compute_seconds_per_bar=Decimal("0.01"),
        state_window=60,
        trend_threshold=Decimal("0.3"),
        family_id=FAMILY,
        knowledge=(_knowledge(1), _knowledge(2), _knowledge(3)),
        max_new_hypotheses_per_round=1,
        hypothesis_compute_seconds=Decimal("0.5"),
        compute_seconds_per_trial=Decimal(1),
        validation_compute_seconds=Decimal("0.5"),
        state_compute_seconds=Decimal("0.5"),
        profile=TEST_ONLY_PROFILE,
        constitution_version="1.0.0",
        llm_prompt="propose one falsifiable hypothesis about minute returns",
        llm_cost_units_per_call=Decimal(1),
    )


def _build(
    tmp: Path,
    *,
    config: SyntheticLoopConfig | None = None,
    llm_lags: tuple[int | None, ...] = (1, 2, None),
) -> tuple[ResearchLoop, ResearchMemory, InMemoryEventBus]:
    bus = InMemoryEventBus()
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    llm = ScriptedLLMProvider(
        [_llm_output(i, lag) for i, lag in enumerate(llm_lags)], clock=lambda: EPOCH
    )
    loop = build_synthetic_loop(
        config or _config(), provider=RandomWalkMarket(), bus=bus, memory=memory, llm=llm
    )
    return loop, memory, bus


def _states(loop: ResearchLoop) -> set[LifecycleState]:
    return {t.to_state for h in loop.guard.histories for t in h.transitions}


def test_three_unattended_rounds_produce_an_audit_trail(tmp_path: Path) -> None:
    loop, memory, bus = _build(tmp_path)
    records = loop.run_unattended(3)
    assert [r.status for r in records] == [RoundStatus.COMPLETED] * 3
    assert loop.audit.verify()
    # every stage of every round completed and was published; every round was published
    assert all(s.status is StageStatus.COMPLETED for r in records for s in r.stages)
    assert len(bus.poll("audit", STAGE_TOPIC, 100)) == 3 * len(STAGE_ORDER)
    assert len(bus.poll("audit", ROUND_TOPIC, 100)) == 3
    # one knowledge hypothesis per round was pre-registered and counted as a trial
    assert memory.ledger.trials(FAMILY) == 3 and loop.total_usage.trials == 3
    assert {h.origin for h in memory.ledger.hypotheses} == {HypothesisOrigin.KNOWLEDGE}
    # LLM drafts were only queued for human review, never registered by the loop
    assert memory.reviews.pending == ("h_llm_0@1.0.0", "h_llm_1@1.0.0", "h_llm_2@1.0.0")
    assert loop.total_usage.llm_cost_units == Decimal(3)
    # outputs (failures included) are recorded
    assert len(memory.experiments) == 3
    verdicts = [
        row["verdict"]
        for r in records
        for s in r.stages
        if s.name == "validation" and s.summary is not None
        for row in s.summary["reports"]
    ]
    # with this seed the planted lag-1 effect passes the screening, lags 2 and 3 are rejected
    assert verdicts == ["PASS", "FAIL", "FAIL"]
    rejected = [r for r in memory.failures.records() if r.terminal_state == "REJECTED"]
    assert [r.gate_id for r in rejected] == ["G3.adjusted_p_value"] * 2
    # nothing ever reaches OOS or beyond
    assert _states(loop) <= {
        LifecycleState.CANDIDATE,
        LifecycleState.VALIDATION,
        LifecycleState.REJECTED,
        LifecycleState.FAILED,
    }
    assert LifecycleState.ACTIVE not in _states(loop)


def test_same_seed_gives_identical_audit_hashes(tmp_path: Path) -> None:
    first, _, _ = _build(tmp_path / "a")
    second, _, _ = _build(tmp_path / "b")
    hashes = [r.record_hash for r in first.run_unattended(3)]
    assert hashes == [r.record_hash for r in second.run_unattended(3)]
    other, _, _ = _build(tmp_path / "c", config=_config(seed=12))
    assert [r.record_hash for r in other.run_unattended(3)] != hashes


def test_budget_exhaustion_stops_the_loop(tmp_path: Path) -> None:
    budget = LoopBudget(
        max_trials_per_round=3,
        max_trials_total=2,
        max_llm_cost_units=Decimal(10),
        max_compute_seconds=Decimal(100),
    )
    loop, memory, _ = _build(tmp_path, config=_config(budget=budget))
    records = loop.run_unattended(5)
    assert [r.status for r in records] == [
        RoundStatus.COMPLETED,
        RoundStatus.COMPLETED,
        RoundStatus.BUDGET_EXHAUSTED,
    ]
    refused = {s.name: s for s in records[-1].stages}
    assert refused["hypothesis"].status is StageStatus.REFUSED_BUDGET
    assert refused["experiment"].status is StageStatus.SKIPPED
    assert memory.ledger.trials(FAMILY) == 2 and loop.halted is RoundStatus.BUDGET_EXHAUSTED


def test_a_human_reviewed_llm_draft_is_registered_in_a_later_round(tmp_path: Path) -> None:
    loop, memory, _ = _build(tmp_path)
    loop.run_unattended(1)
    memory.reviews.approve("h_llm_0@1.0.0", reviewer="test-human")  # outside the loop
    [record] = loop.run_unattended(1)
    hypothesis = {s.name: s for s in record.stages}["hypothesis"]
    assert hypothesis.summary is not None
    assert "hypothesis:h_llm_0@1.0.0" in hypothesis.summary["registered"]
    assert {h.origin for h in memory.ledger.hypotheses} == {
        HypothesisOrigin.KNOWLEDGE,
        HypothesisOrigin.LLM,
    }
    evidence = [e for t in record.transitions for e in t.evidence]
    assert "human_review:test-human" in evidence


def test_an_unrunnable_hypothesis_is_failed_and_recorded(tmp_path: Path) -> None:
    loop, memory, _ = _build(tmp_path, llm_lags=(None, None))
    loop.run_unattended(1)
    memory.reviews.approve("h_llm_0@1.0.0", reviewer="test-human")
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    failed = [r for r in memory.failures.records() if r.terminal_state == "FAILED"]
    assert [str(r.subject_ref) for r in failed] == ["hypothesis:h_llm_0@1.0.0"]
    assert LifecycleState.FAILED in _states(loop)


def test_the_loop_never_writes_approved_transitions(tmp_path: Path) -> None:
    loop, _, _ = _build(tmp_path)
    loop.run_unattended(3)
    assert all(t.approved_by is None for h in loop.guard.histories for t in h.transitions)
