"""The Phase 11 research-loop audit record as a versioned contract (ADR-0050; additive).

The worker (``apps/worker/loop.py``, ADR-0049) writes one audit record per round into a
hash-chained journal (``loop_round_started`` / ``loop_round_recorded`` lines), publishes it on the
event bus and, through ``research/reports``, as a ``research_loop_round`` report served by the
console. Those payloads existed before this contract, and their ``record_hash`` values are already
anchored (journals, checkpoints, report file names). This module therefore **describes the existing
bytes**; it does not redefine them:

- every model here mirrors one persisted payload shape field for field (no key added, renamed or
  dropped; the lifecycle transition keeps its ``from`` / ``to`` keys through aliases);
- ``audit_payload()`` rebuilds exactly that shape — **without** the ``schema_version`` envelope,
  which the persisted bytes have never carried — and ``from_audit_payload`` refuses any payload
  that does not round-trip byte-identically (canonical JSON) through the model;
- ``LoopRoundRecord.record_hash`` is the worker's rule unchanged: ``content_hash`` (SHA-256 of
  ``core.domain.base.canonical_json``) of the record payload. It is **not**
  ``Contract.content_hash()``, which includes the ``schema_version`` envelope (02-domain.md §3.1).

Strings are not stripped here (``str_strip_whitespace=False``): whitespace in the bytes is part of
the hashed bytes, so it is either accepted exactly as written or refused, never normalized. Numbers
the worker writes as ``str(Decimal)`` must be that canonical text (``"1E+3"`` yes, ``"1e3"`` no).

Invariants the models check (the ones the worker constructs; a payload breaking any is refused):

- stage names follow ``STAGE_ORDER`` with optional stages at their fixed place
  (``check_stage_order``; the worker checks the same list when a loop is built);
- stage / round statuses are the enum values; a stage's fields match its status (what a skipped,
  refused, failed, guard-violating, overrunning or completed stage carries); an overrun equals
  usage minus estimate; ``charged`` is ``max(estimate, usage)`` exactly when that differs from
  usage;
- the round status is the first non-completed stage's outcome, and every stage after it is
  ``SKIPPED``; the round ``overrun`` is the first overrunning stage's; ``round_usage`` is the sum of
  what each stage was charged; ``total_usage`` covers ``round_usage``;
- round 0 and only round 0 has no ``previous_hash`` (the audit chain starts at it);
- a recorded line's ``record_hash`` is the record's hash (recomputed, never trusted).

Cross-record rules (the chain, cumulative totals, the schedule, the budget binding, the guard
replay) stay in the worker's ``LoopAuditLog`` / ``ResearchLoop``: one record cannot see them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, StrictInt, StrictStr, model_validator

from core.domain.base import (
    ContentHash,
    Contract,
    FrozenMapping,
    Ref,
    canonical_json,
    content_hash,
)
from core.lifecycle.strategy import (
    ALLOWED_TRANSITIONS,
    HUMAN_APPROVAL_TRANSITIONS,
    LifecycleState,
)

__all__ = [
    "BUDGET_LIMITS",
    "EXTENDED_STAGE_ORDER",
    "OPTIONAL_STAGES",
    "ROUND_STATUS_OF_STAGE",
    "STAGE_ORDER",
    "LoopBudgetLimits",
    "LoopBudgetUsage",
    "LoopOverrun",
    "LoopRoundRecord",
    "LoopRoundRecorded",
    "LoopRoundStarted",
    "LoopRoundStatus",
    "LoopStageRecord",
    "LoopStageStatus",
    "LoopTransitionRecord",
    "check_stage_order",
]

# --------------------------------------------------------------------------- stage order

#: The fixed order of a round (roadmap Phase 11: 新数据 → 状态更新 → 假设 → 实验 → 验证 → 记忆).
STAGE_ORDER: Final[tuple[str, ...]] = (
    "ingest",
    "state",
    "hypothesis",
    "experiment",
    "validation",
    "memory",
)
#: Stages a composition may add; each has one fixed place in ``EXTENDED_STAGE_ORDER``.
OPTIONAL_STAGES: Final[frozenset[str]] = frozenset({"evolution"})
#: The required stages with every optional stage at its place (evolution: new variants of the best
#: earlier candidates, registered before this round's experiments so they are re-validated).
EXTENDED_STAGE_ORDER: Final[tuple[str, ...]] = (
    "ingest",
    "state",
    "hypothesis",
    "evolution",
    "experiment",
    "validation",
    "memory",
)


def check_stage_order(names: Sequence[str]) -> tuple[str, ...]:
    """``names`` must be ``STAGE_ORDER`` plus optional stages at their fixed places."""
    given = tuple(names)
    present = set(given) & OPTIONAL_STAGES
    expected = tuple(n for n in EXTENDED_STAGE_ORDER if n in STAGE_ORDER or n in present)
    if given != expected:
        raise ValueError(
            f"stages must be exactly {STAGE_ORDER} in order (optional {sorted(OPTIONAL_STAGES)} "
            f"at their place in {EXTENDED_STAGE_ORDER}), got {given}"
        )
    return given


# --------------------------------------------------------------------------- statuses

#: The budget limits a refused stage can name, in the order the worker checks them.
BUDGET_LIMITS: Final[tuple[str, ...]] = (
    "max_trials_per_round",
    "max_trials_total",
    "max_llm_cost_units",
    "max_compute_seconds",
)
BudgetLimit = Literal[
    "max_trials_per_round", "max_trials_total", "max_llm_cost_units", "max_compute_seconds"
]


class LoopStageStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REFUSED_BUDGET = "REFUSED_BUDGET"
    BUDGET_OVERRUN = "BUDGET_OVERRUN"
    GUARD_VIOLATION = "GUARD_VIOLATION"
    SKIPPED = "SKIPPED"


class LoopRoundStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    BUDGET_OVERRUN = "BUDGET_OVERRUN"
    GUARD_VIOLATION = "GUARD_VIOLATION"


_SS = LoopStageStatus
_RS = LoopRoundStatus

#: The round outcome a stage's status produces (``SKIPPED`` produces none: it follows the outcome).
ROUND_STATUS_OF_STAGE: Final[Mapping[LoopStageStatus, LoopRoundStatus]] = {
    _SS.COMPLETED: _RS.COMPLETED,
    _SS.FAILED: _RS.FAILED,
    _SS.REFUSED_BUDGET: _RS.BUDGET_EXHAUSTED,
    _SS.BUDGET_OVERRUN: _RS.BUDGET_OVERRUN,
    _SS.GUARD_VIOLATION: _RS.GUARD_VIOLATION,
}

# --------------------------------------------------------------------------- field types


def _decimal_text(value: str) -> str:
    """Canonical ``str(Decimal)`` text of a finite, non-negative number (the worker's encoding)."""
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{value!r} is not a decimal number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{value!r} must be a finite, non-negative number")
    if str(number) != value:
        raise ValueError(f"{value!r} is not the canonical text {str(number)!r}")
    return value


def _exact_text(value: str) -> str:
    """Non-empty text without surrounding whitespace (written from a stripped lifecycle object)."""
    if not value or value != value.strip():
        raise ValueError(f"{value!r} must be non-empty without leading / trailing whitespace")
    return value


def _ref_text(value: str) -> str:
    if str(Ref.parse(value)) != value:
        raise ValueError(f"{value!r} is not a canonical kind:name@version reference")
    return value


def _utc_text(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not an ISO-8601 datetime") from exc
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError(f"{value!r} must be UTC")
    if moment.isoformat() != value:
        raise ValueError(f"{value!r} is not the canonical isoformat {moment.isoformat()!r}")
    return value


DecimalText = Annotated[StrictStr, AfterValidator(_decimal_text)]
ExactText = Annotated[StrictStr, AfterValidator(_exact_text)]
RefText = Annotated[StrictStr, AfterValidator(_ref_text)]
UtcText = Annotated[StrictStr, AfterValidator(_utc_text)]
Count = Annotated[StrictInt, Field(ge=0)]

_Usage = tuple[int, Decimal, Decimal]
_ZERO: Final[_Usage] = (0, Decimal(0), Decimal(0))


def _add(a: _Usage, b: _Usage) -> _Usage:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _exceeds(a: _Usage, b: _Usage) -> bool:
    """True if any dimension of ``a`` is larger than in ``b`` (``StageUsage.exceeds``)."""
    return a[0] > b[0] or a[1] > b[1] or a[2] > b[2]


def _excess(a: _Usage, b: _Usage) -> _Usage:
    """Per dimension, how much larger than ``b`` ``a`` is (``StageUsage.excess_over``)."""
    return (max(a[0] - b[0], 0), max(a[1] - b[1], Decimal(0)), max(a[2] - b[2], Decimal(0)))


def _at_least(a: _Usage, b: _Usage) -> _Usage:
    """Per dimension, the larger of ``a`` and ``b`` (``StageUsage.at_least``)."""
    return (max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]))


# --------------------------------------------------------------------------- base


class _AuditContract(Contract):
    """A contract over bytes that already exist: exact round trip, no whitespace normalization."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", str_strip_whitespace=False, allow_inf_nan=False
    )

    def audit_payload(self) -> dict[str, Any]:
        """The persisted payload (never the ``schema_version`` envelope)."""
        raise NotImplementedError

    @classmethod
    def from_audit_payload(cls, payload: Any) -> Self:
        """Validate a persisted payload; it must round-trip byte-identically (canonical JSON)."""
        model = cls.model_validate(payload)
        try:
            same = canonical_json(model.audit_payload()) == canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the {cls.__name__} payload is not canonical JSON: {exc}") from exc
        if not same:
            raise ValueError(f"the payload does not round-trip byte-identically as {cls.__name__}")
        return model


# --------------------------------------------------------------------------- usage and budget


class LoopBudgetUsage(_AuditContract):
    """Declared, actual, charged or overrun usage of a stage or round (``StageUsage.payload``)."""

    trials: Count
    llm_cost_units: DecimalText
    compute_seconds: DecimalText

    def audit_payload(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "llm_cost_units": self.llm_cost_units,
            "compute_seconds": self.compute_seconds,
        }

    def values(self) -> _Usage:
        return (self.trials, Decimal(self.llm_cost_units), Decimal(self.compute_seconds))


class LoopBudgetLimits(_AuditContract):
    """The limits a loop runs under (``LoopBudget.payload``); records bind its ``budget_hash``."""

    max_trials_per_round: Count
    max_trials_total: Count
    max_llm_cost_units: DecimalText
    max_compute_seconds: DecimalText

    def audit_payload(self) -> dict[str, Any]:
        return {
            "max_trials_per_round": self.max_trials_per_round,
            "max_trials_total": self.max_trials_total,
            "max_llm_cost_units": self.max_llm_cost_units,
            "max_compute_seconds": self.max_compute_seconds,
        }

    @property
    def budget_hash(self) -> str:
        return content_hash(self.audit_payload())


def _usage(value: LoopBudgetUsage | None) -> dict[str, Any] | None:
    return None if value is None else value.audit_payload()


# --------------------------------------------------------------------------- stage record


class LoopStageRecord(_AuditContract):
    """One stage of a round (``StageRecord.payload``)."""

    name: Annotated[StrictStr, Field(min_length=1)]
    status: LoopStageStatus
    estimate: LoopBudgetUsage | None
    usage: LoopBudgetUsage | None
    summary: FrozenMapping[str, Any] | None
    error: Annotated[StrictStr, Field(min_length=1)] | None
    refused: tuple[BudgetLimit, ...]
    overrun: LoopBudgetUsage | None
    charged: LoopBudgetUsage | None

    def audit_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "estimate": _usage(self.estimate),
            "usage": _usage(self.usage),
            "summary": self.model_dump(mode="json", include={"summary"})["summary"],
            "error": self.error,
            "refused": list(self.refused),
            "overrun": _usage(self.overrun),
            "charged": _usage(self.charged),
        }

    def spent(self) -> _Usage:
        """What the budget was charged for this stage (the worker's accounting)."""
        if self.status in (_SS.SKIPPED, _SS.REFUSED_BUDGET):
            return _ZERO
        for usage in (self.charged, self.usage, self.estimate):
            if usage is not None:
                return usage.values()
        return _ZERO  # an estimate that raised: nothing was declared, nothing was run

    @model_validator(mode="after")
    def _fields_match_status(self) -> Self:
        status, where = self.status, f"stage {self.name!r} ({self.status.value})"
        present = {
            key
            for key in ("estimate", "usage", "summary", "error", "overrun", "charged")
            if getattr(self, key) is not None
        } | ({"refused"} if self.refused else set())
        allowed: dict[LoopStageStatus, tuple[set[str], set[str]]] = {
            # status: (required, allowed)
            _SS.SKIPPED: (set(), set()),
            _SS.REFUSED_BUDGET: ({"estimate", "refused"}, {"estimate", "refused"}),
            _SS.GUARD_VIOLATION: ({"estimate", "error"}, {"estimate", "error"}),
            _SS.FAILED: ({"error"}, {"estimate", "usage", "error"}),
            _SS.COMPLETED: (
                {"estimate", "usage", "summary"},
                {"estimate", "usage", "summary", "charged"},
            ),
            _SS.BUDGET_OVERRUN: (
                {"estimate", "usage", "overrun"},
                {"estimate", "usage", "overrun", "summary", "error", "charged"},
            ),
        }
        required, permitted = allowed[status]
        if not required <= present or not present <= permitted:
            raise ValueError(
                f"{where} must carry {sorted(required)} and may carry only {sorted(permitted)}, "
                f"carries {sorted(present)}"
            )
        if list(self.refused) != [limit for limit in BUDGET_LIMITS if limit in self.refused]:
            raise ValueError(f"{where}: refused limits must be unique, in {BUDGET_LIMITS} order")
        if self.usage is not None and self.estimate is None:
            raise ValueError(f"{where}: a usage needs the estimate it is compared with")
        if status is _SS.BUDGET_OVERRUN and (self.summary is None) == (self.error is None):
            raise ValueError(f"{where}: an overrun carries either a summary or an error")
        if self.usage is not None and self.estimate is not None:
            usage, estimate = self.usage.values(), self.estimate.values()
            if _exceeds(usage, estimate) != (status is _SS.BUDGET_OVERRUN):
                raise ValueError(f"{where}: BUDGET_OVERRUN iff the usage exceeds the estimate")
            if self.overrun is not None and self.overrun.values() != _excess(usage, estimate):
                raise ValueError(f"{where}: the overrun must be usage minus estimate")
            if self.summary is not None:  # a stage that ran to its end: charged max(declared, used)
                floor = _at_least(usage, estimate)
                expected = None if floor == usage else floor
                charged = None if self.charged is None else self.charged.values()
                if charged != expected:
                    raise ValueError(f"{where}: charged must be max(estimate, usage) when larger")
            elif self.charged is not None:
                raise ValueError(f"{where}: only a stage that ran to its end carries charged")
        return self


# --------------------------------------------------------------------------- transitions


class LoopTransitionRecord(_AuditContract):
    """A lifecycle transition the loop's guard made (no approval; at the round's ``as_of``)."""

    subject: RefText
    from_state: LifecycleState = Field(alias="from")
    to_state: LifecycleState = Field(alias="to")
    reason: ExactText
    evidence: tuple[ExactText, ...] = Field(min_length=1)
    triggered_by: ExactText

    def audit_payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "from": self.from_state.value,
            "to": self.to_state.value,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "triggered_by": self.triggered_by,
        }

    @model_validator(mode="after")
    def _automatable_edge(self) -> Self:
        edge = (self.from_state, self.to_state)
        if edge not in ALLOWED_TRANSITIONS:
            raise ValueError(f"{edge} is not a lifecycle transition")
        if edge in HUMAN_APPROVAL_TRANSITIONS:
            raise ValueError(f"{edge} needs a human approval; the loop records none")
        return self


class LoopOverrun(_AuditContract):
    """The round's ``overrun``: the first stage that overran its declaration, and by how much."""

    stage: Annotated[StrictStr, Field(min_length=1)]
    amount: LoopBudgetUsage

    def audit_payload(self) -> dict[str, Any]:
        return {"stage": self.stage, "amount": self.amount.audit_payload()}


# --------------------------------------------------------------------------- round record


class LoopRoundRecord(_AuditContract):
    """The audit record of one round (``LoopRecord.payload``; hash-chained by ``previous_hash``)."""

    loop_id: StrictStr
    round_index: Count
    seed: Count
    as_of: UtcText
    budget_hash: ContentHash
    status: LoopRoundStatus
    stages: tuple[LoopStageRecord, ...]
    transitions: tuple[LoopTransitionRecord, ...]
    round_usage: LoopBudgetUsage
    total_usage: LoopBudgetUsage
    overrun: LoopOverrun | None
    previous_hash: ContentHash | None

    def audit_payload(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "round_index": self.round_index,
            "seed": self.seed,
            "as_of": self.as_of,
            "budget_hash": self.budget_hash,
            "status": self.status.value,
            "stages": [stage.audit_payload() for stage in self.stages],
            "transitions": [t.audit_payload() for t in self.transitions],
            "round_usage": self.round_usage.audit_payload(),
            "total_usage": self.total_usage.audit_payload(),
            "overrun": None if self.overrun is None else self.overrun.audit_payload(),
            "previous_hash": self.previous_hash,
        }

    @property
    def record_hash(self) -> str:
        """The worker's record hash: ``content_hash`` of the payload (no envelope)."""
        return content_hash(self.audit_payload())

    @model_validator(mode="after")
    def _round_invariants(self) -> Self:
        check_stage_order([stage.name for stage in self.stages])
        status = _RS.COMPLETED
        for stage in self.stages:
            if status is not _RS.COMPLETED:
                if stage.status is not _SS.SKIPPED:
                    raise ValueError(f"stage {stage.name!r} follows a {status} outcome: SKIPPED")
                continue
            if stage.status is _SS.SKIPPED:
                raise ValueError(f"stage {stage.name!r} is SKIPPED, yet nothing stopped the round")
            status = ROUND_STATUS_OF_STAGE[stage.status]
        if self.status is not status:
            raise ValueError(f"the round status must be {status} (its stages), not {self.status}")
        first = next((stage for stage in self.stages if stage.overrun is not None), None)
        expected = None if first is None else (first.name, first.overrun)
        actual = None if self.overrun is None else (self.overrun.stage, self.overrun.amount)
        if expected != actual:
            raise ValueError("the round overrun must be the first overrunning stage's")
        spent = _ZERO
        for stage in self.stages:
            spent = _add(spent, stage.spent())
        if self.round_usage.values() != spent:
            raise ValueError("round_usage must be the sum of what each stage was charged")
        if _exceeds(self.round_usage.values(), self.total_usage.values()):
            raise ValueError("total_usage must cover round_usage")
        if (self.round_index == 0) != (self.previous_hash is None):
            raise ValueError("round 0, and only round 0, starts the chain (previous_hash null)")
        return self


class LoopRoundStarted(_AuditContract):
    """A ``loop_round_started`` journal line: a round is about to spend budget."""

    loop_id: StrictStr
    round_index: Count
    previous_hash: ContentHash | None

    def audit_payload(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "round_index": self.round_index,
            "previous_hash": self.previous_hash,
        }

    @model_validator(mode="after")
    def _chain_start(self) -> Self:
        if (self.round_index == 0) != (self.previous_hash is None):
            raise ValueError("round 0, and only round 0, starts the chain (previous_hash null)")
        return self


class LoopRoundRecorded(_AuditContract):
    """A ``loop_round_recorded`` journal line: the record and its (recomputed) hash."""

    record: LoopRoundRecord
    record_hash: ContentHash

    def audit_payload(self) -> dict[str, Any]:
        return {"record": self.record.audit_payload(), "record_hash": self.record_hash}

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        if self.record_hash != self.record.record_hash:
            raise ValueError("record_hash is not the content hash of the record payload")
        return self
