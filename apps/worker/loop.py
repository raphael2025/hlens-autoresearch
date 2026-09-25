"""Continuous research loop: scheduler, budget, lifecycle guard and audit (Phase 11; ADR-0049).

This is the **research-agnostic** mechanism. It never imports ``research/`` (01-system.md §3): the
concrete stages are injected by a composition root (``research/loop/compose.py`` or a test).

A *round* runs the stages in the fixed order ``STAGE_ORDER`` (ingest new data -> state update ->
hypothesis generation -> experiment -> validation -> memory). Before each stage the scheduler asks
the stage for its declared usage (``estimate``) and consults the ``LoopBudget``:

- the estimate does not fit -> the stage is ``REFUSED_BUDGET``, the round stops as
  ``BUDGET_EXHAUSTED`` and the loop halts; the budget is never expanded (a larger budget is a new
  ``LoopBudget`` whose hash is written into every record);
- the stage reports more usage than it declared -> the usage is charged (it happened), the stage is
  ``BUDGET_OVERRUN`` and the loop halts (fail closed on an under-declaring stage);
- the stage raises -> the stage is ``FAILED``, the round is recorded as ``FAILED`` (a failure is
  research data) and the next round may run;
- the stage asks for a lifecycle transition the automation may not make -> ``GUARD_VIOLATION`` and
  the loop halts.

Every round, including failures and refusals, becomes one ``LoopRecord`` appended to a hash-chained,
append-only ``LoopAuditLog`` and published on the event bus. The records hold no wall-clock time:
a round's time is its **scheduled** time ``epoch + round_index * cadence``, and its seed is derived
from the loop seed, so the same seed and inputs give the same record hashes.

Rounds are driven as ``JobRunner`` jobs (one job per round, ``max_attempts = 1``: a round that
spent budget is never retried; a duplicate submission is absorbed by the job's content identity).

**Lifecycle guard.** Stages receive no lifecycle object; they can only call
``RoundContext.open_subject`` / ``RoundContext.advance``, which go through ``LifecycleGuard``. The
guard only moves subjects it opened itself (from ``IDEA``), never sets ``approved_by`` and only
targets ``AUTOMATABLE_TARGETS`` (the states reachable from ``IDEA`` before the first human approval
gate ``OOS -> PAPER``). ``PAPER``, ``PRODUCTION_CANDIDATE`` and ``ACTIVE`` are therefore unreachable
for the loop by construction (roadmap Phase 11: no automatic promotion to ACTIVE).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

from apps.worker.jobs import JobOutcome, JobRunner, JobSpec
from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.domain.base import Ref, content_hash
from core.errors import LifecycleViolation
from core.lifecycle.strategy import (
    ALLOWED_TRANSITIONS,
    HUMAN_APPROVAL_TRANSITIONS,
    LifecycleHistory,
    LifecycleState,
    LifecycleTransition,
)

__all__ = [
    "AUTOMATABLE_TARGETS",
    "FORBIDDEN_TARGETS",
    "ROUND_JOB",
    "ROUND_TOPIC",
    "STAGE_ORDER",
    "STAGE_TOPIC",
    "AutomationForbidden",
    "LifecycleGuard",
    "LoopAuditLog",
    "LoopBudget",
    "LoopHalted",
    "LoopRecord",
    "LoopStage",
    "ResearchLoop",
    "RoundContext",
    "RoundStatus",
    "StageRecord",
    "StageResult",
    "StageStatus",
    "StageUsage",
    "automation_reachable_states",
]

#: The fixed order of a round (roadmap Phase 11: 新数据 → 状态更新 → 假设 → 实验 → 验证 → 记忆).
STAGE_ORDER: Final[tuple[str, ...]] = (
    "ingest",
    "state",
    "hypothesis",
    "experiment",
    "validation",
    "memory",
)

ROUND_JOB: Final = "research_loop.round"
JOB_TOPIC: Final = "research_loop.jobs"
STAGE_TOPIC: Final = "research_loop.stage"
ROUND_TOPIC: Final = "research_loop.round"

S = LifecycleState

#: States the loop may move a subject into (all reachable from IDEA without human approval).
AUTOMATABLE_TARGETS: Final[frozenset[LifecycleState]] = frozenset(
    {S.CANDIDATE, S.VALIDATION, S.OOS, S.REJECTED, S.FAILED}
)
#: States the loop must never produce (roadmap Phase 11 禁止事项; ADR-0006 human gates).
FORBIDDEN_TARGETS: Final[frozenset[LifecycleState]] = frozenset(
    {S.PAPER, S.PRODUCTION_CANDIDATE, S.ACTIVE, S.DEGRADED, S.REVALIDATION, S.RETIRED}
)


def automation_reachable_states() -> frozenset[LifecycleState]:
    """States reachable from IDEA through transitions the guard permits (closure over the graph)."""
    reached = {S.IDEA}
    frontier = [S.IDEA]
    while frontier:
        state = frontier.pop()
        for source, target in ALLOWED_TRANSITIONS:
            if (
                source is state
                and target in AUTOMATABLE_TARGETS
                and (source, target) not in HUMAN_APPROVAL_TRANSITIONS
                and target not in reached
            ):
                reached.add(target)
                frontier.append(target)
    return frozenset(reached)


class AutomationForbidden(LifecycleViolation):
    """The loop asked for a lifecycle move reserved for humans or outside its own subjects."""


class LoopHalted(RuntimeError):
    """The loop stopped (budget refusal / overrun / guard violation); a human must reconfigure."""


# --------------------------------------------------------------------------- usage and budget


def _decimal(value: Decimal | int | str, name: str) -> Decimal:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return number


@dataclass(frozen=True, slots=True)
class StageUsage:
    """Declared or actual usage of one stage: trials, LLM cost units and compute seconds."""

    trials: int = 0
    llm_cost_units: Decimal = Decimal(0)
    compute_seconds: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if isinstance(self.trials, bool) or not isinstance(self.trials, int) or self.trials < 0:
            raise ValueError("trials must be a non-negative int")
        object.__setattr__(self, "llm_cost_units", _decimal(self.llm_cost_units, "llm_cost_units"))
        object.__setattr__(
            self, "compute_seconds", _decimal(self.compute_seconds, "compute_seconds")
        )

    def __add__(self, other: StageUsage) -> StageUsage:
        return StageUsage(
            self.trials + other.trials,
            self.llm_cost_units + other.llm_cost_units,
            self.compute_seconds + other.compute_seconds,
        )

    def exceeds(self, other: StageUsage) -> bool:
        """True if any dimension is larger than in ``other``."""
        return (
            self.trials > other.trials
            or self.llm_cost_units > other.llm_cost_units
            or self.compute_seconds > other.compute_seconds
        )

    def payload(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "llm_cost_units": str(self.llm_cost_units),
            "compute_seconds": str(self.compute_seconds),
        }


@dataclass(frozen=True, slots=True)
class LoopBudget:
    """Hard limits of one loop. Numbers come from configuration; there are no defaults.

    ``max_trials_per_round`` bounds one round, the other three bound the loop's whole life. The
    budget is frozen: the scheduler has no way to raise it (roadmap P11: 禁止无限扩大 trial 预算).
    """

    max_trials_per_round: int
    max_trials_total: int
    max_llm_cost_units: Decimal
    max_compute_seconds: Decimal

    def __post_init__(self) -> None:
        for name in ("max_trials_per_round", "max_trials_total"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        object.__setattr__(
            self, "max_llm_cost_units", _decimal(self.max_llm_cost_units, "max_llm_cost_units")
        )
        object.__setattr__(
            self, "max_compute_seconds", _decimal(self.max_compute_seconds, "max_compute_seconds")
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> LoopBudget:
        """Build from a configuration mapping; every key is required (no silent defaults)."""
        missing = {
            "max_trials_per_round",
            "max_trials_total",
            "max_llm_cost_units",
            "max_compute_seconds",
        } - set(config)
        if missing:
            raise ValueError(f"loop budget configuration lacks {sorted(missing)}")
        return cls(
            max_trials_per_round=config["max_trials_per_round"],
            max_trials_total=config["max_trials_total"],
            max_llm_cost_units=Decimal(str(config["max_llm_cost_units"])),
            max_compute_seconds=Decimal(str(config["max_compute_seconds"])),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "max_trials_per_round": self.max_trials_per_round,
            "max_trials_total": self.max_trials_total,
            "max_llm_cost_units": str(self.max_llm_cost_units),
            "max_compute_seconds": str(self.max_compute_seconds),
        }

    @property
    def budget_hash(self) -> str:
        return content_hash(self.payload())

    def refusals(
        self, round_spent: StageUsage, total_spent: StageUsage, ask: StageUsage
    ) -> list[str]:
        """The limits ``ask`` would break on top of what was spent (empty = it fits)."""
        broken: list[str] = []
        if round_spent.trials + ask.trials > self.max_trials_per_round:
            broken.append("max_trials_per_round")
        if total_spent.trials + ask.trials > self.max_trials_total:
            broken.append("max_trials_total")
        if total_spent.llm_cost_units + ask.llm_cost_units > self.max_llm_cost_units:
            broken.append("max_llm_cost_units")
        if total_spent.compute_seconds + ask.compute_seconds > self.max_compute_seconds:
            broken.append("max_compute_seconds")
        return broken


# --------------------------------------------------------------------------- lifecycle guard


class LifecycleGuard:
    """The loop's only write path to lifecycle state (see module docs)."""

    def __init__(self, actor: str) -> None:
        if not actor.strip():
            raise ValueError("actor must be non-empty")
        self._actor = actor
        self._histories: dict[tuple[Any, ...], LifecycleHistory] = {}

    @property
    def histories(self) -> tuple[LifecycleHistory, ...]:
        return tuple(self._histories.values())

    def state_of(self, subject: Ref) -> LifecycleState | None:
        history = self._histories.get(subject.target_identity())
        return None if history is None else history.current_state

    def open(self, subject: Ref) -> bool:
        """Start tracking ``subject`` at IDEA; ``False`` if the loop already owns it."""
        key = subject.target_identity()
        if key in self._histories:
            return False
        self._histories[key] = LifecycleHistory(subject=subject)
        return True

    def advance(
        self,
        subject: Ref,
        to_state: LifecycleState,
        *,
        reason: str,
        evidence: Sequence[str],
        occurred_at: datetime,
    ) -> LifecycleTransition:
        if to_state not in AUTOMATABLE_TARGETS:
            raise AutomationForbidden(
                f"the research loop may not move {subject} to {to_state} (human / production state)"
            )
        key = subject.target_identity()
        history = self._histories.get(key)
        if history is None:
            raise AutomationForbidden(f"{subject} was not opened by the loop; it is not the loop's")
        from_state = history.current_state
        if (from_state, to_state) in HUMAN_APPROVAL_TRANSITIONS:
            raise AutomationForbidden(f"{from_state} → {to_state} needs a human approval")
        transition = LifecycleTransition(
            subject=subject,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            evidence=tuple(evidence),
            triggered_by=self._actor,
            approved_by=None,
            occurred_at=occurred_at,
        )
        self._histories[key] = history.append(transition)
        return transition


# --------------------------------------------------------------------------- stages and records


class StageStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REFUSED_BUDGET = "REFUSED_BUDGET"
    BUDGET_OVERRUN = "BUDGET_OVERRUN"
    GUARD_VIOLATION = "GUARD_VIOLATION"
    SKIPPED = "SKIPPED"


class RoundStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    BUDGET_OVERRUN = "BUDGET_OVERRUN"
    GUARD_VIOLATION = "GUARD_VIOLATION"


#: Round outcomes after which the loop refuses to run further rounds.
HALTING: Final = frozenset(
    {RoundStatus.BUDGET_EXHAUSTED, RoundStatus.BUDGET_OVERRUN, RoundStatus.GUARD_VIOLATION}
)


@dataclass(frozen=True, slots=True)
class StageResult:
    """What a stage returns: an auditable JSON ``summary``, its actual usage, and in-memory
    ``artifacts`` for later stages of the same round (not hashed, not published)."""

    summary: Mapping[str, Any]
    usage: StageUsage = StageUsage()
    artifacts: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RoundContext:
    """Per-round inputs handed to every stage."""

    loop_id: str
    round_index: int
    seed: int
    as_of: datetime
    _guard: LifecycleGuard
    artifacts: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    transitions: list[LifecycleTransition] = field(default_factory=list)

    def artifact(self, stage: str, key: str) -> Any:
        """An artifact a previous stage of this round produced."""
        return self.artifacts[stage][key]

    def open_subject(self, subject: Ref) -> bool:
        return self._guard.open(subject)

    def state_of(self, subject: Ref) -> LifecycleState | None:
        return self._guard.state_of(subject)

    def advance(
        self, subject: Ref, to_state: LifecycleState, *, reason: str, evidence: Sequence[str]
    ) -> LifecycleTransition:
        transition = self._guard.advance(
            subject, to_state, reason=reason, evidence=evidence, occurred_at=self.as_of
        )
        self.transitions.append(transition)
        return transition


class LoopStage(Protocol):
    """One stage of a round. ``estimate`` must be pure and must not undercount ``run``."""

    @property
    def name(self) -> str: ...

    def estimate(self, ctx: RoundContext) -> StageUsage: ...

    def run(self, ctx: RoundContext) -> StageResult: ...


@dataclass(frozen=True, slots=True)
class StageRecord:
    name: str
    status: StageStatus
    estimate: StageUsage | None = None
    usage: StageUsage | None = None
    summary: Mapping[str, Any] | None = None
    error: str | None = None
    refused: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "estimate": None if self.estimate is None else self.estimate.payload(),
            "usage": None if self.usage is None else self.usage.payload(),
            "summary": None if self.summary is None else dict(self.summary),
            "error": self.error,
            "refused": list(self.refused),
        }


def _transition_payload(transition: LifecycleTransition) -> dict[str, Any]:
    return {
        "subject": str(transition.subject),
        "from": transition.from_state.value,
        "to": transition.to_state.value,
        "reason": transition.reason,
        "evidence": list(transition.evidence),
        "triggered_by": transition.triggered_by,
    }


@dataclass(frozen=True, slots=True)
class LoopRecord:
    """The audit record of one round (hash-chained through ``previous_hash``)."""

    loop_id: str
    round_index: int
    seed: int
    as_of: datetime
    budget_hash: str
    status: RoundStatus
    stages: tuple[StageRecord, ...]
    transitions: tuple[LifecycleTransition, ...]
    round_usage: StageUsage
    total_usage: StageUsage
    previous_hash: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "round_index": self.round_index,
            "seed": self.seed,
            "as_of": self.as_of.isoformat(),
            "budget_hash": self.budget_hash,
            "status": self.status.value,
            "stages": [stage.payload() for stage in self.stages],
            "transitions": [_transition_payload(t) for t in self.transitions],
            "round_usage": self.round_usage.payload(),
            "total_usage": self.total_usage.payload(),
            "previous_hash": self.previous_hash,
        }

    @property
    def record_hash(self) -> str:
        return content_hash(self.payload())


class LoopAuditLog:
    """Append-only, hash-chained list of ``LoopRecord`` (no update or delete method)."""

    def __init__(self) -> None:
        self._records: list[LoopRecord] = []

    @property
    def records(self) -> tuple[LoopRecord, ...]:
        return tuple(self._records)

    @property
    def head(self) -> str | None:
        return self._records[-1].record_hash if self._records else None

    def append(self, record: LoopRecord) -> None:
        if record.round_index != len(self._records):
            raise ValueError(
                f"round {record.round_index} is out of order (next {len(self._records)})"
            )
        if record.previous_hash != self.head:
            raise ValueError("the record does not chain to the audit head")
        self._records.append(record)

    def verify(self) -> bool:
        previous: str | None = None
        for index, record in enumerate(self._records):
            if record.round_index != index or record.previous_hash != previous:
                return False
            previous = record.record_hash
        return True


# --------------------------------------------------------------------------- the loop


def _round_seed(loop_seed: int, round_index: int) -> int:
    return int(content_hash({"loop_seed": loop_seed, "round": round_index})[:12], 16)


class ResearchLoop:
    """Schedules rounds of injected stages through ``JobRunner`` (see module docs)."""

    def __init__(
        self,
        *,
        loop_id: str,
        stages: Sequence[LoopStage],
        budget: LoopBudget,
        bus: EventBusAdapter,
        seed: int,
        epoch: datetime,
        cadence: timedelta,
        guard: LifecycleGuard | None = None,
        audit: LoopAuditLog | None = None,
        consumer: str = "research_loop_worker",
    ) -> None:
        names = tuple(stage.name for stage in stages)
        if names != STAGE_ORDER:
            raise ValueError(f"stages must be exactly {STAGE_ORDER} in order, got {names}")
        if epoch.tzinfo is None or epoch.utcoffset() != timedelta(0):
            raise ValueError("epoch must be a UTC datetime")
        if cadence <= timedelta(0):
            raise ValueError("cadence must be positive")
        self._loop_id = loop_id
        self._stages = tuple(stages)
        self._budget = budget
        self._bus = bus
        self._seed = seed
        self._epoch = epoch
        self._cadence = cadence
        self._guard = guard or LifecycleGuard(actor=f"research_loop:{loop_id}")
        self._audit = audit or LoopAuditLog()
        self._total = StageUsage()
        self._halted: RoundStatus | None = None
        self._runner = JobRunner(
            bus,
            consumer=consumer,
            topic=JOB_TOPIC,
            handlers={ROUND_JOB: self._handle_round},
            max_attempts=1,
        )

    @property
    def audit(self) -> LoopAuditLog:
        return self._audit

    @property
    def guard(self) -> LifecycleGuard:
        return self._guard

    @property
    def budget(self) -> LoopBudget:
        return self._budget

    @property
    def total_usage(self) -> StageUsage:
        return self._total

    @property
    def halted(self) -> RoundStatus | None:
        """Why the loop stopped, or ``None`` while it may run."""
        return self._halted

    def submit_round(self, round_index: int) -> str:
        if self._halted is not None:
            raise LoopHalted(f"the loop halted ({self._halted}); reconfigure it to continue")
        job = JobSpec(ROUND_JOB, {"loop_id": self._loop_id, "round": round_index})
        return self._runner.submit(job)

    def run_pending(self) -> list[JobOutcome]:
        return self._runner.run_pending()

    def run_unattended(self, rounds: int) -> tuple[LoopRecord, ...]:
        """Run up to ``rounds`` further rounds, stopping as soon as the loop halts."""
        if rounds < 1:
            raise ValueError("rounds must be positive")
        start = len(self._audit.records)
        for index in range(start, start + rounds):
            if self._halted is not None:
                break
            self.submit_round(index)
            for outcome in self.run_pending():
                if not outcome.succeeded:
                    raise RuntimeError(f"round job {outcome.job_id} failed: {outcome.error}")
        return self._audit.records[start:]

    # -- internals ------------------------------------------------------------------------------

    def _handle_round(self, params: Mapping[str, Any]) -> str:
        if params["loop_id"] != self._loop_id:
            raise ValueError("the job belongs to another loop")
        round_index = int(params["round"])
        if round_index != len(self._audit.records):
            raise ValueError(f"round {round_index} is out of order")
        if self._halted is not None:
            raise LoopHalted(f"the loop halted ({self._halted})")
        record = self._run_round(round_index)
        self._audit.append(record)
        self._bus.publish(
            BusMessage.build(
                ROUND_TOPIC,
                f"{self._loop_id}:{round_index}",
                {"record_hash": record.record_hash, "record": record.payload()},
            )
        )
        if record.status in HALTING:
            self._halted = record.status
        return record.record_hash

    def _run_round(self, round_index: int) -> LoopRecord:
        ctx = RoundContext(
            loop_id=self._loop_id,
            round_index=round_index,
            seed=_round_seed(self._seed, round_index),
            as_of=self._epoch + round_index * self._cadence,
            _guard=self._guard,
        )
        round_usage = StageUsage()
        records: list[StageRecord] = []
        status = RoundStatus.COMPLETED
        for stage in self._stages:
            if status is not RoundStatus.COMPLETED:
                records.append(StageRecord(stage.name, StageStatus.SKIPPED))
                continue
            record, spent = self._run_stage(stage, ctx, round_usage)
            round_usage = round_usage + spent
            self._total = self._total + spent
            records.append(record)
            self._publish_stage(ctx, record)
            status = {
                StageStatus.COMPLETED: RoundStatus.COMPLETED,
                StageStatus.FAILED: RoundStatus.FAILED,
                StageStatus.REFUSED_BUDGET: RoundStatus.BUDGET_EXHAUSTED,
                StageStatus.BUDGET_OVERRUN: RoundStatus.BUDGET_OVERRUN,
                StageStatus.GUARD_VIOLATION: RoundStatus.GUARD_VIOLATION,
            }[record.status]
        return LoopRecord(
            loop_id=self._loop_id,
            round_index=round_index,
            seed=ctx.seed,
            as_of=ctx.as_of,
            budget_hash=self._budget.budget_hash,
            status=status,
            stages=tuple(records),
            transitions=tuple(ctx.transitions),
            round_usage=round_usage,
            total_usage=self._total,
            previous_hash=self._audit.head,
        )

    def _run_stage(
        self, stage: LoopStage, ctx: RoundContext, round_usage: StageUsage
    ) -> tuple[StageRecord, StageUsage]:
        try:
            estimate = stage.estimate(ctx)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed stage, never dropped
            return StageRecord(stage.name, StageStatus.FAILED, error=_error(exc)), StageUsage()
        refused = self._budget.refusals(round_usage, self._total, estimate)
        if refused:
            record = StageRecord(
                stage.name, StageStatus.REFUSED_BUDGET, estimate=estimate, refused=tuple(refused)
            )
            return record, StageUsage()
        try:
            result = stage.run(ctx)
            content_hash(dict(result.summary))  # the summary must be canonical JSON
        except AutomationForbidden as exc:
            record = StageRecord(
                stage.name, StageStatus.GUARD_VIOLATION, estimate=estimate, error=_error(exc)
            )
            return record, estimate
        except Exception as exc:  # noqa: BLE001 - recorded as a failed stage, never dropped
            record = StageRecord(
                stage.name, StageStatus.FAILED, estimate=estimate, error=_error(exc)
            )
            return record, estimate
        ctx.artifacts[stage.name] = result.artifacts
        status = (
            StageStatus.BUDGET_OVERRUN if result.usage.exceeds(estimate) else StageStatus.COMPLETED
        )
        record = StageRecord(
            stage.name, status, estimate=estimate, usage=result.usage, summary=result.summary
        )
        return record, result.usage

    def _publish_stage(self, ctx: RoundContext, record: StageRecord) -> None:
        self._bus.publish(
            BusMessage.build(
                STAGE_TOPIC,
                f"{self._loop_id}:{ctx.round_index}:{record.name}",
                {"round_index": ctx.round_index, "stage": record.payload()},
            )
        )


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]
