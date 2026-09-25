"""ADR-0049: the generic research-loop mechanism in apps/worker (fake stages, no research/ import).

Budgets and thresholds below are TEST ONLY numbers chosen for readability, not proposals.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.worker import (
    AUTOMATABLE_TARGETS,
    STAGE_ORDER,
    AutomationForbidden,
    DegradationMonitor,
    LifecycleGuard,
    LoopBudget,
    LoopHalted,
    ResearchLoop,
    RoundContext,
    RoundStatus,
    StageFailed,
    StageResult,
    StageStatus,
    StageUsage,
)
from apps.worker.degradation import DEGRADATION_TOPIC
from apps.worker.loop import (
    EXTENDED_STAGE_ORDER,
    FORBIDDEN_TARGETS,
    ROUND_TOPIC,
    STAGE_TOPIC,
    automation_reachable_states,
)
from core.contracts.validation_profile import LifecycleParams
from core.domain.base import FrozenMapping, Kind, Ref
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from tests import factories

REPO = Path(__file__).resolve().parents[2]
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SUBJECT = Ref(kind=Kind.HYPOTHESIS, name="h_fake", version="1.0.0")

#: TEST ONLY budget: generous except where a test narrows it.
TEST_ONLY_BUDGET = LoopBudget(
    max_trials_per_round=5,
    max_trials_total=100,
    max_llm_cost_units=Decimal(100),
    max_compute_seconds=Decimal(1000),
)


class FakeStage:
    def __init__(
        self,
        name: str,
        usage: StageUsage | None = None,
        *,
        actual: StageUsage | None = None,
        action: Callable[[RoundContext], None] | None = None,
    ) -> None:
        self.name = name
        self._usage = usage or StageUsage()
        self._actual = actual or self._usage
        self._action = action
        self.runs = 0

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return self._usage

    def run(self, ctx: RoundContext) -> StageResult:
        self.runs += 1
        if self._action is not None:
            self._action(ctx)
        return StageResult({"stage": self.name, "seed": ctx.seed}, self._actual)


def _stages(**overrides: FakeStage) -> list[FakeStage]:
    return [overrides.get(name, FakeStage(name)) for name in STAGE_ORDER]


def _loop(
    stages: list[FakeStage], budget: LoopBudget = TEST_ONLY_BUDGET, seed: int = 7
) -> tuple[ResearchLoop, InMemoryEventBus]:
    bus = InMemoryEventBus()
    loop = ResearchLoop(
        loop_id="fake_loop",
        stages=stages,
        budget=budget,
        bus=bus,
        seed=seed,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
    )
    return loop, bus


def _admit(ctx: RoundContext) -> None:
    ctx.open_subject(SUBJECT)
    ctx.advance(SUBJECT, LifecycleState.CANDIDATE, reason="test", evidence=("e",))


def test_stages_must_follow_the_fixed_order() -> None:
    stages = _stages()
    stages[0], stages[1] = stages[1], stages[0]
    with pytest.raises(ValueError, match="exactly"):
        _loop(stages)


def test_rounds_are_recorded_chained_published_and_deterministic() -> None:
    loop, bus = _loop(_stages(hypothesis=FakeStage("hypothesis", StageUsage(trials=1))))
    records = loop.run_unattended(3)
    assert [r.status for r in records] == [RoundStatus.COMPLETED] * 3
    assert loop.audit.verify() and records[1].previous_hash == records[0].record_hash
    assert records[2].as_of == EPOCH + 2 * timedelta(hours=1)
    assert loop.total_usage.trials == 3
    assert len(bus.poll("audit", STAGE_TOPIC, 100)) == 3 * len(STAGE_ORDER)
    assert len(bus.poll("audit", ROUND_TOPIC, 100)) == 3
    again, _ = _loop(_stages(hypothesis=FakeStage("hypothesis", StageUsage(trials=1))))
    assert [r.record_hash for r in again.run_unattended(3)] == [r.record_hash for r in records]
    other, _ = _loop(_stages(), seed=8)
    assert other.run_unattended(1)[0].seed != records[0].seed


def test_a_duplicate_round_job_runs_once() -> None:
    stage = FakeStage("ingest")
    loop, _ = _loop(_stages(ingest=stage))
    loop.submit_round(0)
    loop.submit_round(0)
    assert len(loop.run_pending()) == 1 and stage.runs == 1
    assert len(loop.audit.records) == 1


def test_budget_exhaustion_stops_the_round_and_halts_the_loop() -> None:
    budget = LoopBudget(
        max_trials_per_round=5,
        max_trials_total=2,
        max_llm_cost_units=Decimal(0),
        max_compute_seconds=Decimal(1000),
    )
    experiment = FakeStage("experiment")
    loop, _ = _loop(
        _stages(hypothesis=FakeStage("hypothesis", StageUsage(trials=1)), experiment=experiment),
        budget,
    )
    records = loop.run_unattended(5)
    assert [r.status for r in records] == [
        RoundStatus.COMPLETED,
        RoundStatus.COMPLETED,
        RoundStatus.BUDGET_EXHAUSTED,
    ]
    last = records[-1]
    by_name = {s.name: s for s in last.stages}
    assert by_name["hypothesis"].status is StageStatus.REFUSED_BUDGET
    assert by_name["hypothesis"].refused == ("max_trials_total",)
    assert by_name["experiment"].status is StageStatus.SKIPPED and experiment.runs == 2
    assert loop.total_usage.trials == 2 and loop.halted is RoundStatus.BUDGET_EXHAUSTED
    with pytest.raises(LoopHalted):
        loop.submit_round(3)
    assert loop.budget.max_trials_total == 2  # never expanded


def test_llm_cost_and_per_round_limits_are_enforced_before_the_stage() -> None:
    budget = LoopBudget(
        max_trials_per_round=1,
        max_trials_total=10,
        max_llm_cost_units=Decimal(1),
        max_compute_seconds=Decimal(10),
    )
    greedy = FakeStage("hypothesis", StageUsage(trials=2, llm_cost_units=Decimal(2)))
    loop, _ = _loop(_stages(hypothesis=greedy), budget)
    [record] = loop.run_unattended(3)
    refused = {s.name: s for s in record.stages}["hypothesis"]
    assert set(refused.refused) == {"max_trials_per_round", "max_llm_cost_units"}
    assert greedy.runs == 0 and loop.total_usage == StageUsage()


def test_an_under_declared_stage_is_an_overrun_and_halts() -> None:
    sneaky = FakeStage(
        "experiment",
        StageUsage(trials=1, compute_seconds=Decimal(2)),
        actual=StageUsage(trials=3, compute_seconds=Decimal(1)),
    )
    loop, _ = _loop(_stages(experiment=sneaky))
    [record] = loop.run_unattended(2)
    assert record.status is RoundStatus.BUDGET_OVERRUN and loop.total_usage.trials == 3
    assert loop.halted is RoundStatus.BUDGET_OVERRUN
    stage = {s.name: s for s in record.stages}["experiment"]
    assert stage.status is StageStatus.BUDGET_OVERRUN
    assert stage.overrun == StageUsage(trials=2)  # actual minus declared, per dimension
    assert record.overrun == {"stage": "experiment", "amount": StageUsage(trials=2).payload()}
    assert record.payload()["overrun"] == record.overrun
    with pytest.raises(LoopHalted):
        loop.submit_round(1)


def test_a_failed_stage_is_charged_what_it_reports_else_its_estimate() -> None:
    declared = StageUsage(trials=2, compute_seconds=Decimal(5))

    def partial(ctx: RoundContext) -> None:
        raise StageFailed("died half way", usage=StageUsage(trials=1, compute_seconds=Decimal(2)))

    def boom(ctx: RoundContext) -> None:
        raise RuntimeError("no usage report")

    reported, _ = _loop(_stages(experiment=FakeStage("experiment", declared, action=partial)))
    [record] = reported.run_unattended(1)
    stage = {s.name: s for s in record.stages}["experiment"]
    assert (record.status, stage.status) == (RoundStatus.FAILED, StageStatus.FAILED)
    assert stage.usage == StageUsage(trials=1, compute_seconds=Decimal(2)) == record.round_usage
    assert stage.overrun is None and record.overrun is None and reported.halted is None
    unreported, _ = _loop(_stages(experiment=FakeStage("experiment", declared, action=boom)))
    [record] = unreported.run_unattended(1)
    assert record.round_usage == declared and "no usage report" in (
        {s.name: s for s in record.stages}["experiment"].error or ""
    )


def test_a_failed_stage_that_spent_more_than_declared_halts() -> None:
    def greedy(ctx: RoundContext) -> None:
        raise StageFailed("over and out", usage=StageUsage(compute_seconds=Decimal(9)))

    stage = FakeStage("validation", StageUsage(compute_seconds=Decimal(4)), action=greedy)
    loop, _ = _loop(_stages(validation=stage))
    [record] = loop.run_unattended(3)
    failed = {s.name: s for s in record.stages}["validation"]
    assert failed.status is StageStatus.BUDGET_OVERRUN and "over and out" in (failed.error or "")
    assert failed.overrun == StageUsage(compute_seconds=Decimal(5))
    assert record.status is RoundStatus.BUDGET_OVERRUN and loop.halted is RoundStatus.BUDGET_OVERRUN
    assert loop.total_usage.compute_seconds == Decimal(9)


def test_the_optional_evolution_stage_has_exactly_one_place() -> None:
    stages = _stages()
    stages.insert(3, FakeStage("evolution"))
    loop, _ = _loop(stages)
    [record] = loop.run_unattended(1)
    assert [s.name for s in record.stages] == list(EXTENDED_STAGE_ORDER)
    misplaced = _stages()
    misplaced.insert(5, FakeStage("evolution"))
    with pytest.raises(ValueError, match="exactly"):
        _loop(misplaced)
    with pytest.raises(ValueError, match="exactly"):
        _loop([*_stages(), FakeStage("reporting")])


def test_a_failing_stage_is_recorded_and_the_loop_continues() -> None:
    def boom(ctx: RoundContext) -> None:
        if ctx.round_index == 0:
            raise RuntimeError("provider down")

    loop, _ = _loop(_stages(state=FakeStage("state", action=boom)))
    records = loop.run_unattended(2)
    assert [r.status for r in records] == [RoundStatus.FAILED, RoundStatus.COMPLETED]
    failed = {s.name: s for s in records[0].stages}["state"]
    assert failed.status is StageStatus.FAILED and "provider down" in (failed.error or "")


@pytest.mark.parametrize("target", sorted(FORBIDDEN_TARGETS))
def test_the_guard_never_moves_anything_to_paper_or_beyond(target: LifecycleState) -> None:
    def promote(ctx: RoundContext) -> None:
        _admit(ctx)
        for step in (LifecycleState.VALIDATION, LifecycleState.OOS):  # as far as automation goes
            ctx.advance(SUBJECT, step, reason="validated", evidence=("e",))
        ctx.advance(SUBJECT, target, reason="sneak", evidence=("e",))

    loop, _ = _loop(_stages(memory=FakeStage("memory", action=promote)))
    [record] = loop.run_unattended(2)
    assert record.status is RoundStatus.GUARD_VIOLATION and loop.halted is not None
    assert loop.guard.state_of(SUBJECT) is LifecycleState.OOS
    assert all(t.to_state not in FORBIDDEN_TARGETS for t in record.transitions)
    assert all(t.approved_by is None for t in record.transitions)


def test_the_guard_refuses_subjects_it_does_not_own_and_human_gates() -> None:
    guard = LifecycleGuard(actor="loop")
    with pytest.raises(AutomationForbidden, match="not opened"):
        guard.advance(
            SUBJECT, LifecycleState.CANDIDATE, reason="r", evidence=("e",), occurred_at=EPOCH
        )
    guard.open(SUBJECT)
    for state in (LifecycleState.CANDIDATE, LifecycleState.VALIDATION, LifecycleState.OOS):
        transition = guard.advance(SUBJECT, state, reason="r", evidence=("e",), occurred_at=EPOCH)
        assert transition.approved_by is None
    with pytest.raises(AutomationForbidden):
        guard.advance(SUBJECT, LifecycleState.PAPER, reason="r", evidence=("e",), occurred_at=EPOCH)


def test_automation_cannot_reach_any_human_or_production_state() -> None:
    reachable = automation_reachable_states()
    assert LifecycleState.ACTIVE not in reachable
    assert reachable.isdisjoint(
        {LifecycleState.PAPER, LifecycleState.PRODUCTION_CANDIDATE, LifecycleState.RETIRED}
    )
    assert reachable - {LifecycleState.IDEA} == AUTOMATABLE_TARGETS


def test_budget_configuration_has_no_defaults() -> None:
    with pytest.raises(ValueError, match="lacks"):
        LoopBudget.from_config({"max_trials_per_round": 1})
    budget = LoopBudget.from_config(
        {
            "max_trials_per_round": 1,
            "max_trials_total": 2,
            "max_llm_cost_units": "3",
            "max_compute_seconds": 4,
        }
    )
    assert budget.max_compute_seconds == Decimal(4) and len(budget.budget_hash) == 64


def test_degradation_monitor_uses_profile_thresholds_and_publishes() -> None:
    # TEST ONLY thresholds (arbitrary numbers, not a proposal).
    profile = factories.validation_profile(
        lifecycle=LifecycleParams(
            paper_period=timedelta(days=30),
            paper_acceptance_rule="test-rule",
            degradation_thresholds=FrozenMapping({"sharpe": 0.5, "max_drawdown[<=]": 0.1}),
        )
    )
    bus = InMemoryEventBus()
    monitor = DegradationMonitor.from_profile(profile, bus=bus)
    baseline = {"sharpe": Decimal("1.2"), "max_drawdown": Decimal("0.10")}
    healthy = monitor.observe(SUBJECT, baseline, {"sharpe": Decimal("1.0")}, window="w1")
    assert not healthy.degraded and healthy.missing == ("max_drawdown",)
    assert bus.poll("ops", DEGRADATION_TOPIC, 10) == ()
    worse = monitor.observe(
        SUBJECT, baseline, {"sharpe": Decimal("0.5"), "max_drawdown": Decimal("0.25")}, window="w2"
    )
    assert {b["metric"] for b in worse.breaches} == {"sharpe", "max_drawdown"}
    [event] = bus.poll("ops", DEGRADATION_TOPIC, 10)
    assert event.payload["subject"] == str(SUBJECT)
    with pytest.raises(ValueError, match="no defaults"):
        DegradationMonitor({}, source="none")
    with pytest.raises(ValueError, match="lacks"):
        monitor.check(SUBJECT, {"sharpe": 1}, {})


def test_worker_loop_modules_depend_only_on_core_and_the_stdlib() -> None:
    allowed_roots = {"apps", "core", "__future__", "collections", "dataclasses", "datetime"}
    allowed_roots |= {"decimal", "enum", "typing", "re"}
    for name in ("loop.py", "degradation.py"):
        tree = ast.parse((REPO / "apps" / "worker" / name).read_text(encoding="utf-8"))
        roots = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert roots <= allowed_roots, f"apps/worker/{name} imports {sorted(roots - allowed_roots)}"
