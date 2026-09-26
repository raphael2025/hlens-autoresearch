"""Continuous research loop: scheduler, budget, lifecycle guard and audit (Phase 11; ADR-0049).

This is the **research-agnostic** mechanism. It never imports ``research/`` (01-system.md §3): the
concrete stages are injected by a composition root (``research/loop/compose.py`` or a test).

A *round* runs the stages in the fixed order ``STAGE_ORDER`` (ingest new data -> state update ->
hypothesis generation -> experiment -> validation -> memory). The optional stages of
``OPTIONAL_STAGES`` (``evolution``, between hypothesis and experiment) may be added at their fixed
place in ``EXTENDED_STAGE_ORDER``; nothing else. Before each stage the scheduler asks the stage for
its declared usage (``estimate``) and consults the ``LoopBudget``:

- the estimate does not fit -> the stage is ``REFUSED_BUDGET``, the round stops as
  ``BUDGET_EXHAUSTED`` and the loop halts; the budget is never expanded (a larger budget is a new
  ``LoopBudget`` whose hash is written into every record);
- a completed stage is charged ``max(declared, reported)`` per dimension (``StageRecord.charged``;
  ADR-0049 review fixes 2): the reported usage is self-declared, so a stage that reports less than
  it declared is still charged its declaration and cannot stretch the budget by under-reporting;
- the stage reports more usage than it declared -> the usage is charged (it happened), the stage is
  ``BUDGET_OVERRUN``, the stage and round records state the overrun amount (actual minus declared,
  per dimension) and the loop halts (fail closed on an under-declaring stage);
- the stage raises -> the stage is ``FAILED``, the round is recorded as ``FAILED`` (a failure is
  research data) and the next round may run. A stage that raises ``StageFailed`` reports the usage
  it actually spent before failing, which is charged instead of the estimate (an overrun there halts
  the loop like any other); any other exception is charged at the estimate;
- the stage asks for a lifecycle transition the automation may not make -> ``GUARD_VIOLATION`` and
  the loop halts.

Every round, including failures and refusals, becomes one ``LoopRecord`` appended to a hash-chained,
append-only ``LoopAuditLog`` and published on the event bus. The records hold no wall-clock time:
a round's time is its **scheduled** time ``epoch + round_index * cadence``, and its seed is derived
from the loop seed, so the same seed and inputs give the same record hashes.

**Durable audit** (ADR-0049 implementation note, 2026-09-26). ``LoopAuditLog(path)`` backs the log
with a hash-chained, append-only, fsync'd JSON-lines file (``apps.worker.journal``). Before a
round runs, a ``loop_round_started`` line is written; after it, a ``loop_round_recorded`` line with
the record's payload and hash. Reopening the file replays and verifies both chains (the journal's
line hashes and the records' ``previous_hash`` / ``record_hash``, each record rebuilt from its
payload must reproduce its hash); a tampered or truncated file is refused (``JournalCorrupted``).
A ``ResearchLoop`` given a non-empty audit continues it: the next round index, the cumulative
budget totals, the halting state and the guard's subjects come from the verified records, so a
restart never re-runs a recorded round and never resets the budget. A round that was started but
never recorded (the process died mid-round, spending unknown) stops the loop for human review.

**Audit contract** (ADR-0050). The record and journal payloads are the versioned contracts of
``core.contracts.loop_audit`` (``LoopRoundRecord`` / ``LoopRoundStarted`` / ``LoopRoundRecorded``),
which describe the bytes this module writes without changing them (same keys, same
``record_hash``). ``LoopAuditLog`` validates every payload it writes (``begin_round``, ``append``;
durable or in memory) and every line it replays; a payload that does not conform, or does not
round-trip byte-identically, is refused (fail closed: a refused write stops the loop, a refused
replay is ``LoopAuditCorrupted``).

**Round checkpoint** (ADR-0049 implementation note, durable composition, 2026-09-26). An optional
``checkpoint`` callable is called with each finished round's ``LoopRecord`` after the round ran and
**before** the audit records it: a composition root persists whatever state its stages keep across
rounds there (``research/loop/compose.py`` writes its research-memory checkpoint, which names the
record hash). If it raises, the round is not recorded and the loop stops (fail closed), exactly
like an audit write failure; a checkpoint written for a round the audit never recorded is an
interrupted round for the composition to refuse on reopening. An optional ``after_record``
callable is called with each round's record right **after** the audit recorded it (the research
composition moves its external anchor there); if it raises, the round stays recorded and the loop
stops (fail closed).

**Budget binding** (ADR-0049 implementation note, durable review fixes, 2026-09-26). A loop that
continues an audit refuses any record made under another ``LoopBudget`` (``budget_hash``): the
budget of a running loop is never changed on a restart — not raised, not lowered. Raising a budget
is a human decision and takes a new audit / ``loop_id``.

**Measured time.** Every ``stage.run`` is timed with a monotonic wall clock and the process CPU
clock (``apps.worker.metrics``). The measurement is kept **outside** the hashed record, in
``ResearchLoop.metrics`` and on ``research_loop.metrics``; the budget still charges
``max(declared, reported)``. A stage whose measurement exceeds its declared compute seconds by more
than the configured ``compute_tolerance_seconds`` is flagged there (no default: report only).

Rounds are driven as ``JobRunner`` jobs (one job per round, ``max_attempts = 1``: a round that
spent budget is never retried; a duplicate submission is absorbed by the job's content identity).

**Durable bus** (ADR-0044 / ADR-0049 implementation notes, durable jobs and bus wiring,
2026-09-26). The audit is the durable result of a round job: a loop continuing an audit
acknowledges, when it is constructed, every still-unacknowledged round job (``ROUND_JOB`` on
``JOB_TOPIC``) of **this** loop whose round the audit already recorded (a durable bus re-delivers the job of a
process that died between recording the round and acking its message); such a job is never run
again. Jobs of other loops, of unrecorded rounds or with another content are left alone.
Each recorded round is published on ``research_loop.round`` as ``round_message(loop_id, record)``
right after the audit (and ``after_record``); if that publish fails, the round stays recorded and
the loop stops (fail closed), so a bus is never more than the last round behind its audit. The
research composition cross-checks its own bus against the audit on reopening.

**Lifecycle guard.** Stages receive no lifecycle object; they can only call
``RoundContext.open_subject`` / ``RoundContext.advance``, which go through ``LifecycleGuard``. The
guard only moves subjects it opened itself (from ``IDEA``), never sets ``approved_by`` and only
targets ``AUTOMATABLE_TARGETS`` (the states reachable from ``IDEA`` before the first human approval
gate ``OOS -> PAPER``). ``PAPER``, ``PRODUCTION_CANDIDATE`` and ``ACTIVE`` are therefore unreachable
for the loop by construction (roadmap Phase 11: no automatic promotion to ACTIVE).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

from apps.worker.jobs import JobOutcome, JobRunner, JobSpec
from apps.worker.journal import AppendOnlyJournal, JournalCorrupted, JournalEntry, JournalPath
from apps.worker.metrics import Clock, RoundMetrics, StageMetrics, monotonic_clock, seconds
from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.contracts.loop_audit import (
    EXTENDED_STAGE_ORDER,
    OPTIONAL_STAGES,
    STAGE_ORDER,
    LoopRoundRecorded,
    LoopRoundStarted,
    check_stage_order,
)
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
    "EXTENDED_STAGE_ORDER",
    "FORBIDDEN_TARGETS",
    "METRICS_TOPIC",
    "OPTIONAL_STAGES",
    "ROUND_JOB",
    "ROUND_RECORDED",
    "ROUND_STARTED",
    "ROUND_TOPIC",
    "STAGE_ORDER",
    "STAGE_TOPIC",
    "AutomationForbidden",
    "LifecycleGuard",
    "LoopAuditCorrupted",
    "LoopAuditLog",
    "LoopBudget",
    "LoopHalted",
    "LoopRecord",
    "LoopStage",
    "ResearchLoop",
    "RoundContext",
    "RoundStatus",
    "StageRecord",
    "StageFailed",
    "StageResult",
    "StageStatus",
    "StageUsage",
    "automation_reachable_states",
    "check_stage_order",
    "round_message",
]

# ``STAGE_ORDER`` / ``OPTIONAL_STAGES`` / ``EXTENDED_STAGE_ORDER`` / ``check_stage_order`` live in
# the audit contract (``core.contracts.loop_audit``, ADR-0050) and are re-exported here unchanged.

ROUND_JOB: Final = "research_loop.round"
JOB_TOPIC: Final = "research_loop.jobs"
STAGE_TOPIC: Final = "research_loop.stage"
ROUND_TOPIC: Final = "research_loop.round"
#: Side channel for measured stage time (never hashed into a ``LoopRecord``).
METRICS_TOPIC: Final = "research_loop.metrics"
#: Durable audit line types: a round is marked started before it runs, recorded after it.
ROUND_STARTED: Final = "loop_round_started"
ROUND_RECORDED: Final = "loop_round_recorded"

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

    def at_least(self, other: StageUsage) -> StageUsage:
        """Per dimension, the larger of this usage and ``other``."""
        return StageUsage(
            max(self.trials, other.trials),
            max(self.llm_cost_units, other.llm_cost_units),
            max(self.compute_seconds, other.compute_seconds),
        )

    def excess_over(self, other: StageUsage) -> StageUsage:
        """Per dimension, how much larger than ``other`` this usage is (zero where it is not)."""
        return StageUsage(
            max(self.trials - other.trials, 0),
            max(self.llm_cost_units - other.llm_cost_units, Decimal(0)),
            max(self.compute_seconds - other.compute_seconds, Decimal(0)),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "llm_cost_units": str(self.llm_cost_units),
            "compute_seconds": str(self.compute_seconds),
        }


class StageFailed(Exception):  # noqa: N818 - named after the stage status it produces
    """Raised by a stage that failed after spending ``usage`` (charged instead of the estimate)."""

    def __init__(self, message: str, *, usage: StageUsage) -> None:
        super().__init__(message)
        if not isinstance(usage, StageUsage):
            raise TypeError("usage must be a StageUsage")
        self.usage = usage


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
    def actor(self) -> str:
        """The automation identity written as ``triggered_by`` (never a human reviewer)."""
        return self._actor

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
    #: Actual minus declared usage (per dimension) when the stage overran its estimate.
    overrun: StageUsage | None = None
    #: What the budget was charged when it differs from ``usage`` (a completed stage is charged
    #: ``max(estimate, usage)`` per dimension); ``None``: exactly ``usage`` (or nothing).
    charged: StageUsage | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "estimate": None if self.estimate is None else self.estimate.payload(),
            "usage": None if self.usage is None else self.usage.payload(),
            "summary": None if self.summary is None else dict(self.summary),
            "error": self.error,
            "refused": list(self.refused),
            "overrun": None if self.overrun is None else self.overrun.payload(),
            "charged": None if self.charged is None else self.charged.payload(),
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

    @property
    def overrun(self) -> dict[str, Any] | None:
        """The stage that overran its declared usage and by how much (``None``: no overrun)."""
        for stage in self.stages:
            if stage.overrun is not None:
                return {"stage": stage.name, "amount": stage.overrun.payload()}
        return None

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
            "overrun": self.overrun,
            "previous_hash": self.previous_hash,
        }

    @property
    def record_hash(self) -> str:
        return content_hash(self.payload())


_USAGE_KEYS: Final = frozenset({"trials", "llm_cost_units", "compute_seconds"})
_STAGE_KEYS: Final = frozenset(
    {"name", "status", "estimate", "usage", "summary", "error", "refused", "overrun", "charged"}
)
_TRANSITION_KEYS: Final = frozenset({"subject", "from", "to", "reason", "evidence", "triggered_by"})
_RECORD_KEYS: Final = frozenset(
    {
        "loop_id",
        "round_index",
        "seed",
        "as_of",
        "budget_hash",
        "status",
        "stages",
        "transitions",
        "round_usage",
        "total_usage",
        "overrun",
        "previous_hash",
    }
)
_STARTED_KEYS: Final = frozenset({"loop_id", "round_index", "previous_hash"})
_RECORDED_KEYS: Final = frozenset({"record", "record_hash"})


def _fields(raw: Any, keys: frozenset[str], what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise ValueError(f"{what} does not have exactly the fields {sorted(keys)}")
    return raw


def _int(raw: Any, what: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{what} must be an int")
    return raw


def _str(raw: Any, what: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{what} must be a string")
    return raw


def _optional_str(raw: Any, what: str) -> str | None:
    return None if raw is None else _str(raw, what)


def _usage_from(raw: Any) -> StageUsage:
    fields = _fields(raw, _USAGE_KEYS, "a usage")
    return StageUsage(
        _int(fields["trials"], "trials"),
        Decimal(_str(fields["llm_cost_units"], "llm_cost_units")),
        Decimal(_str(fields["compute_seconds"], "compute_seconds")),
    )


def _optional_usage(raw: Any) -> StageUsage | None:
    return None if raw is None else _usage_from(raw)


def _stage_from(raw: Any) -> StageRecord:
    fields = _fields(raw, _STAGE_KEYS, "a stage record")
    summary = fields["summary"]
    if summary is not None and not isinstance(summary, Mapping):
        raise ValueError("a stage summary must be a JSON object")
    refused = fields["refused"]
    if not isinstance(refused, list):
        raise ValueError("refused must be a list")
    return StageRecord(
        name=_str(fields["name"], "stage name"),
        status=StageStatus(fields["status"]),
        estimate=_optional_usage(fields["estimate"]),
        usage=_optional_usage(fields["usage"]),
        summary=summary,
        error=_optional_str(fields["error"], "error"),
        refused=tuple(_str(item, "refused limit") for item in refused),
        overrun=_optional_usage(fields["overrun"]),
        charged=_optional_usage(fields["charged"]),
    )


def _transition_from(raw: Any, occurred_at: datetime) -> LifecycleTransition:
    fields = _fields(raw, _TRANSITION_KEYS, "a transition")
    evidence = fields["evidence"]
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    # the loop's transitions happen at the round's scheduled time and never carry an approval
    return LifecycleTransition(
        subject=Ref.parse(_str(fields["subject"], "subject")),
        from_state=LifecycleState(fields["from"]),
        to_state=LifecycleState(fields["to"]),
        reason=_str(fields["reason"], "reason"),
        evidence=tuple(_str(item, "evidence") for item in evidence),
        triggered_by=_str(fields["triggered_by"], "triggered_by"),
        approved_by=None,
        occurred_at=occurred_at,
    )


def _record_from(raw: Any) -> LoopRecord:
    """Rebuild a ``LoopRecord`` from its payload (the caller checks it reproduces the payload)."""
    fields = _fields(raw, _RECORD_KEYS, "a loop record")
    as_of = datetime.fromisoformat(_str(fields["as_of"], "as_of"))
    if as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
        raise ValueError("as_of must be UTC")
    stages, transitions = fields["stages"], fields["transitions"]
    if not isinstance(stages, list) or not isinstance(transitions, list):
        raise ValueError("stages and transitions must be lists")
    return LoopRecord(
        loop_id=_str(fields["loop_id"], "loop_id"),
        round_index=_int(fields["round_index"], "round_index"),
        seed=_int(fields["seed"], "seed"),
        as_of=as_of,
        budget_hash=_str(fields["budget_hash"], "budget_hash"),
        status=RoundStatus(fields["status"]),
        stages=tuple(_stage_from(stage) for stage in stages),
        transitions=tuple(_transition_from(t, as_of) for t in transitions),
        round_usage=_usage_from(fields["round_usage"]),
        total_usage=_usage_from(fields["total_usage"]),
        previous_hash=_optional_str(fields["previous_hash"], "previous_hash"),
    )


def _conform(
    model: type[LoopRoundStarted] | type[LoopRoundRecorded], payload: Any, what: str
) -> None:
    """Refuse (``ValueError``) a payload that is not a byte-identical instance of ``model``."""
    try:
        model.from_audit_payload(payload)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise ValueError(
            f"{what} does not conform to the {model.__name__} contract (ADR-0050): {exc}"
        ) from exc


class LoopAuditCorrupted(JournalCorrupted):
    """A durable audit whose lines chain correctly but do not form a valid loop history."""


class LoopAuditLog:
    """Append-only, hash-chained list of ``LoopRecord`` (no update or delete method).

    ``path=None`` (the default): in memory only. With a ``path``: durable (see module docs) —
    ``begin_round`` writes a ``loop_round_started`` line before a round runs and ``append`` a
    ``loop_round_recorded`` line after it, each fsync'd before the in-memory state changes, and
    opening an existing file replays and verifies it (``JournalCorrupted`` /
    ``LoopAuditCorrupted`` on any tampering, truncation or unknown line type).
    """

    def __init__(self, path: JournalPath | None = None) -> None:
        self._records: list[LoopRecord] = []
        self._loop_id: str | None = None
        self._open_round: int | None = None
        self._journal = None if path is None else AppendOnlyJournal(path)
        if self._journal is not None:
            for entry in self._journal.entries:
                self._replay(entry)

    @property
    def durable(self) -> bool:
        return self._journal is not None

    @property
    def records(self) -> tuple[LoopRecord, ...]:
        return tuple(self._records)

    @property
    def head(self) -> str | None:
        return self._records[-1].record_hash if self._records else None

    @property
    def loop_id(self) -> str | None:
        """The loop this audit belongs to (``None`` while nothing was written)."""
        return self._loop_id

    @property
    def open_round(self) -> int | None:
        """A round started but not recorded; after a replay, a round that was interrupted."""
        return self._open_round

    def begin_round(self, loop_id: str, round_index: int) -> None:
        """Mark ``round_index`` as started (durable: before the round spends anything)."""
        self._check_start(loop_id, round_index)
        started = {"loop_id": loop_id, "round_index": round_index, "previous_hash": self.head}
        _conform(LoopRoundStarted, started, "the round start")
        if self._journal is not None:
            self._journal.append(ROUND_STARTED, started)
        self._loop_id = loop_id
        self._open_round = round_index

    def append(self, record: LoopRecord) -> None:
        self._check_record(record)
        recorded = {"record": record.payload(), "record_hash": record.record_hash}
        # every record written (durable or not) must be a valid LoopRoundRecord (ADR-0050)
        _conform(LoopRoundRecorded, recorded, "the round record")
        if self._journal is not None:
            if self._open_round != record.round_index:
                raise ValueError("a durable audit records only a round begun with begin_round")
            self._journal.append(ROUND_RECORDED, recorded)
        self._admit(record)

    def verify(self) -> bool:
        previous: str | None = None
        for index, record in enumerate(self._records):
            if record.round_index != index or record.previous_hash != previous:
                return False
            previous = record.record_hash
        return True

    # -- internals ------------------------------------------------------------------------------

    def _check_loop(self, loop_id: str) -> None:
        if self._loop_id is not None and loop_id != self._loop_id:
            raise ValueError(f"the audit belongs to loop {self._loop_id!r}, not {loop_id!r}")

    def _check_start(self, loop_id: str, round_index: int) -> None:
        if self._open_round is not None:
            raise ValueError(f"round {self._open_round} was started and has no record")
        self._check_loop(loop_id)
        if round_index != len(self._records):
            raise ValueError(f"round {round_index} is out of order (next {len(self._records)})")

    def _check_record(self, record: LoopRecord) -> None:
        self._check_loop(record.loop_id)
        if record.round_index != len(self._records):
            raise ValueError(
                f"round {record.round_index} is out of order (next {len(self._records)})"
            )
        if record.previous_hash != self.head:
            raise ValueError("the record does not chain to the audit head")
        if self._open_round not in (None, record.round_index):
            raise ValueError(f"round {self._open_round} was started, not {record.round_index}")

    def _admit(self, record: LoopRecord) -> None:
        self._records.append(record)
        self._loop_id = record.loop_id
        self._open_round = None

    def _replay(self, entry: JournalEntry) -> None:
        where = f"{self._journal.path if self._journal else '?'}:{entry.seq}"
        try:
            if entry.type == ROUND_STARTED:
                started = _fields(entry.payload, _STARTED_KEYS, "a round start")
                _conform(LoopRoundStarted, started, "the round start")
                round_index = _int(started["round_index"], "round_index")
                self._check_start(_str(started["loop_id"], "loop_id"), round_index)
                if started["previous_hash"] != self.head:
                    raise ValueError("the round start does not chain to the audit head")
                self._loop_id, self._open_round = started["loop_id"], round_index
            elif entry.type == ROUND_RECORDED:
                recorded = _fields(entry.payload, _RECORDED_KEYS, "a round record")
                # integrity first (the stored hash), then the contract (ADR-0050), then rebuild
                if content_hash(recorded["record"]) != recorded["record_hash"]:
                    raise ValueError("the record does not reproduce its record_hash")
                _conform(LoopRoundRecorded, recorded, "the round record")
                record = _record_from(recorded["record"])
                if record.payload() != recorded["record"]:
                    raise ValueError("the record does not rebuild to its own payload")
                if record.record_hash != recorded["record_hash"]:
                    raise ValueError("the record does not reproduce its record_hash")
                if self._open_round != record.round_index:
                    raise ValueError("a round was recorded without being started")
                self._check_record(record)
                self._admit(record)
            else:
                raise ValueError(f"unknown audit line type {entry.type!r}")
        except (ArithmeticError, KeyError, LifecycleViolation, TypeError, ValueError) as exc:
            raise LoopAuditCorrupted(f"{where} is not a valid loop audit line: {exc}") from exc


# --------------------------------------------------------------------------- the loop


def round_message(loop_id: str, record: LoopRecord) -> BusMessage:
    """The ``research_loop.round`` message a loop publishes for a recorded round (a pure function
    of the audit record, so a bus can be checked against, or caught up from, the audit)."""
    return BusMessage.build(
        ROUND_TOPIC,
        f"{loop_id}:{record.round_index}",
        {"record_hash": record.record_hash, "record": record.payload()},
    )


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
        clock: Clock | None = None,
        compute_tolerance_seconds: Decimal | int | str | None = None,
        checkpoint: Callable[[LoopRecord], None] | None = None,
        after_record: Callable[[LoopRecord], None] | None = None,
    ) -> None:
        """``audit``: a ``LoopAuditLog`` (``LoopAuditLog(path)`` for a durable one); when it
        already holds rounds the loop continues it (see module docs). ``clock``: the stage timer
        (default ``monotonic_clock``; tests inject a fake). ``compute_tolerance_seconds``: how far
        a stage's measured time may exceed its declared compute seconds before the metrics flag
        it; ``None`` (no default) = report only. ``checkpoint``: called with every finished
        round's record before the audit records it (see module docs); ``None``: nothing.
        ``after_record``: called with every round's record right after the audit recorded it
        (e.g. to move an external anchor); if it raises, the round stays recorded and the loop
        stops (fail closed); ``None``: nothing."""
        check_stage_order([stage.name for stage in stages])
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
        self._audit = audit if audit is not None else LoopAuditLog()
        self._clock: Clock = clock or monotonic_clock
        self._checkpoint = checkpoint
        self._after_record = after_record
        self._tolerance = (
            None
            if compute_tolerance_seconds is None
            else _decimal(compute_tolerance_seconds, "compute_tolerance_seconds")
        )
        self._metrics: list[RoundMetrics] = []
        #: Why the loop stopped outside a halting round (interrupted round, audit write failure).
        self._stopped: str | None = None
        self._total = StageUsage()
        self._halted: RoundStatus | None = None
        self._restore()
        self._runner = JobRunner(
            bus,
            consumer=consumer,
            topic=JOB_TOPIC,
            handlers={ROUND_JOB: self._handle_round},
            max_attempts=1,
        )
        self._settle_recorded_jobs(consumer)

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

    @property
    def stopped(self) -> str | None:
        """Why the loop stopped outside a round's status (interrupted round, audit failure)."""
        return self._stopped

    @property
    def metrics(self) -> tuple[RoundMetrics, ...]:
        """Measured stage time of the rounds this process ran (unhashed side channel)."""
        return tuple(self._metrics)

    @property
    def compute_tolerance_seconds(self) -> Decimal | None:
        return self._tolerance

    def submit_round(self, round_index: int) -> str:
        self._check_can_run()
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

    def _check_can_run(self) -> None:
        if self._stopped is not None:
            raise LoopHalted(f"the loop stopped: {self._stopped}")
        if self._halted is not None:
            raise LoopHalted(f"the loop halted ({self._halted}); reconfigure it to continue")

    def _scheduled(self, round_index: int) -> datetime:
        return self._epoch + round_index * self._cadence

    def _restore(self) -> None:
        """Continue an audit that already holds rounds (a restart): take the next round index,
        the cumulative budget totals, the halting state and the guard's subjects from its
        verified records. Anything inconsistent with this loop's configuration is refused —
        including a record made under another ``LoopBudget`` (its ``budget_hash``): a budget is
        never changed on a running loop; a larger one is a human decision and a new audit /
        ``loop_id``."""
        audit = self._audit
        if audit.loop_id is not None and audit.loop_id != self._loop_id:
            raise ValueError(f"the audit belongs to loop {audit.loop_id!r}, not {self._loop_id!r}")
        if audit.open_round is not None:
            self._stopped = (
                f"round {audit.open_round} was started but never recorded (interrupted); what it "
                "spent is unknown, so the loop does not continue until a human reviews the audit"
            )
        records = audit.records
        if not records:
            return
        if not audit.verify():
            raise ValueError("the audit's hash chain does not verify")
        if self._guard.histories:
            raise ValueError("a loop continuing an audit rebuilds its guard; pass a fresh guard")
        total = StageUsage()
        for record in records:
            index = record.round_index
            if record.seed != _round_seed(self._seed, index) or record.as_of != self._scheduled(
                index
            ):
                raise ValueError(f"audit round {index} was scheduled with another seed / epoch")
            if record.budget_hash != self._budget.budget_hash:
                raise ValueError(
                    f"audit round {index} ran under another LoopBudget (budget_hash "
                    f"{record.budget_hash[:12]}, this loop's {self._budget.budget_hash[:12]}): a "
                    "budget is never changed on a running loop; raising it is a human decision "
                    "and takes a new audit / loop_id"
                )
            total = total + record.round_usage
            if total != record.total_usage:
                raise ValueError(f"audit round {index}'s total usage does not add up")
            if record.status in HALTING and record is not records[-1]:
                raise ValueError(f"audit round {index} halted the loop, yet rounds follow it")
            for transition in record.transitions:
                self._replay_transition(transition)
        self._total = total
        last = records[-1].status
        self._halted = last if last in HALTING else None

    def _settle_recorded_jobs(self, consumer: str) -> None:
        """Acknowledge this loop's pending round jobs whose round the audit already recorded (see
        module docs, **Durable bus**): the audit holds their result; they never run again."""
        recorded = len(self._audit.records)
        if recorded == 0:
            return
        done = {
            JobSpec(ROUND_JOB, {"loop_id": self._loop_id, "round": index})
            .message(JOB_TOPIC)
            .message_id
            for index in range(recorded)
        }
        batch_size = 100
        while True:
            batch = self._bus.poll(consumer, JOB_TOPIC, batch_size)
            settled = [message for message in batch if message.message_id in done]
            for message in settled:
                self._bus.ack(consumer, JOB_TOPIC, message.message_id)
            if not settled or len(batch) < batch_size:
                return

    def _replay_transition(self, transition: LifecycleTransition) -> None:
        subject = transition.subject
        if transition.from_state is S.IDEA and self._guard.state_of(subject) is None:
            self._guard.open(subject)
        replayed = self._guard.advance(
            subject,
            transition.to_state,
            reason=transition.reason,
            evidence=transition.evidence,
            occurred_at=transition.occurred_at,
        )
        if _transition_payload(replayed) != _transition_payload(transition):
            raise ValueError(
                f"audit transition of {subject} does not replay under guard {self._guard.actor!r}"
            )

    def _handle_round(self, params: Mapping[str, Any]) -> str:
        if params["loop_id"] != self._loop_id:
            raise ValueError("the job belongs to another loop")
        round_index = int(params["round"])
        if round_index != len(self._audit.records):
            raise ValueError(f"round {round_index} is out of order")
        self._check_can_run()
        try:
            self._audit.begin_round(self._loop_id, round_index)
            record, timings = self._run_round(round_index)
            if self._checkpoint is not None:
                self._checkpoint(record)  # the composition's state, before the audit names it
            self._audit.append(record)
        except Exception as exc:
            # the round may have spent budget and moved subjects without a durable record
            self._stopped = f"round {round_index} could not be recorded ({_error(exc)})"
            raise
        self._total = record.total_usage
        if record.status in HALTING:
            self._halted = record.status
        if self._after_record is not None:
            try:
                self._after_record(record)
            except Exception as exc:
                # the round is recorded; whatever the hook keeps (an anchor) is now behind it
                self._stopped = (
                    f"round {round_index} was recorded, but the after-record hook failed "
                    f"({_error(exc)}); the loop does not continue until a human reviews it"
                )
                raise
        try:
            self._bus.publish(round_message(self._loop_id, record))
        except Exception as exc:
            # the round is recorded; the bus is now one round behind the audit
            self._stopped = (
                f"round {round_index} was recorded, but publishing it on {ROUND_TOPIC} failed "
                f"({_error(exc)}); the loop does not continue until its bus is caught up"
            )
            raise
        metrics = RoundMetrics(
            loop_id=self._loop_id,
            round_index=round_index,
            record_hash=record.record_hash,
            tolerance_seconds=self._tolerance,
            stages=timings,
        )
        self._metrics.append(metrics)
        self._bus.publish(
            BusMessage.build(METRICS_TOPIC, f"{self._loop_id}:{round_index}", metrics.payload())
        )
        return record.record_hash

    def _run_round(self, round_index: int) -> tuple[LoopRecord, tuple[StageMetrics, ...]]:
        ctx = RoundContext(
            loop_id=self._loop_id,
            round_index=round_index,
            seed=_round_seed(self._seed, round_index),
            as_of=self._scheduled(round_index),
            _guard=self._guard,
        )
        round_usage = StageUsage()
        total = self._total
        records: list[StageRecord] = []
        timings: list[StageMetrics] = []
        status = RoundStatus.COMPLETED
        for stage in self._stages:
            if status is not RoundStatus.COMPLETED:
                records.append(StageRecord(stage.name, StageStatus.SKIPPED))
                continue
            record, spent, timing = self._run_stage(stage, ctx, round_usage, total)
            round_usage = round_usage + spent
            total = total + spent
            records.append(record)
            if timing is not None:
                timings.append(timing)
            self._publish_stage(ctx, record)
            status = {
                StageStatus.COMPLETED: RoundStatus.COMPLETED,
                StageStatus.FAILED: RoundStatus.FAILED,
                StageStatus.REFUSED_BUDGET: RoundStatus.BUDGET_EXHAUSTED,
                StageStatus.BUDGET_OVERRUN: RoundStatus.BUDGET_OVERRUN,
                StageStatus.GUARD_VIOLATION: RoundStatus.GUARD_VIOLATION,
            }[record.status]
        loop_record = LoopRecord(
            loop_id=self._loop_id,
            round_index=round_index,
            seed=ctx.seed,
            as_of=ctx.as_of,
            budget_hash=self._budget.budget_hash,
            status=status,
            stages=tuple(records),
            transitions=tuple(ctx.transitions),
            round_usage=round_usage,
            total_usage=total,
            previous_hash=self._audit.head,
        )
        return loop_record, tuple(timings)

    def _run_stage(
        self, stage: LoopStage, ctx: RoundContext, round_usage: StageUsage, total: StageUsage
    ) -> tuple[StageRecord, StageUsage, StageMetrics | None]:
        """Run one stage: its record, what the budget is charged, and its measured time
        (``None`` when ``run`` was never called: a failed estimate or a budget refusal)."""
        try:
            estimate = stage.estimate(ctx)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed stage, never dropped
            record = StageRecord(stage.name, StageStatus.FAILED, error=_error(exc))
            return record, StageUsage(), None
        refused = self._budget.refusals(round_usage, total, estimate)
        if refused:
            record = StageRecord(
                stage.name, StageStatus.REFUSED_BUDGET, estimate=estimate, refused=tuple(refused)
            )
            return record, StageUsage(), None
        failure: Exception | None = None
        started = self._clock()
        try:
            result = stage.run(ctx)
            content_hash(dict(result.summary))  # the summary must be canonical JSON
        except Exception as exc:  # noqa: BLE001 - classified below, never dropped
            failure = exc
        timing = self._timing(stage.name, estimate, started, self._clock())
        if isinstance(failure, AutomationForbidden):
            record = StageRecord(
                stage.name, StageStatus.GUARD_VIOLATION, estimate=estimate, error=_error(failure)
            )
            return record, estimate, timing
        if isinstance(failure, StageFailed):  # the stage reports what it spent before failing
            record = self._charged(stage.name, StageStatus.FAILED, estimate, failure.usage, failure)
            return record, failure.usage, timing
        if failure is not None:
            record = StageRecord(
                stage.name, StageStatus.FAILED, estimate=estimate, error=_error(failure)
            )
            return record, estimate, timing
        ctx.artifacts[stage.name] = result.artifacts
        record = self._charged(stage.name, StageStatus.COMPLETED, estimate, result.usage, None)
        # the reported usage is self-declared: never charge less than was declared up front
        charged = result.usage.at_least(estimate)
        return (
            replace(
                record,
                summary=result.summary,
                charged=None if charged == result.usage else charged,
            ),
            charged,
            timing,
        )

    def _timing(
        self,
        name: str,
        estimate: StageUsage,
        started: tuple[int, int],
        ended: tuple[int, int],
    ) -> StageMetrics:
        """Measured wall / CPU time of one ``run`` (unhashed; flagged only with a tolerance)."""
        wall = seconds(ended[0] - started[0])
        cpu = seconds(ended[1] - started[1])
        excess = max(wall, cpu) - estimate.compute_seconds
        return StageMetrics(
            name=name,
            declared_compute_seconds=estimate.compute_seconds,
            wall_seconds=wall,
            cpu_seconds=cpu,
            flagged=None if self._tolerance is None else excess > self._tolerance,
        )

    @staticmethod
    def _charged(
        name: str,
        status: StageStatus,
        estimate: StageUsage,
        usage: StageUsage,
        exc: Exception | None,
    ) -> StageRecord:
        """The record of a stage that spent ``usage``: an overrun halts, whatever the outcome."""
        overrun = usage.excess_over(estimate) if usage.exceeds(estimate) else None
        return StageRecord(
            name,
            StageStatus.BUDGET_OVERRUN if overrun is not None else status,
            estimate=estimate,
            usage=usage,
            error=None if exc is None else _error(exc),
            overrun=overrun,
        )

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
