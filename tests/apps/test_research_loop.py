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
from apps.worker.degradation import (
    DEGRADATION_TOPIC,
    INSUFFICIENT_EVIDENCE_TOPIC,
    DegradationCheck,
)
from apps.worker.loop import (
    EXTENDED_STAGE_ORDER,
    FORBIDDEN_TARGETS,
    ROUND_TOPIC,
    STAGE_TOPIC,
    automation_reachable_states,
    missing_validation_failed_evidence,
)
from core.contracts.event_bus import BusMessage
from core.contracts.validation_profile import LifecycleParams
from core.domain.base import FrozenMapping, Kind, Ref
from core.errors import LifecycleViolation
from core.lifecycle.strategy import ALLOWED_TRANSITIONS, LifecycleState
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


def test_an_under_reporting_stage_is_still_charged_its_declaration() -> None:
    """R24: a completed stage is charged max(declared, reported) per dimension."""
    declared = StageUsage(trials=2, llm_cost_units=Decimal(1), compute_seconds=Decimal(5))
    quiet = FakeStage(
        "hypothesis", declared, actual=StageUsage(trials=0, compute_seconds=Decimal(7))
    )
    budget = LoopBudget(
        max_trials_per_round=5,
        max_trials_total=4,
        max_llm_cost_units=Decimal(100),
        max_compute_seconds=Decimal(1000),
    )
    loop, _ = _loop(_stages(hypothesis=quiet), budget)
    records = loop.run_unattended(5)
    stage = {s.name: s for s in records[0].stages}["hypothesis"]
    # compute 7 > 5 is an overrun and halts; the dimensions it under-reported are still charged
    assert stage.status is StageStatus.BUDGET_OVERRUN
    charged = StageUsage(trials=2, llm_cost_units=Decimal(1), compute_seconds=Decimal(7))
    assert stage.charged == charged == records[0].round_usage
    assert stage.payload()["charged"] == charged.payload()
    honest_but_quiet = FakeStage("hypothesis", declared, actual=StageUsage())
    loop, _ = _loop(_stages(hypothesis=honest_but_quiet), budget)
    records = loop.run_unattended(5)
    # reported 0 trials each round, but 2 were declared: the trial budget (4) ends after 2 rounds
    assert [r.status for r in records] == [
        RoundStatus.COMPLETED,
        RoundStatus.COMPLETED,
        RoundStatus.BUDGET_EXHAUSTED,
    ]
    assert loop.total_usage == StageUsage(
        trials=4, llm_cost_units=Decimal(2), compute_seconds=Decimal(10)
    )
    first = {s.name: s for s in records[0].stages}["hypothesis"]
    assert first.usage == StageUsage() and first.charged == declared
    # a stage that reports exactly its declaration records no separate charge
    exact = {s.name: s for s in records[0].stages}["ingest"]
    assert exact.charged is None and exact.payload()["charged"] is None


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


def test_adr_0053_leaves_the_automation_reachable_states_unchanged() -> None:
    """ADR-0053 §4: FAILED was already reachable (CANDIDATE → FAILED); the new edge adds no
    state, and PAPER / PRODUCTION_CANDIDATE / ACTIVE stay out of reach."""
    assert (LifecycleState.VALIDATION, LifecycleState.FAILED) in ALLOWED_TRANSITIONS
    assert automation_reachable_states() == {
        LifecycleState.IDEA,
        LifecycleState.CANDIDATE,
        LifecycleState.VALIDATION,
        LifecycleState.OOS,
        LifecycleState.REJECTED,
        LifecycleState.FAILED,
    }
    assert automation_reachable_states().isdisjoint(FORBIDDEN_TARGETS)


def _in_validation() -> LifecycleGuard:
    guard = LifecycleGuard(actor="loop")
    guard.open(SUBJECT)
    for state in (LifecycleState.CANDIDATE, LifecycleState.VALIDATION):
        guard.advance(SUBJECT, state, reason="r", evidence=("e",), occurred_at=EPOCH)
    return guard


ADR_0053_EVIDENCE = ("validation_report:r-1", "failure_record:abc", "loop_round:loop:0")


@pytest.mark.parametrize(
    "evidence",
    [
        ("e",),
        ADR_0053_EVIDENCE[1:],  # no report / run
        (ADR_0053_EVIDENCE[0], ADR_0053_EVIDENCE[2]),  # no FailureRecord hash
        ADR_0053_EVIDENCE[:2],  # no round
        ("validation_report: ", "failure_record:abc", "loop_round:loop:0"),  # empty reference
    ],
)
def test_the_guard_refuses_validation_to_failed_without_the_adr_0053_evidence(
    evidence: tuple[str, ...],
) -> None:
    guard = _in_validation()
    assert missing_validation_failed_evidence(evidence)
    with pytest.raises(AutomationForbidden, match="ADR-0053"):
        guard.advance(
            SUBJECT, LifecycleState.FAILED, reason="r", evidence=evidence, occurred_at=EPOCH
        )
    assert guard.state_of(SUBJECT) is LifecycleState.VALIDATION


@pytest.mark.parametrize(
    "evidence",
    [ADR_0053_EVIDENCE, ("run:loop:1:h@1.0.0", "failure_record:abc", "loop_round:loop:1")],
)
def test_the_guard_moves_validation_to_failed_with_the_adr_0053_evidence(
    evidence: tuple[str, ...],
) -> None:
    guard = _in_validation()
    assert missing_validation_failed_evidence(evidence) == ()
    transition = guard.advance(
        SUBJECT, LifecycleState.FAILED, reason="r", evidence=evidence, occurred_at=EPOCH
    )
    assert transition.approved_by is None and guard.state_of(SUBJECT) is LifecycleState.FAILED
    for target in FORBIDDEN_TARGETS | {LifecycleState.CANDIDATE}:  # FAILED is terminal
        with pytest.raises(LifecycleViolation):
            guard.advance(SUBJECT, target, reason="r", evidence=evidence, occurred_at=EPOCH)


def test_the_guard_keeps_candidate_to_failed_as_it_was() -> None:
    guard = LifecycleGuard(actor="loop")
    guard.open(SUBJECT)
    guard.advance(SUBJECT, LifecycleState.CANDIDATE, reason="r", evidence=("e",), occurred_at=EPOCH)
    guard.advance(SUBJECT, LifecycleState.FAILED, reason="r", evidence=("e",), occurred_at=EPOCH)
    assert guard.state_of(SUBJECT) is LifecycleState.FAILED


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


class _RecordingBus(InMemoryEventBus):
    """Every published message, whatever its topic (a lifecycle side effect would show here)."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[BusMessage] = []

    def publish(self, message: BusMessage) -> None:
        super().publish(message)
        self.published.append(message)


def test_a_check_with_every_metric_missing_is_insufficient_evidence_never_healthy() -> None:
    # TEST ONLY thresholds (arbitrary numbers, not a proposal).
    bus = _RecordingBus()
    monitor = DegradationMonitor({"sharpe": 0.5, "max_drawdown[<=]": 0.1}, source="t", bus=bus)
    baseline = {"sharpe": Decimal("1.2"), "max_drawdown": Decimal("0.10")}
    # check() is pure: the same result, nothing published
    pure = monitor.check(SUBJECT, baseline, {})
    assert pure.status == "insufficient_evidence" and bus.published == []
    empty = monitor.observe(SUBJECT, baseline, {}, window="w0")
    assert empty == pure
    assert empty.insufficient_evidence and empty.status == "insufficient_evidence"
    assert not empty.degraded and empty.missing == ("max_drawdown", "sharpe")
    # exactly one alert on the distinct topic (D-DEG-IE), never a degradation event
    assert bus.poll("ops", DEGRADATION_TOPIC, 10) == ()
    [alert] = bus.published
    assert alert.topic == INSUFFICIENT_EVIDENCE_TOPIC
    assert INSUFFICIENT_EVIDENCE_TOPIC == "research_loop.degradation.insufficient_evidence"
    assert alert.key == f"{SUBJECT}:w0"
    # the exact envelope: topic, key, payload and content-derived message_id
    assert alert == BusMessage.build(
        INSUFFICIENT_EVIDENCE_TOPIC,
        f"{SUBJECT}:w0",
        {
            "subject": str(SUBJECT),
            "window": "w0",
            "status": "insufficient_evidence",
            "missing": ["max_drawdown", "sharpe"],
            "required": ["max_drawdown", "sharpe"],
        },
    )
    assert set(alert.payload) == {"subject", "window", "status", "missing", "required"}
    assert bus.poll("ops", INSUFFICIENT_EVIDENCE_TOPIC, 10) == (alert,)
    # the state cannot be claimed with a breach or without missing metrics
    worse = monitor.check(SUBJECT, baseline, {"sharpe": Decimal("0.1")})
    assert worse.status == "degraded" and not worse.insufficient_evidence
    with pytest.raises(ValueError, match="insufficient evidence"):
        DegradationCheck(SUBJECT, worse.breaches, ("sharpe",), insufficient_evidence=True)
    with pytest.raises(ValueError, match="insufficient evidence"):
        DegradationCheck(SUBJECT, (), (), insufficient_evidence=True)


def test_insufficient_evidence_payload_order_is_deterministic() -> None:
    # TEST ONLY thresholds; keys given out of order and with both direction spellings.
    bus = _RecordingBus()
    thresholds = {"zeta[<=]": 0.2, "alpha[>=]": 0.1, "mid": 0.3}
    DegradationMonitor(thresholds, source="t", bus=bus).observe(
        SUBJECT, {"zeta": 1, "alpha": 1, "mid": 1}, {}, window="w"
    )
    [alert] = bus.published
    assert tuple(alert.payload["missing"]) == ("alpha", "mid", "zeta")
    assert tuple(alert.payload["required"]) == ("alpha", "mid", "zeta")


def test_observe_without_a_bus_fails_closed_but_check_stays_pure() -> None:
    # TEST ONLY thresholds (arbitrary numbers, not a proposal).
    monitor = DegradationMonitor({"sharpe": 0.5}, source="t")
    baseline = {"sharpe": Decimal("1.2")}
    assert monitor.check(SUBJECT, baseline, {}).status == "insufficient_evidence"
    assert monitor.check(SUBJECT, baseline, {"sharpe": 0}).status == "degraded"
    with pytest.raises(ValueError, match="needs a bus to publish the insufficient_evidence event"):
        monitor.observe(SUBJECT, baseline, {}, window="w")
    with pytest.raises(ValueError, match="needs a bus to publish the degraded event"):
        monitor.observe(SUBJECT, baseline, {"sharpe": 0}, window="w")
    # nothing to publish -> no bus needed
    healthy = monitor.observe(SUBJECT, baseline, {"sharpe": Decimal("1.1")}, window="w")
    assert healthy.status == "not_degraded"


def test_a_breach_publishes_only_the_degradation_topic() -> None:
    # TEST ONLY thresholds (arbitrary numbers, not a proposal).
    bus = _RecordingBus()
    monitor = DegradationMonitor({"sharpe": 0.5, "max_drawdown[<=]": 0.1}, source="t", bus=bus)
    baseline = {"sharpe": Decimal("1.2"), "max_drawdown": Decimal("0.10")}
    # a breach with a missing metric: the existing event and payload, the missing list attached
    worse = monitor.observe(SUBJECT, baseline, {"sharpe": Decimal("0.5")}, window="w2")
    assert worse.status == "degraded" and worse.missing == ("max_drawdown",)
    [event] = bus.published
    assert event.topic == DEGRADATION_TOPIC and event.key == f"{SUBJECT}:w2"
    assert event == BusMessage.build(
        DEGRADATION_TOPIC,
        f"{SUBJECT}:w2",
        {
            "subject": str(SUBJECT),
            "window": "w2",
            "breaches": list(worse.breaches),
            "missing": ["max_drawdown"],
        },
    )
    assert bus.poll("ops", INSUFFICIENT_EVIDENCE_TOPIC, 10) == ()


def test_partial_missing_without_a_breach_publishes_nothing() -> None:
    # TEST ONLY thresholds (arbitrary numbers, not a proposal).
    bus = _RecordingBus()
    monitor = DegradationMonitor({"sharpe": 0.5, "max_drawdown[<=]": 0.1}, source="t", bus=bus)
    baseline = {"sharpe": Decimal("1.2"), "max_drawdown": Decimal("0.10")}
    partial = monitor.observe(SUBJECT, baseline, {"sharpe": Decimal("1.1")}, window="w1")
    assert not partial.insufficient_evidence and partial.status == "not_degraded"
    assert partial.missing == ("max_drawdown",)
    assert bus.published == []


def test_degradation_events_never_move_lifecycle_state() -> None:
    # The monitor has no lifecycle access: it imports nothing lifecycle-related and holds no
    # guard / subject store, so neither topic can trigger ACTIVE -> DEGRADED, replacement, or
    # approval. Control Plane / an operator treats the events as evidence only (ADR-0049).
    tree = ast.parse((REPO / "apps" / "worker" / "degradation.py").read_text(encoding="utf-8"))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert modules == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "decimal",
        "typing",
        "core.contracts.event_bus",
        "core.contracts.validation_profile",
        "core.domain.base",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"LifecycleState", "LifecycleGuard", "RoundContext", "approved_by"}
    bus = _RecordingBus()
    monitor = DegradationMonitor({"sharpe": 0.5}, source="t", bus=bus)
    monitor.observe(SUBJECT, {"sharpe": 1}, {}, window="a")
    monitor.observe(SUBJECT, {"sharpe": 1}, {"sharpe": 0}, window="b")
    assert [m.topic for m in bus.published] == [INSUFFICIENT_EVIDENCE_TOPIC, DEGRADATION_TOPIC]
    for message in bus.published:
        assert not {"state", "target", "transition", "approved_by"} & set(message.payload)


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
