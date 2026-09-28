"""One state directory for the whole research loop (ADR-0049 implementation note, durable
composition, 2026-09-26).

``open_synthetic_loop(config, state_dir=...)`` (``research/loop/compose.py``) keeps every stateful
part of the composed loop in one directory, so a process can die between rounds and a new one
continues exactly where it stopped:

=====================  ==========================================================================
``audit.jsonl``        ``apps.worker.LoopAuditLog`` — the hash-chained round records
``memory.jsonl``       this module — a header (``loop_state_opened``: the configuration
                       fingerprint), one ``round_memory`` checkpoint per recorded round and one
                       ``between_rounds`` checkpoint per human approval made between rounds
``trial_ledger.jsonl`` ``TrialLedger(path)`` — registrations and pre-registered re-evaluations
``sealed_oos.jsonl``   ``DurableUnsealingLedger`` — the sealed-OOS unsealings and evaluations
``lineage.jsonl``      ``LineageGraph(path=...)`` — every strategy spec the loop evolved from / into
``reviews.jsonl``      ``ReviewQueue(path)`` — LLM drafts, human approvals, drafts taken
``failures.jsonl``     ``FailureRegistry`` — FailureRecords (append-only, fsync'd, not chained)
=====================  ==========================================================================

Every file but ``failures.jsonl`` is an ``AppendOnlyJournal`` (``research.persistence`` /
``apps.worker.journal``): each verifies its own hash chain on open.

**Checkpoint.** ``ResearchLoop(checkpoint=...)`` calls ``MemoryCheckpoint`` with every finished
round's record **before** the audit records it. The checkpoint line holds

- ``round_index`` and ``record_hash``: the audit record it belongs to;
- ``heads``: the position of every other file at the end of the round — ``{"seq", "hash"}`` of each
  journal (entry count and chain head) and ``{"count", "digest"}`` of the failure registry
  (``content_hash`` of the record hashes);
- ``delta``: what the round added to the in-memory ``ResearchMemory`` that the stages read in
  later rounds — market specs (the market is regenerated on restore and must reproduce its
  ``market_hash``), research pieces, strategies added to the catalog (offspring), trial and
  validation outcomes (their pydantic records; the in-memory ``inputs`` / ``trial`` run artifacts
  are not kept — a restored ``TrialOutcome`` keeps ``knowledge_cutoff``), the experiment / state /
  offspring summaries.

**Cross-checks on reopening** (``LoopStateInconsistent`` on any failure; nothing is repaired):

1. configuration: the header's fingerprint equals this configuration's (loop id, seed, epoch,
   exact cadence, family, Profile, market spec, strategy catalog, knowledge, feature / state /
   label / cost specs, Constitution version, code commit, environment lock, evolution on / off,
   the loop budget and the sealed-OOS unseal budget); the refusal names the fields that differ; a
   missing header while any other file holds state is refused;
2. no interrupted round: an audit round started but never recorded is refused (what it spent is
   unknown; a human reviews it);
3. audit ↔ checkpoint: exactly one checkpoint per recorded round, same index and record hash;
4. positions: every checkpoint's (round or between-rounds) position of every journal exists in
   that journal (same sequence number and chain hash), positions never go back, and the last
   checkpoint's position is the journal's end (for ``reviews.jsonl`` too: an approval after the
   last round is named by its between-rounds checkpoint); the failure registry's count / digest
   likewise; every between-rounds checkpoint follows its round (count and audit head), keeps every
   other file where the previous checkpoint left it and moves ``reviews.jsonl`` by exactly the one
   approval it names; every approval line of ``reviews.jsonl`` is named by a between-rounds
   checkpoint (see **Approvals between rounds**);
5. content: each round's restored delta equals what the audit record's hashed stage summaries say
   (ingest market and spec hash, the state summary, every experiment row = its trial's summary,
   every validation report row, every offspring row), trial / validation / hypothesis / report
   records reproduce their content hashes, and every catalog strategy the delta names rebuilds
   from its parent;
6. audit ↔ ledgers: every hypothesis the audit registered or re-evaluated is in the trial ledger
   (the re-evaluation under its attempt), and so is every conditional cell hypothesis an
   experiment row registered (opt-in ``ConditionalPlan``; under the row's attempt) or a
   validation row validated (``conditional_cells``, ``validate_cells=True``); every
   offspring's child and parent spec is in the lineage
   with the recorded spec hash; every ``human_review:<who>`` evidence has that human's approval of
   that draft in the review queue, taken; every unsealed / consumed sealed-OOS report has the
   family's unsealing by the same approver, marked evaluated; every failure record hash the audit
   lists is in the failure registry;
7. after the loop replayed the audit into its guard: every lifecycle subject is a registered
   hypothesis (v4: also checked by ``open_state`` itself, on a pure replay of every audited
   transition through a fresh guard of the loop actor, before any admission recovery write);
8. with an anchor: every file (the review journal included) is at or after its anchored position
   with the same line there, and the directory is at or after the anchored head with the same
   history up to it (see **External anchor**).

**Tail truncation.** Deleting whole trailing lines of one journal leaves a valid shorter chain
(the journal alone cannot tell). Across files it is detected: the checkpoint names the position of
every other file (a shorter ledger / lineage / vault / failure registry is *behind* its recorded
position) and the audit names the checkpoints (a shorter audit or memory file no longer pairs one
checkpoint per record).

**External anchor** (ADR-0049 implementation note, durable review fixes, 2026-09-26). Truncating
*every* file consistently back to an earlier round boundary yields a valid, shorter history: the
directory alone cannot tell, and those rounds could then run again (re-register trials, re-spend
the budget, re-unseal the sealed OOS window). An optional ``StateAnchor`` outside the directory
closes that: after every recorded round (and every between-rounds approval) it receives the
directory's ``StateHead`` — the number of recorded rounds, the audit head (last ``record_hash``),
the memory journal's length and chain head, and every other file's position at that line (the
checkpoint's ``heads``). On reopening with an anchor, every file must be at or after its anchored
position with the same line there, and the directory **at or after** the anchored head with the
**same history up to it** (that round's record hash, the memory journal line at that position and
its positions); behind it (rolled back) or diverged is refused (``LoopStateInconsistent``), and
so is an anchor that holds no head while the directory already holds recorded rounds (the anchor
was lost, or attached to a running directory: a human decides). After the checks the anchor is moved
up to the directory's head (a round recorded while the anchor could not be updated is accepted
once, then anchored). ``FileAnchor(path)`` keeps the heads in a hash-chained journal file, which
must lie outside the state directory; any object with ``load`` / ``publish`` (e.g. one that
publishes on a bus or to another host) works. **Without an anchor** the behaviour is unchanged and
the limit stands: a consistent truncation opens as the shorter history.

**Approvals between rounds** (ADR-0049 implementation note, approvals between rounds,
2026-09-26). A human approval is the one legitimate write between rounds. ``open_state`` binds the
``DurableState`` as the review queue's observer, so every ``ReviewQueue.approve`` on the restored
memory (``DurableLoop.memory.reviews.approve``) is refused while a round is running, and once
journaled immediately writes a ``between_rounds`` line to ``memory.jsonl`` — the recorded round
count, the audit head, every file's position (``heads``) and the approval it covers (its review
journal line: ``seq`` / ``hash``, key, reviewer) — and publishes the new ``StateHead`` to the
anchor (``memory_seq`` grows by one, ``rounds`` stays). So on reopening:

- every approval line of ``reviews.jsonl`` must be named by a between-rounds checkpoint: an
  approval appended without one (forged, or the process died between approving and checkpointing,
  or made while a round ran) is refused, with or without an anchor; a round checkpoint refuses too
  (the round is not recorded) when an approval of the running process was never checkpointed;
- dropping the approval alone leaves its checkpoint pointing past the end of ``reviews.jsonl``:
  refused;
- with an anchor, dropping the approval **and** its checkpoint line (a consistent truncation of
  the between-rounds action) leaves ``reviews.jsonl`` (and ``memory.jsonl``) behind the anchor:
  refused.

Nothing else is written between rounds: the sealed-OOS approvals are configuration (the
``OosUnsealBudget`` in the fingerprint, i.e. the anchored header), and the unsealing ledger, trial
ledger, lineage and failure registry grow only inside rounds, so any line they hold beyond the last
checkpoint is refused whether or not a between-rounds line names it.

**What stays undetectable without an anchor** (and what no anchor can tell): dropping an approval
together with its between-rounds line (and nothing after them) is a valid shorter history — it
opens as "not approved yet" (the consistent-truncation limit, one action wide); and anyone who can
write the directory and follows the format (``ReviewQueue.approve`` on a state opened by
``open_state``, or the same lines by hand) can add an approval with its checkpoint — the journals
are hash chains, not signatures, so a reviewer identity is an assertion. An anchor turns the first
into a refusal (the directory is behind it); the second, like a forged whole round, is ahead of the
anchor and accepted — it needs an authenticated approval channel (not in scope).

**Budgets are part of the configuration.** The fingerprint binds the ``LoopBudget`` and the whole
sealed-OOS ``OosUnsealBudget`` (``max_unsealings``, every approved family and its approver), so
reopening a directory with a larger (or any other) budget or unseal quota is refused: raising a
budget is a human decision and takes a **new** ``state_dir`` or ``loop_id``.

**Typed-plan admission lease** (ADR-0073 §1, 2026-09-28). A v4 / v5 admission is one scope,
``with state.plan_admission() as lease: lease.prepare(...); lease.complete()``, whose lease covers
the whole PREPARE → TrialLedger batch event → COMMIT → admission checkpoint → anchor sequence. While
it is held, this state refuses every other in-process write it coordinates: the TrialLedger's own
mutation entries (its write lease: only the lease's batch event is written), round and
between-rounds checkpoints, human approvals, anchor moves and a second lease (``LoopStateLocked``);
``state.lock`` still excludes other processes. A scope that ends — by an exception, an interrupt or
without ``complete()`` — after any admission byte was written marks the state and its TrialLedger as
refusing every later write (``LoopStateInconsistent`` / ``LedgerError``) and releases the in-process
locks: the journals stay exactly where they stopped, and closing the loop and reopening the
directory recovers the transaction as ADR-0073 §4 prescribes. A scope that wrote nothing releases
cleanly. The lease does not cover the sealed-OOS, lineage or failure stores: nothing but
``prepare`` / ``complete`` may run inside the scope, and a write to them during it leaves an
admission the reopening refuses rather than recovers.

**One admission gate** (ADR-0073 admission lease review, 2026-09-28). Every in-process write this
state coordinates runs with the same re-entrant gate held, and each checks the lease / poison
before writing anything: acquiring a lease; a round start and, as one span, a finished round's
checkpoint → audit record → after-record anchor move (``round_scope``, which the composition hands
to ``ResearchLoop``); a human approval from ``before_approval`` through its review line, its
in-memory admission and its between-rounds checkpoint / anchor move; a review enqueue or take
(``review_scope`` / ``before_review_write``); and each ``prepare`` / ``complete``. So a lease cannot
start between an approval's line and its checkpoint, nor between a round's checkpoint and its audit
record (it would see the checkpointed round still open). A lease is bound to the thread that
entered its scope and claimed exclusively per call; ``complete`` is claimed once before its first
write (``PlanAdmissionLease``). Lock order is always gate → TrialLedger lock; the ledger never calls
back into the gate. The TrialLedger's journal is read only through its snapshot / head API.

The LLM provider is external: its own state (e.g. a scripted provider's position) is not loop
state and is the caller's to resume.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock, get_ident
from typing import Any, Final, Protocol

from apps.worker.loop import (
    LifecycleGuard,
    LoopAuditLog,
    LoopRecord,
    StageStatus,
    loop_actor,
    replay_transition,
)
from core.contracts.synthetic import SyntheticMarketProvider, SyntheticMarketSpec
from core.domain.base import FrozenMapping, canonical_json, content_hash
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    FailureRecord,
    Hypothesis,
    ValidationReport,
)
from core.domain.specs import StrategySpec
from core.errors import LifecycleViolation, ReasonCode
from research.evolution import LineageGraph
from research.hypotheses import LedgerError, LedgerJournalSnapshot, LedgerLease, TrialLedger
from research.hypotheses.typed_plan_audit import (
    CommittedAdmission,
    PlanAdmissionCorrupted,
    PlanAdmissionError,
    PlanAdmissionEvidence,
    PlanAdmissionJournal,
    PreparedAdmission,
    RoundStartedIdentity,
)
from research.hypotheses.typed_plan import TypedPlan
from research.loop.memory import REVIEW_APPROVED, ResearchMemory, ReviewApproval, ReviewQueue
from research.loop.segment import ResearchPiece
from research.loop.trials import TrialOutcome, ValidationOutcome
from research.persistence import GENESIS_HASH, AppendOnlyJournal, JournalEntry
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import StrategyCandidate
from research.validation.pipeline import CONSUMED_WITHOUT_RESULT
from research.validation.sealed_oos import DurableUnsealingLedger

__all__ = [
    "ANCHOR_HEAD",
    "AUDIT_FILE",
    "BETWEEN_ROUNDS",
    "FAILURES_FILE",
    "LEDGER_FILE",
    "LINEAGE_FILE",
    "LOOP_STATE_OPENED",
    "MEMORY_FILE",
    "PLAN_ADMISSION",
    "PLAN_ADMISSION_FILE",
    "OPERATOR_STATE_VERSION",
    "REVIEWS_FILE",
    "ROUND_MEMORY",
    "SEALED_OOS_FILE",
    "STATE_VERSION",
    "DurableState",
    "FileAnchor",
    "LoopStateInconsistent",
    "MemoryCheckpoint",
    "PlanAdmissionLease",
    "StateAnchor",
    "StateHead",
    "open_state",
]

AUDIT_FILE: Final = "audit.jsonl"
MEMORY_FILE: Final = "memory.jsonl"
LEDGER_FILE: Final = "trial_ledger.jsonl"
PLAN_ADMISSION_FILE: Final = "plan_admission.jsonl"
SEALED_OOS_FILE: Final = "sealed_oos.jsonl"
LINEAGE_FILE: Final = "lineage.jsonl"
REVIEWS_FILE: Final = "reviews.jsonl"
FAILURES_FILE: Final = "failures.jsonl"

#: Memory journal line types.
LOOP_STATE_OPENED: Final = "loop_state_opened"
ROUND_MEMORY: Final = "round_memory"
BETWEEN_ROUNDS: Final = "between_rounds"
PLAN_ADMISSION: Final = "plan_admission"
#: Anchor journal line type (``FileAnchor``).
ANCHOR_HEAD: Final = "loop_state_head"
#: Current memory/checkpoint layout. v3 remains a read/write compatibility path with its original
#: checkpoint shape; v4 adds typed-plan admission and operator-only v5 binds operator identity.
LEGACY_STATE_VERSION: Final = 3
STATE_VERSION: Final = 4
OPERATOR_STATE_VERSION: Final = 5
_OPERATOR_IDENTITY_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")

#: Fingerprint fields that are budgets (a change is a human decision: a new directory).
_BUDGET_FIELDS: Final = {
    "budget": "the loop budget (LoopBudget)",
    "oos_unseal": "the sealed-OOS unseal budget (OosUnsealBudget: max_unsealings, approved "
    "families and their approvers)",
}

#: The journals a checkpoint positions (name -> file), in a fixed order.
_JOURNALS: Final = (
    ("trial_ledger", LEDGER_FILE),
    ("sealed_oos", SEALED_OOS_FILE),
    ("lineage", LINEAGE_FILE),
    ("reviews", REVIEWS_FILE),
)
_ROUND_KEYS: Final = frozenset({"round_index", "record_hash", "heads", "delta"})
_BETWEEN_KEYS: Final = frozenset({"rounds", "audit_head", "heads", "action"})
_ADMISSION_KEYS: Final = frozenset(
    {
        "round", "transaction_id", "prepare_seq", "prepare_hash", "commit_seq",
        "commit_hash", "ledger_event_seq", "ledger_event_hash", "heads",
    }
)
_ACTION_KEYS: Final = frozenset({"type", "seq", "hash", "key", "reviewer"})
_DELTA_KEYS: Final = frozenset(
    {
        "markets",
        "research_data",
        "strategies",
        "trials",
        "validations",
        "experiments",
        "states",
        "offspring",
    }
)
_UNSEALED: Final = frozenset({"unsealed", CONSUMED_WITHOUT_RESULT})


class LoopStateInconsistent(RuntimeError):
    """The files of a loop state directory disagree with each other or with the configuration."""


class LoopStateLocked(RuntimeError):
    """Another live process (or object) holds this loop state directory (single writer)."""


#: The single-writer lock of a state directory (``fcntl.flock``; the kernel drops it on exit).
LOCK_FILE: Final = "state.lock"


class StateLock:
    """The held ``state.lock`` of one opened directory; ``release`` is idempotent."""

    def __init__(self, root: Path) -> None:
        fd = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise LoopStateLocked(f"{root} is held by another loop (single writer)") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class _AdmissionGate:
    """In-process admission exclusion of one opened directory (module docs, **Typed-plan
    admission lease**): ``owner`` is the active ``PlanAdmissionLease``; ``poisoned`` says why the
    state refuses every later write after an interrupted admission."""

    __slots__ = ("lock", "owner", "poisoned")

    def __init__(self) -> None:
        self.lock = RLock()
        self.owner: object | None = None
        self.poisoned: str | None = None

    def require_open(self, what: str, lease: object | None = None) -> None:
        """Refuse ``what`` after an interrupted admission, or while a lease other than ``lease``
        is active (the caller holds ``lock``)."""
        if self.poisoned is not None:
            raise LoopStateInconsistent(
                f"{what} is refused: {self.poisoned}; close this loop and reopen its state "
                "directory, which recovers the admission exactly (ADR-0073 §4)"
            )
        if self.owner is not None and lease is not self.owner:
            raise LoopStateLocked(f"{what} is refused: a typed-plan admission lease is active")

    def require_owner(self, what: str, lease: object | None) -> None:
        """Require ``lease`` to be the active lease."""
        self.require_open(what, lease)
        if lease is None or lease is not self.owner:
            raise LoopStateLocked(f"{what} requires this state's active typed-plan admission lease")


def _admission_file_sizes(root: Path) -> tuple[int, ...]:
    """On-disk sizes of the files an admission appends to (``-1``: missing); partial writes count."""
    sizes: list[int] = []
    for name in (PLAN_ADMISSION_FILE, LEDGER_FILE, MEMORY_FILE):
        try:
            sizes.append(os.stat(root / name).st_size)
        except FileNotFoundError:
            sizes.append(-1)
    return tuple(sizes)


# ------------------------------------------------------------------------------------ positions


def _other_journals(
    memory: ResearchMemory, admission: PlanAdmissionJournal | None
) -> dict[str, AppendOnlyJournal | PlanAdmissionJournal]:
    """Every positioned journal but the TrialLedger's (which is only read through its snapshot /
    head API: the ledger never hands out its writable journal)."""
    reviews = memory.reviews.journal
    graph, oos = memory.lineage_graph, memory.oos_ledger
    if (
        not memory.ledger.durable
        or reviews is None
        or graph is None
        or graph.journal is None
        or not isinstance(oos, DurableUnsealingLedger)
    ):
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    journals: dict[str, AppendOnlyJournal | PlanAdmissionJournal] = {
        "sealed_oos": oos.journal,
        "lineage": graph.journal,
        "reviews": reviews,
    }
    if admission is not None:
        journals["plan_admission"] = admission
    return journals


def _ledger_snapshot(memory: ResearchMemory) -> LedgerJournalSnapshot:
    snapshot = memory.ledger.journal_snapshot()
    if snapshot is None:
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    return snapshot


def _journals(
    memory: ResearchMemory, admission: PlanAdmissionJournal | None = None
) -> dict[str, AppendOnlyJournal | PlanAdmissionJournal | LedgerJournalSnapshot]:
    """Every positioned journal, read-only use: the TrialLedger as a detached snapshot."""
    others = _other_journals(memory, admission)
    journals: dict[str, AppendOnlyJournal | PlanAdmissionJournal | LedgerJournalSnapshot] = {
        "trial_ledger": _ledger_snapshot(memory)
    }
    journals.update(others)
    return journals


def _failure_hashes(records: Sequence[FailureRecord]) -> list[str]:
    return [record.content_hash() for record in records]


def heads(
    memory: ResearchMemory, admission: PlanAdmissionJournal | None = None
) -> dict[str, Any]:
    """The position of every file of the state directory but the audit and the memory journal."""
    others = _other_journals(memory, admission)
    ledger = memory.ledger.journal_head()
    if ledger is None:
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    out: dict[str, Any] = {"trial_ledger": {"seq": ledger[0], "hash": ledger[1]}}
    out.update(
        {
            name: {"seq": len(journal.entries), "hash": journal.head_hash}
            for name, journal in others.items()
        }
    )
    hashes = _failure_hashes(memory.failures.records())
    out["failures"] = {"count": len(hashes), "digest": content_hash(hashes)}
    return out


def _checkpointed_heads(
    entries: Sequence[JournalEntry], admission: PlanAdmissionJournal
) -> dict[str, Any]:
    """Where the memory journal's last line leaves every other file of a v4 directory.

    The header names no positions; for it this is the v4 genesis: every journal empty but the
    plan admission journal's header line, no failure records.
    """
    last = entries[-1]
    if last.type != LOOP_STATE_OPENED:
        return _json(dict(last.payload["heads"]))
    out: dict[str, Any] = {name: {"seq": 0, "hash": GENESIS_HASH} for name, _ in _JOURNALS}
    out["plan_admission"] = {"seq": 1, "hash": admission.entries[0].hash}
    out["failures"] = {"count": 0, "digest": content_hash([])}
    return _json(out)


def _failed_experiment_round(audit: LoopAuditLog) -> int | None:
    """ADR-0070: the last recorded round when its experiment stage FAILED (recovery required)."""
    records = audit.records
    if records and any(
        stage.name == "experiment" and stage.status is StageStatus.FAILED
        for stage in records[-1].stages
    ):
        return records[-1].round_index
    return None


def _exact_state_version(value: object, what: str) -> int:
    """A state version is an exact JSON integer; floats such as ``4.0`` are refused."""
    if type(value) is not int or value not in (
        LEGACY_STATE_VERSION,
        STATE_VERSION,
        OPERATOR_STATE_VERSION,
    ):
        raise _refuse(f"unsupported {what} {value!r}")
    return value


# ----------------------------------------------------------------------------------- the delta


@dataclass(frozen=True, slots=True)
class _Marks:
    markets: int
    research_data: int
    strategies: int
    trials: int
    validations: int
    experiments: int
    states: int
    offspring: int

    @classmethod
    def of(cls, memory: ResearchMemory) -> _Marks:
        return cls(
            markets=len(memory.markets),
            research_data=len(memory.research_data),
            strategies=len(memory.strategies),
            trials=len(memory.trials),
            validations=len(memory.validations),
            experiments=len(memory.experiments),
            states=len(memory.states),
            offspring=len(memory.offspring),
        )


def _json(value: Any) -> Any:
    """``value`` as plain JSON data (canonical round trip; refuses NaN / Infinity)."""
    return json.loads(canonical_json(value))


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _trial_payload(outcome: TrialOutcome) -> dict[str, Any]:
    candidate = outcome.candidate
    return {
        "round_index": outcome.round_index,
        "hypothesis": outcome.hypothesis.model_dump(mode="json"),
        "hypothesis_hash": outcome.hypothesis.content_hash(),
        "origin": outcome.origin,
        "experiment": outcome.experiment.model_dump(mode="json"),
        "experiment_hash": outcome.experiment.content_hash(),
        "run": outcome.run.model_dump(mode="json"),
        "run_hash": outcome.run.content_hash(),
        "strategy": None if candidate is None else str(candidate.spec.ref),
        "strategy_hash": None if candidate is None else candidate.spec.content_hash(),
        "request_params": dict(outcome.request_params),
        "validation_seed": outcome.validation_seed,
        "summary": dict(outcome.summary),
        "error": outcome.error,
        "reason": None if outcome.reason is None else outcome.reason.value,
        "attempt": outcome.attempt,
        "knowledge_cutoff": _iso(outcome.knowledge_cutoff),
    }


def _report_payload(report: ValidationReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {"record": report.model_dump(mode="json"), "hash": report.content_hash()}


def _validation_payload(result: ValidationOutcome, trial_index: int) -> dict[str, Any]:
    return {
        "round_index": result.round_index,
        "trial": trial_index,
        "report": _report_payload(result.report),
        "failure_reason": None if result.failure_reason is None else result.failure_reason.value,
        "sealed_report": _report_payload(result.sealed_report),
        "sealed_status": dict(result.sealed_status),
        "summary": dict(result.summary),
        "error": result.error,
    }


def _first_bar(piece: ResearchPiece) -> int:
    start = piece.bars[0].interval_start
    return next(i for i, bar in enumerate(piece.market.bars) if bar.interval_start == start)


def _strategy_payload(candidate: StrategyCandidate) -> dict[str, Any]:
    spec = candidate.spec
    return {
        "spec": spec.model_dump(mode="json"),
        "spec_hash": spec.content_hash(),
        "parent": str(spec.lineage[-1]) if spec.lineage else None,
        "hypothesis_family_id": candidate.hypothesis_family_id,
        "risk_policy_hash": None
        if candidate.risk_policy is None
        else candidate.risk_policy.content_hash(),
    }


def _delta(memory: ResearchMemory, marks: _Marks) -> Any:
    now = _Marks.of(memory)
    if any(getattr(now, f.name) < getattr(marks, f.name) for f in fields(_Marks)):
        raise ValueError("research memory shrank: it is append-only")
    if len(memory.market_specs) != len(memory.markets):
        raise ValueError("every ingested market needs the spec it was generated from")
    market_index = {id(market): index for index, market in enumerate(memory.markets)}
    trial_index = {id(outcome): index for index, outcome in enumerate(memory.trials)}
    return _json(
        {
            "markets": [
                {"spec": spec.model_dump(mode="json"), "market_hash": market.market_hash}
                for spec, market in zip(
                    memory.market_specs[marks.markets :],
                    memory.markets[marks.markets :],
                    strict=True,
                )
            ],
            "research_data": [
                {
                    "market": market_index[id(piece.market)],
                    "first": _first_bar(piece),
                    "identity": piece.identity(),
                }
                for piece in memory.research_data[marks.research_data :]
            ],
            "strategies": [
                _strategy_payload(candidate)
                for candidate in list(memory.strategies.values())[marks.strategies :]
            ],
            "trials": [_trial_payload(o) for o in memory.trials[marks.trials :]],
            "validations": [
                _validation_payload(v, trial_index[id(v.outcome)])
                for v in memory.validations[marks.validations :]
            ],
            "experiments": memory.experiments[marks.experiments :],
            "states": memory.states[marks.states :],
            "offspring": memory.offspring[marks.offspring :],
        }
    )


def _approval_lines(memory: ResearchMemory) -> int:
    return sum(
        1
        for entry in _other_journals(memory, None)["reviews"].entries
        if entry.type == REVIEW_APPROVED
    )


class MemoryCheckpoint:
    """``ResearchLoop(checkpoint=...)``: one ``round_memory`` line per finished round; plus one
    ``between_rounds`` line per human approval between rounds (``between_rounds``)."""

    def __init__(
        self,
        journal: AppendOnlyJournal,
        memory: ResearchMemory,
        admission: PlanAdmissionJournal | None = None,
        state_lock: StateLock | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self._admission = admission
        self._state_lock = state_lock
        #: In-process admission exclusion, shared with the ``DurableState`` (module docs).
        self.admission_gate = _AdmissionGate()
        self._marks = _Marks.of(memory)
        self._covered = _approval_lines(memory)  # opening verified every one is checkpointed
        #: Transactions a ``plan_admission`` line of this memory journal already names.
        self._checkpointed = {
            entry.payload.get("transaction_id")
            for entry in journal.entries
            if entry.type == PLAN_ADMISSION
        }

    def reset_marks(self) -> None:
        """Set the round delta baseline after durable history has been restored in memory."""
        self._marks = _Marks.of(self._memory)
        self._covered = _approval_lines(self._memory)

    def require_settled(self, what: str, lease: object | None = None) -> None:
        """Refuse ``what`` while a PREPARE is pending or a COMMIT has no admission checkpoint, while
        an admission lease other than ``lease`` is active, or after an interrupted admission.

        The opener only finishes such a transaction as the journal tail; a round or between-rounds
        checkpoint covering it would leave an unfinished admission inside checkpointed history.
        """
        self.admission_gate.require_open(what, lease)
        admission = self._admission
        if admission is None:
            return
        if admission.pending is not None:
            raise LoopStateInconsistent(
                f"{what}: typed-plan PREPARE {admission.pending.transaction_id} is still pending"
            )
        open_commits = sorted(
            item.prepare.transaction_id
            for item in admission.committed
            if item.prepare.transaction_id not in self._checkpointed
        )
        if open_commits:
            raise LoopStateInconsistent(
                f"{what}: committed typed-plan admission(s) {open_commits} have no admission "
                "checkpoint"
            )

    def __call__(self, record: LoopRecord) -> None:
        memory = self._memory
        with self.admission_gate.lock:
            self.require_settled(f"round {record.round_index} checkpoint")
            if _approval_lines(memory) != self._covered:
                raise LoopStateInconsistent(
                    f"round {record.round_index}: the review journal holds a human approval no "
                    "between-rounds checkpoint names (its checkpoint failed, or it was written "
                    "past the review queue); the round is not recorded"
                )
            self._journal.append(
                ROUND_MEMORY,
                {
                    "round_index": record.round_index,
                    "record_hash": record.record_hash,
                    "heads": heads(memory, self._admission),
                    "delta": _delta(memory, self._marks),
                },
            )
            self._marks = _Marks.of(memory)

    def between_rounds(self, rounds: int, audit_head: str | None, approval: ReviewApproval) -> None:
        """Checkpoint the human approval just journaled (the review journal's last line)."""
        with self.admission_gate.lock:
            self.require_settled("between-rounds checkpoint")
            line = _other_journals(self._memory, None)["reviews"].entries[-1]
            if line.type != REVIEW_APPROVED or line.payload.get("key") != approval.key:
                raise LoopStateInconsistent("the review journal's last line is not this approval")
            self._journal.append(
                BETWEEN_ROUNDS,
                {
                    "rounds": rounds,
                    "audit_head": audit_head,
                    "heads": heads(self._memory, self._admission),
                    "action": {
                        "type": REVIEW_APPROVED,
                        "seq": line.seq,
                        "hash": line.hash,
                        "key": approval.key,
                        "reviewer": approval.reviewer,
                    },
                },
            )
            self._covered += 1

    def plan_admission(self, committed: CommittedAdmission, *, lease: object) -> JournalEntry:
        """Checkpoint one committed admission while its round remains open; v4 / v5 only, and only
        under the state's active admission ``lease`` (with the admission gate held)."""
        with self.admission_gate.lock:
            return self._plan_admission(committed, lease)

    def _plan_admission(self, committed: CommittedAdmission, lease: object) -> JournalEntry:
        self.admission_gate.require_owner("plan admission checkpoint", lease)
        if self._admission is None:
            raise LoopStateInconsistent("typed-plan admission requires a v4 or v5 state")
        if self._state_lock is None or not self._state_lock.held:
            raise LoopStateLocked("plan admission checkpoint requires the held loop state lock")
        round_identity = committed.prepare.round
        transaction_id = committed.prepare.transaction_id
        if not self._memory.ledger.durable:
            raise LoopStateInconsistent("typed-plan admission requires a durable TrialLedger")
        if committed not in self._admission.committed or transaction_id in self._checkpointed:
            raise LoopStateInconsistent(
                f"typed-plan admission {transaction_id} is not one uncheckpointed COMMIT of this "
                "plan admission journal"
            )
        # The opener accepts an admission checkpoint only when it moves the TrialLedger to the
        # batch event and the plan journal to the COMMIT, and nothing else: check the same here.
        expected = _checkpointed_heads(self._journal.entries, self._admission)
        expected["trial_ledger"] = {
            "seq": committed.ledger_event_seq,
            "hash": committed.ledger_event_hash,
        }
        expected["plan_admission"] = {"seq": committed.seq, "hash": committed.entry_hash}
        current = _json(heads(self._memory, self._admission))
        if current != expected:
            moved = sorted(
                k for k in set(current) | set(expected) if current.get(k) != expected.get(k)
            )
            raise LoopStateInconsistent(
                f"typed-plan admission {transaction_id}: {moved} are not where the previous "
                "checkpoint plus this one ledger event and COMMIT leave them"
            )
        entry = self._journal.append(
            PLAN_ADMISSION,
            {
                "round": round_identity.payload(),
                "transaction_id": transaction_id,
                "prepare_seq": committed.prepare.seq,
                "prepare_hash": committed.prepare.entry_hash,
                "commit_seq": committed.seq,
                "commit_hash": committed.entry_hash,
                "ledger_event_seq": committed.ledger_event_seq,
                "ledger_event_hash": committed.ledger_event_hash,
                "heads": current,
            },
        )
        self._checkpointed.add(transaction_id)
        return entry

    @property
    def journal(self) -> AppendOnlyJournal:
        return self._journal


# -------------------------------------------------------------------------------------- anchor


@dataclass(frozen=True, slots=True)
class StateHead:
    """Where a state directory's history ends (what an external anchor keeps; module docs).

    ``rounds``: recorded rounds; ``audit_head``: the last ``record_hash`` (``None`` before the
    first round); ``memory_head`` / ``memory_seq``: the memory journal's chain head and length
    (header + one checkpoint per round + one per between-rounds approval); ``heads``: every other
    file's position at the memory journal's last line (that checkpoint's ``heads``; empty while
    the journal holds only its header).
    """

    rounds: int
    audit_head: str | None
    memory_head: str
    memory_seq: int
    heads: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "audit_head": self.audit_head,
            "memory_head": self.memory_head,
            "memory_seq": self.memory_seq,
            "heads": _json(dict(self.heads)),
        }

    @classmethod
    def from_payload(cls, raw: Any) -> StateHead:
        if not isinstance(raw, Mapping) or set(raw) != _HEAD_KEYS:
            raise _refuse(f"an anchored head must have exactly the fields {sorted(_HEAD_KEYS)}")
        rounds, audit_head, memory_head = raw["rounds"], raw["audit_head"], raw["memory_head"]
        memory_seq = raw["memory_seq"]
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
            raise _refuse("an anchored head's round count must be a non-negative int")
        if isinstance(memory_seq, bool) or not isinstance(memory_seq, int) or memory_seq <= rounds:
            raise _refuse("an anchored head's memory length must exceed its round count")
        if (audit_head is None) != (rounds == 0) or not isinstance(audit_head, str | None):
            raise _refuse("an anchored head names an audit head exactly when it has rounds")
        if not isinstance(memory_head, str) or not isinstance(raw["heads"], Mapping):
            raise _refuse("an anchored head needs a memory head and the file positions")
        return cls(rounds, audit_head, memory_head, memory_seq, _json(dict(raw["heads"])))


_HEAD_KEYS: Final = frozenset({"rounds", "audit_head", "memory_head", "memory_seq", "heads"})


class StateAnchor(Protocol):
    """Keeps a state directory's head somewhere the directory's files cannot roll back."""

    def load(self) -> StateHead | None:
        """The last head published (``None``: nothing published yet)."""
        ...

    def publish(self, head: StateHead) -> None:
        """Keep ``head`` (called after every recorded round, every between-rounds approval and
        a verified reopening)."""
        ...


class FileAnchor:
    """A ``StateAnchor`` in one hash-chained journal file **outside** the state directory.

    Every published head is one ``loop_state_head`` line (``AppendOnlyJournal``: a tampered or
    shrunk file is ``JournalCorrupted``). Publishing the current head again is a no-op; a head
    that is not strictly further (a memory journal no longer than the last head's, or fewer
    rounds) is refused — the anchor never moves backwards or sideways.
    """

    def __init__(self, path: Path | str) -> None:
        self._journal = AppendOnlyJournal(Path(path))

    @property
    def path(self) -> Path:
        return self._journal.path

    def load(self) -> StateHead | None:
        entries = self._journal.entries
        if not entries:
            return None
        last = entries[-1]
        if last.type != ANCHOR_HEAD:
            raise _refuse(f"{self.path}:{last.seq} is not an anchored loop state head")
        return StateHead.from_payload(last.payload)

    def publish(self, head: StateHead) -> None:
        last = self.load()
        if last == head:
            return
        if last is not None and (head.memory_seq <= last.memory_seq or head.rounds < last.rounds):
            raise _refuse(
                f"the anchor {self.path} is at round count {last.rounds}, memory line "
                f"{last.memory_seq}; it never moves back or sideways (asked to keep round count "
                f"{head.rounds}, memory line {head.memory_seq})"
            )
        self._journal.append(ANCHOR_HEAD, head.payload())


# ------------------------------------------------------------------------------------- restore


@dataclass(frozen=True)
class DurableState:
    """What ``open_state`` hands the composition root."""

    root: Path
    memory: ResearchMemory
    audit: LoopAuditLog
    checkpoint: MemoryCheckpoint
    anchor: StateAnchor | None = None
    _plan_admission: PlanAdmissionJournal | None = None
    state_version: int = LEGACY_STATE_VERSION
    #: The directory's single-writer lock (set by ``open_state``; released by ``DurableLoop.close``,
    #: when the audit log is garbage-collected, or at process exit).
    lock: StateLock | None = None

    @contextmanager
    def plan_admission(self) -> Iterator[PlanAdmissionLease]:
        """The one way to admit a typed plan: ``with state.plan_admission() as lease:
        lease.prepare(...); lease.complete()`` (module docs, **Typed-plan admission lease**).

        Refused before anything is written when the state is v3, its ``state.lock`` is released,
        another lease is active (``LoopStateLocked``) or an earlier admission was interrupted
        (``LoopStateInconsistent``). Leaving the scope after ``prepare`` wrote anything but before
        ``complete`` returned — by an exception, an interrupt or normally — marks the state and its
        TrialLedger as refusing every later write (normally: ``LoopStateInconsistent`` is raised);
        the in-process locks are always released. The lease never compiles, invokes Providers or
        runs experiments.
        """
        with self._admission_lease() as lease:
            yield lease

    @contextmanager
    def _admission_lease(self) -> Iterator[PlanAdmissionLease]:
        """``plan_admission`` (also used by the opener's exact recovery)."""
        if self.state_version not in {STATE_VERSION, OPERATOR_STATE_VERSION} or (
            self._plan_admission is None
        ):
            raise LoopStateInconsistent(
                f"typed-plan admission is disabled in state version {self.state_version}"
            )
        if self.lock is None or not self.lock.held:
            raise LoopStateLocked("typed-plan admission requires the held loop state lock")
        gate = self.checkpoint.admission_gate
        ledger = self.memory.ledger
        with gate.lock:
            gate.require_open("a typed-plan admission lease")
            try:
                ledger_lease = ledger.acquire_write_lease()
            except LedgerError as exc:
                raise LoopStateLocked(f"a typed-plan admission lease is refused: {exc}") from exc
            try:
                lease = PlanAdmissionLease(self, ledger_lease, _admission_file_sizes(self.root))
            except BaseException:
                ledger.release_write_lease(ledger_lease)
                raise
            gate.owner = lease
        try:
            yield lease
        except BaseException:
            self._end_admission_lease(lease, failed=True)
            raise
        self._end_admission_lease(lease, failed=False)

    def _end_admission_lease(self, lease: PlanAdmissionLease, *, failed: bool) -> None:
        """Release the lease; poison the state and seal the ledger if it stopped half-written."""
        gate = self.checkpoint.admission_gate
        with gate.lock:
            lease._active = False
            reason: str | None = None
            if lease._committed is None:
                if self._admission_bytes_written(lease):
                    reason = (
                        "a typed-plan admission lease ended "
                        + ("by an exception or interrupt" if failed else "without complete()")
                        + " after writing part of its PREPARE → TrialLedger batch → COMMIT → "
                        "checkpoint → anchor sequence"
                    )
                    gate.poisoned = reason
            gate.owner = None
            self.memory.ledger.release_write_lease(lease._ledger_lease, seal=reason)
        if reason is not None and not failed:
            raise LoopStateInconsistent(
                f"{reason}; this state refuses every later write: close the loop and reopen its "
                "state directory to recover the admission (ADR-0073 §4)"
            )

    def _admission_bytes_written(self, lease: PlanAdmissionLease) -> bool:
        """Whether any byte was appended to the plan journal, TrialLedger or memory journal since
        ``lease`` began (a partial line included); unreadable sizes count as written."""
        try:
            return _admission_file_sizes(self.root) != lease._sizes
        except BaseException:
            return True

    def _prepare_admission(
        self,
        lease: PlanAdmissionLease,
        *,
        round: RoundStartedIdentity,
        plan: TypedPlan,
        compiler: PlanAdmissionEvidence,
        operators: Sequence[PlanAdmissionEvidence],
        providers: Sequence[PlanAdmissionEvidence],
        inputs: Sequence[PlanAdmissionEvidence],
        outputs: Sequence[PlanAdmissionEvidence],
        experiment_specs: Sequence[PlanAdmissionEvidence],
        hypotheses: Sequence[Hypothesis],
    ) -> PreparedAdmission:
        """``PlanAdmissionLease.prepare``: persist PREPARE at the current durable ledger head.

        This records evidence only. It does not prove producer relationships, invoke Providers, or
        make the non-runnable typed plan executable.

        Refused before anything is written (ADR-0073 §1 / §3 / §4, ADR-0070): a v3 state or a
        released lock; a round that is not the unique persisted open round; a loop whose last
        recorded experiment stage FAILED (recovery required); a second admission for the same
        round; an unfinished earlier admission; any journal or the failure registry moved since
        the memory journal's last line (the opener could not attribute that tail); an external
        anchor that does not hold this directory's current head (a crash after the admission
        checkpoint must leave an anchored history the opener can catch up, never an empty anchor
        it cannot tell from a lost one); and a batch Hypothesis identity the TrialLedger already
        holds (reuse / re-evaluation is not admitted).
        Raises ``LoopStateInconsistent`` / ``LoopStateLocked`` for the state and
        ``PlanAdmissionError`` for the request.
        """
        admission = self._plan_admission
        if self.state_version not in {STATE_VERSION, OPERATOR_STATE_VERSION} or admission is None:
            raise LoopStateInconsistent(
                f"typed-plan admission is disabled in state version {self.state_version}"
            )
        self.checkpoint.admission_gate.require_owner("typed-plan PREPARE", lease)
        lease._require_usable("typed-plan PREPARE")
        if self.lock is None or not self.lock.held:
            raise LoopStateLocked("typed-plan PREPARE requires the held loop state lock")
        if not isinstance(round, RoundStartedIdentity):
            raise LoopStateInconsistent("PREPARE needs a validated started-round identity")
        failed = _failed_experiment_round(self.audit)
        if failed is not None:
            raise LoopStateInconsistent(
                f"round {failed} has a failed experiment stage (ADR-0070 recovery required): "
                "no typed-plan admission until a human reviews it"
            )
        actual_round = _round_started_identity(self.audit, round.loop_id, round.round_index)
        if actual_round != round:
            raise LoopStateInconsistent("PREPARE does not match the unique persisted open round")
        if any(
            (item.round.loop_id, item.round.round_index) == (round.loop_id, round.round_index)
            for item in admission.prepared
        ):
            raise PlanAdmissionError(
                f"round {round.round_index} already has a typed-plan admission (one per round)"
            )
        self.checkpoint.require_settled("typed-plan PREPARE", lease)
        if self.anchor is not None and self.anchor.load() != self.head():
            raise LoopStateInconsistent(
                "typed-plan PREPARE requires the external anchor to hold this directory's "
                "current head (publish it first): an admission checkpoint written before the "
                "anchor ever held a head could not be told apart from a lost anchor on "
                "reopening"
            )
        ledger = self.memory.ledger
        position = ledger.journal_head()
        if position is None:
            raise LoopStateInconsistent("typed-plan admission requires a durable TrialLedger")
        # The lease's TrialLedger write lease refuses every other writer from here to COMMIT, so
        # the baseline read above is the one the batch event follows.
        if isinstance(hypotheses, Sequence):
            known = {(item.name, item.version) for item in ledger.hypotheses}
            reused = sorted(
                str(item.ref)
                for item in hypotheses
                if isinstance(item, Hypothesis) and (item.name, item.version) in known
            )
            if reused:
                raise PlanAdmissionError(
                    "a typed-plan admission registers only new Hypothesis identities; the "
                    f"TrialLedger already holds {reused}"
                )
        current = _json(heads(self.memory, admission))
        if current != _checkpointed_heads(self.checkpoint.journal.entries, admission):
            raise LoopStateInconsistent(
                "typed-plan PREPARE must start where the memory journal's last line left "
                "every file; this round already wrote to one of them"
            )
        return admission.prepare(
            round=actual_round,
            plan=plan,
            compiler=compiler,
            operators=operators,
            providers=providers,
            inputs=inputs,
            outputs=outputs,
            experiment_specs=experiment_specs,
            hypotheses=hypotheses,
            ledger_baseline_seq=position[0],
            ledger_baseline_hash=position[1],
        )

    def _finish_admission(
        self, lease: PlanAdmissionLease, transaction_id: str
    ) -> CommittedAdmission:
        """Ledger event → COMMIT → admission checkpoint under ``lease``, without the anchor; the
        admission gate is held throughout (module docs).

        Raises ``LoopStateInconsistent`` / ``LoopStateLocked`` for the state, ``LedgerError`` /
        ``PlanAdmissionError`` when the ledger no longer matches the PREPARE.
        """
        with self.checkpoint.admission_gate.lock:
            admission = self._plan_admission
            if (
                self.state_version not in {STATE_VERSION, OPERATOR_STATE_VERSION}
                or admission is None
            ):
                raise LoopStateInconsistent(
                    f"typed-plan admission is disabled in state version {self.state_version}"
                )
            self.checkpoint.admission_gate.require_owner("typed-plan COMMIT", lease)
            lease._require_usable("typed-plan COMMIT")
            if self.lock is None or not self.lock.held:
                raise LoopStateLocked("typed-plan admission requires the held loop state lock")
            prepared = admission.pending
            if prepared is None or prepared.transaction_id != transaction_id:
                raise LoopStateInconsistent("no matching pending plan PREPARE exists")
            current = _round_started_identity(
                self.audit, prepared.round.loop_id, prepared.round.round_index
            )
            if current != prepared.round:
                raise LoopStateInconsistent(
                    "plan PREPARE does not match the unique open round start"
                )
            event = self.memory.ledger.recover_register_batch(
                prepared.hypotheses,
                baseline_seq=prepared.ledger_baseline_seq,
                baseline_hash=prepared.ledger_baseline_hash,
                lease=lease._ledger_lease,
            )
            committed = admission.commit(transaction_id, event)
            self.checkpoint.plan_admission(committed, lease=lease)
            return committed

    def head(self) -> StateHead:
        """The directory's current head (after the last recorded round or between-rounds line)."""
        entries = self.checkpoint.journal.entries
        rounds = len(self.audit.records)
        checkpoints = sum(1 for entry in entries if entry.type == ROUND_MEMORY)
        if checkpoints != rounds:
            raise _refuse(
                f"the memory journal holds {checkpoints} round checkpoint(s) for {rounds} "
                "recorded round(s): a round was checkpointed but not recorded"
            )
        return StateHead(
            rounds=rounds,
            audit_head=self.audit.head,
            memory_head=entries[-1].hash,
            memory_seq=len(entries),
            heads=_line_heads(entries[-1]),
        )

    # -- round and review writes, serialized with the admission gate (module docs) --------------

    @contextmanager
    def round_scope(self) -> Iterator[None]:
        """``ResearchLoop(round_scope=...)``: hold the admission gate across a round start and
        across a finished round's checkpoint → audit record → after-record anchor move.

        Refused on entry (nothing written) while a typed-plan admission lease is active
        (``LoopStateLocked``), after an interrupted one, or while an admission is unfinished
        (``LoopStateInconsistent``). Holding the gate means no lease can start between a round's
        checkpoint and its audit record (it would see the checkpointed round still open).
        """
        with self.checkpoint.admission_gate.lock:
            self.checkpoint.require_settled("a loop round start or record")
            yield

    @contextmanager
    def review_scope(self) -> Iterator[None]:
        """``ReviewObserver.review_scope``: the admission gate, held across one review write."""
        with self.checkpoint.admission_gate.lock:
            yield

    def before_review_write(self, what: str) -> None:
        """Refuse an enqueue / take while an admission lease is active or after an interrupted
        one (called inside ``review_scope``, before anything is written)."""
        self.checkpoint.admission_gate.require_open(what)

    # -- ReviewObserver: human approvals between rounds (module docs) --------------------------

    def before_approval(self, key: str) -> None:
        """Refuse an approval while a round is running (approvals are made between rounds)."""
        if self.audit.open_round is not None:
            raise ValueError(
                f"round {self.audit.open_round} is running (or was interrupted): {key} can only "
                "be approved between rounds"
            )
        # before the approval is journaled: its between-rounds line would refuse afterwards
        self.checkpoint.require_settled(f"approval of {key}")

    def after_approval(self, approval: ReviewApproval) -> None:
        """Checkpoint the journaled approval and move the anchor up to it, immediately."""
        self.checkpoint.between_rounds(len(self.audit.records), self.audit.head, approval)
        self.publish_anchor()

    def publish_anchor(self, record: LoopRecord | None = None) -> None:
        """Move the anchor up to the current head (no anchor: nothing).

        ``ResearchLoop(after_record=...)`` calls it with every recorded round; the composition
        calls it without a record once the reopened directory passed every cross-check. Refused
        while a typed-plan admission lease is active or after an interrupted one.
        """
        self._publish_anchor(record, None)

    def _publish_anchor(self, record: LoopRecord | None, lease: object | None) -> None:
        if self.anchor is None:
            return
        gate = self.checkpoint.admission_gate
        with gate.lock:
            gate.require_open("moving the external anchor", lease)
            if record is not None and record.record_hash != self.audit.head:
                raise _refuse(
                    f"round {record.round_index} is not the audit head: nothing to anchor"
                )
            self.anchor.publish(self.head())

    def verify_guard(self, guard: LifecycleGuard) -> None:
        """Cross-check 7: every lifecycle subject the audit replayed is a registered hypothesis."""
        registered = {str(h.ref) for h in self.memory.ledger.hypotheses}
        stray = sorted(str(h.subject) for h in guard.histories if str(h.subject) not in registered)
        if stray:
            raise LoopStateInconsistent(
                f"the audit moved {stray} through the lifecycle, but the trial ledger never "
                "registered them"
            )


class PlanAdmissionLease:
    """One typed-plan admission of a v4 / v5 ``DurableState`` (``DurableState.plan_admission``).

    Only the state creates it; it is valid only inside its ``with`` scope and admits exactly one
    plan: ``prepare`` once, then ``complete`` once.

    Concurrency: the lease is bound to the thread that entered its scope; ``prepare`` and
    ``complete`` from any other thread are refused (``LoopStateLocked``) before anything is read
    or written. Each call also claims the lease exclusively (a re-entrant call while one runs is
    refused) and runs with the state's admission gate held. ``complete`` is claimed once, before
    its first write: a second call — also after the first one failed — is refused, so the
    batch event → COMMIT → checkpoint → anchor steps never run twice or concurrently for one
    lease. A ``prepare`` that fails after writing any admission byte makes the lease unusable;
    leaving the scope then poisons the state (module docs) and reopening recovers it.
    """

    def __init__(
        self, state: DurableState, ledger_lease: LedgerLease, sizes: tuple[int, ...]
    ) -> None:
        self._state = state
        self._ledger_lease = ledger_lease
        self._sizes = sizes
        self._thread = get_ident()
        self._claim = Lock()
        self._active = True
        self._broken: str | None = None
        self._complete_claimed = False
        self._prepared: PreparedAdmission | None = None
        self._committed: CommittedAdmission | None = None

    def _require_usable(self, what: str) -> None:
        """Refuse ``what`` from another thread, after the scope ended or once the lease broke."""
        if get_ident() != self._thread:
            raise LoopStateLocked(
                f"{what}: this typed-plan admission lease belongs to another thread"
            )
        if not self._active:
            raise LoopStateLocked(f"{what}: this typed-plan admission lease has ended")
        if self._broken is not None:
            raise LoopStateInconsistent(f"{what}: this typed-plan admission lease {self._broken}")

    @contextmanager
    def _claimed(self, what: str) -> Iterator[None]:
        """Exclusive use of this lease, with the state's admission gate held."""
        self._require_usable(what)
        if not self._claim.acquire(blocking=False):
            raise LoopStateLocked(f"{what}: this typed-plan admission lease is already in use")
        try:
            with self._state.checkpoint.admission_gate.lock:
                self._require_usable(what)
                yield
        finally:
            self._claim.release()

    def prepare(
        self,
        *,
        round: RoundStartedIdentity,
        plan: TypedPlan,
        compiler: PlanAdmissionEvidence,
        operators: Sequence[PlanAdmissionEvidence],
        providers: Sequence[PlanAdmissionEvidence],
        inputs: Sequence[PlanAdmissionEvidence],
        outputs: Sequence[PlanAdmissionEvidence],
        experiment_specs: Sequence[PlanAdmissionEvidence],
        hypotheses: Sequence[Hypothesis],
    ) -> PreparedAdmission:
        """Persist this lease's PREPARE (see ``DurableState._prepare_admission`` for the
        refusals); a lease admits one plan."""
        with self._claimed("typed-plan PREPARE"):
            if self._prepared is not None:
                raise PlanAdmissionError(
                    "this admission lease already wrote its PREPARE (one plan)"
                )
            try:
                prepared = self._state._prepare_admission(
                    self,
                    round=round,
                    plan=plan,
                    compiler=compiler,
                    operators=operators,
                    providers=providers,
                    inputs=inputs,
                    outputs=outputs,
                    experiment_specs=experiment_specs,
                    hypotheses=hypotheses,
                )
            except BaseException:
                if self._state._admission_bytes_written(self):
                    self._broken = "failed after writing part of its PREPARE"
                raise
            self._prepared = prepared
            return prepared

    def complete(self) -> CommittedAdmission:
        """Append the PREPARE's batch event, COMMIT and admission checkpoint, then move the anchor.

        Never compiles, invokes Providers or runs experiments.
        """
        with self._claimed("typed-plan COMMIT"):
            if self._prepared is None or self._complete_claimed:
                raise LoopStateInconsistent("complete() needs this lease's PREPARE and runs once")
            # Claimed before the first write: a failed or repeated complete() never re-runs it.
            self._complete_claimed = True
            committed = self._state._finish_admission(self, self._prepared.transaction_id)
            self._state._publish_anchor(None, self)
            self._committed = committed
            return committed


def _refuse(message: str) -> LoopStateInconsistent:
    return LoopStateInconsistent(message)


def _line_heads(entry: JournalEntry) -> Any:
    """The file positions a memory journal line names (none for the header)."""
    return {} if entry.type == LOOP_STATE_OPENED else _json(dict(entry.payload["heads"]))


def _round_started_identity(
    audit: LoopAuditLog, loop_id: str, round_index: int
) -> RoundStartedIdentity:
    """Resolve one started-round entry by its verified journal position and hash."""
    identity = audit.started_entry_identity(loop_id, round_index)
    if identity is None or audit.open_round != round_index:
        raise _refuse("typed-plan admission needs one matching persisted open round start")
    return RoundStartedIdentity(loop_id, round_index, identity[0], identity[1])


def _validate_operator_identity(value: Any) -> str:
    """Require the canonical lowercase SHA-256 identity used by operator state v5."""
    if not isinstance(value, str) or _OPERATOR_IDENTITY_PATTERN.fullmatch(value) is None:
        raise _refuse("operator state v5 requires a canonical lowercase SHA-256 operator_identity")
    return value


def open_state(
    state_dir: Path,
    *,
    fingerprint: Mapping[str, Any],
    strategies: Sequence[StrategyCandidate],
    provider: SyntheticMarketProvider | None,
    provider_for: Callable[[StrategySpec], Any] | None,
    anchor: StateAnchor | None = None,
    state_version: int = STATE_VERSION,
) -> DurableState:
    """Open (or create) a loop state directory and restore the research memory from it.

    ``strategies``: the configured library catalog (added before the restored offspring);
    ``provider``: regenerates the ingested markets (``None``: a round data source that keeps no
    ingest memory — the dataset-backed loop re-reads each round's verified manifests — so every
    checkpoint's ``markets`` / ``research_data`` must be empty); ``provider_for``: serves an
    offspring spec
    (the evolution plan's; ``None`` without evolution); ``anchor``: the optional external anchor
    (module docs; a ``FileAnchor`` must lie outside ``state_dir``); ``state_version`` selects the
    format for a new directory (defaults to v4). Existing v3 directories are always reopened in
    their v3 format and never migrated. Raises
    ``LoopStateInconsistent`` when the files disagree with each other, the configuration or the
    anchor (see module docs) — v4 plan admission reducer and recovery refusals included —,
    ``JournalCorrupted`` when one file is itself corrupt. A v4 admission tail is recovered only
    after every read-only cross-check passed (the audit's lifecycle transitions replayed through
    a fresh loop guard included), and the anchor moves only after the recovered positions are
    re-checked; an open round is refused afterwards. A v4 reopening with an open round or an
    unfinished admission never calls ``provider`` / ``provider_for`` (ADR-0073 §4): its rounds
    are verified from their recorded hashes and summaries only, and it always ends refused. The
    anchor is
    only verified here; ``DurableState.publish_anchor`` moves it up once the caller's own checks
    (the lifecycle guard) passed too. The returned state observes the restored review queue:
    every later approval is checkpointed and anchored at once (module docs, **Approvals between
    rounds**).
    """
    root = Path(state_dir)
    if isinstance(anchor, FileAnchor) and anchor.path.resolve().is_relative_to(root.resolve()):
        raise ValueError(
            f"the anchor {anchor.path} lies inside the state directory {root}: an anchor must "
            "live outside it (it has to survive a rollback of the directory)"
        )
    root.mkdir(parents=True, exist_ok=True)
    # Single writer (cross-process): taken before any file is read, whatever bus the caller uses;
    # held for the life of the restored audit log (which the composed loop keeps), released on
    # garbage collection or process exit (the kernel drops flocks; a crash leaves no stale lock).
    lock = StateLock(root)
    try:
        # Read the external head only after taking the directory's single-writer lock so the
        # recovery decision and every append below share one serialized state view.
        anchored = None if anchor is None else anchor.load()
        state = _open_locked(
            root, fingerprint, strategies, provider, provider_for, anchor, anchored, state_version,
            lock,
        )
    except BaseException:
        lock.release()
        raise
    object.__setattr__(state, "lock", lock)
    weakref.finalize(state.audit, lock.release)
    return state


def _open_locked(
    root: Path,
    fingerprint: Mapping[str, Any],
    strategies: Sequence[StrategyCandidate],
    provider: SyntheticMarketProvider | None,
    provider_for: Callable[[StrategySpec], Any] | None,
    anchor: StateAnchor | None,
    anchored: StateHead | None,
    requested_state_version: int,
    state_lock: StateLock,
) -> DurableState:
    """``open_state`` once the directory's single-writer lock is held (see ``open_state``)."""
    audit = LoopAuditLog(root / AUDIT_FILE)
    journal = AppendOnlyJournal(root / MEMORY_FILE)
    graph = LineageGraph((), path=root / LINEAGE_FILE)
    memory = ResearchMemory(
        failures=FailureRegistry(root / FAILURES_FILE),
        ledger=TrialLedger(root / LEDGER_FILE),
        reviews=ReviewQueue(root / REVIEWS_FILE),
        oos_ledger=DurableUnsealingLedger(root / SEALED_OOS_FILE),
        lineage=list(graph.specs),
        lineage_graph=graph,
    )
    for candidate in strategies:
        memory.add_strategy(candidate)
    entries = journal.entries
    _exact_state_version(requested_state_version, "requested loop state version")
    expected = _json(fingerprint)
    expected_operator_identity = expected.get("operator_identity")
    if requested_state_version == OPERATOR_STATE_VERSION:
        _validate_operator_identity(expected_operator_identity)
    elif expected_operator_identity is not None:
        raise _refuse("operator_identity requires operator state version 5")
    if entries:
        header = entries[0]
        if header.type != LOOP_STATE_OPENED:
            raise _refuse(f"{journal.path} does not start with a loop state header")
        state_version = _exact_state_version(
            header.payload.get("state_version"), "loop state version"
        )
        if state_version == STATE_VERSION and set(header.payload) != {
            "state_version",
            "fingerprint",
        }:
            raise _refuse(f"{journal.path} has a v4 header with other fields")
        if state_version == OPERATOR_STATE_VERSION and set(header.payload) != {
            "state_version",
            "fingerprint",
        }:
            raise _refuse(f"{journal.path} has a v5 header with other fields")
    else:
        state_version = requested_state_version
    if state_version not in {
        LEGACY_STATE_VERSION,
        STATE_VERSION,
        OPERATOR_STATE_VERSION,
    }:
        raise _refuse(f"unsupported loop state version {state_version!r}")
    if (
        state_version != requested_state_version
        and OPERATOR_STATE_VERSION in {state_version, requested_state_version}
    ):
        raise _refuse(
            f"state directory is v{state_version}; opener explicitly requested v"
            f"{requested_state_version}, and durable state versions are never migrated"
        )
    if state_version == OPERATOR_STATE_VERSION:
        _validate_operator_identity(expected_operator_identity)
    elif expected_operator_identity is not None:
        raise _refuse("operator_identity cannot open a v3 or v4 state directory")
    admission_path = root / PLAN_ADMISSION_FILE
    admission: PlanAdmissionJournal | None
    if state_version == LEGACY_STATE_VERSION:
        if admission_path.exists():
            raise _refuse("a v3 state directory contains a v4 plan admission journal")
        admission = None
    else:
        loop_id = expected.get("loop_id") if isinstance(expected, Mapping) else None
        if not isinstance(loop_id, str):
            raise _refuse(f"a v{state_version} loop fingerprint must bind a loop_id")
        if entries:
            if not admission_path.exists():
                raise _refuse(
                    f"a v{state_version} state directory is missing its required plan admission journal"
                )
            admission = _plan_journal(
                admission_path, loop_id=loop_id, state_version=state_version, create=False
            )
        else:
            # A v4 directory is created plan journal header first, memory header second. A crash
            # in between leaves an empty or header-only plan journal and no memory header: that
            # holds no admission and is finished below (the header is checked exactly there).
            # Anything past the header is an orphaned record.
            if admission_path.exists() and len(AppendOnlyJournal(admission_path).entries) > 1:
                raise _refuse(
                    "a new state directory has an existing plan admission journal that contains "
                    "orphaned records"
                )
            admission = None
    if not entries:
        _require_empty(audit, memory)
        if anchored is not None and (
            state_version in {STATE_VERSION, OPERATOR_STATE_VERSION}
            or anchored.memory_seq > 1
        ):
            raise _behind(root, 0, 0, anchored)
        if state_version in {STATE_VERSION, OPERATOR_STATE_VERSION}:
            loop_id = expected["loop_id"]
            admission = _plan_journal(
                admission_path,
                loop_id=loop_id,
                create=True,
                state_version=state_version,
            )
        journal.append(LOOP_STATE_OPENED, {"state_version": state_version, "fingerprint": expected})
        state = DurableState(
            root, memory, audit, MemoryCheckpoint(journal, memory, admission, state_lock), anchor,
            admission, state_version, state_lock,
        )
        _check_anchor(state, anchored)
        memory.reviews.observe(state)
        return state
    header = entries[0]
    _check_fingerprint(root, header.payload.get("fingerprint"), expected)
    if anchored is not None:
        _check_anchored_files(root, memory, anchored, admission)
    checkpoints: list[Mapping[str, Any]] = []
    marks: list[tuple[str, JournalEntry]] = []  # every checkpoint line, round or between rounds
    for entry in entries[1:]:
        if entry.type == ROUND_MEMORY and set(entry.payload) == _ROUND_KEYS:
            marks.append((f"the checkpoint of round {len(checkpoints)}", entry))
            checkpoints.append(entry.payload)
        elif entry.type == BETWEEN_ROUNDS and set(entry.payload) == _BETWEEN_KEYS:
            after = f"after round {len(checkpoints) - 1}" if checkpoints else "before round 0"
            marks.append(
                (f"the between-rounds checkpoint (memory line {entry.seq}, {after})", entry)
            )
        elif entry.type == PLAN_ADMISSION and set(entry.payload) == _ADMISSION_KEYS:
            if admission is None:
                raise _refuse("a v3 state directory contains a v4 plan admission checkpoint")
            marks.append((f"plan admission checkpoint (memory line {entry.seq})", entry))
        else:
            raise _refuse(
                f"{journal.path}:{entry.seq} is not a recognized v{state_version} checkpoint"
            )
    _check_rounds(audit, checkpoints, allow_open=admission is not None)
    try:
        _check_between_rounds(audit, marks)
        _check_admission_checkpoints(audit, admission, marks)
        _check_positions(memory, marks, admission, allow_recovery=admission is not None)
    except (KeyError, LookupError, TypeError) as exc:
        raise _refuse(f"a checkpoint of {journal.path} names unreadable positions: {exc}") from exc
    _check_ledgers(memory, audit.records)
    state = DurableState(
        root, memory, audit, MemoryCheckpoint(journal, memory, admission, state_lock), anchor,
        admission, state_version, state_lock,
    )
    if admission is not None:
        _check_anchor(state, anchored)
    # Every read-only cross-check (round contents and the lifecycle replay included) runs before
    # admission recovery appends anything, and recovery moves the anchor only after the re-checks
    # below. ADR-0073 §4: a reopening that may recover (and always ends refused: an open round is
    # never resumed) verifies the rounds without any Provider; see ``_verify_round_offline``.
    recovering = _admission_recovery_path(audit, admission, marks)
    if recovering:
        view = _AuditView.of(memory)
        for record, checkpoint in zip(audit.records, checkpoints, strict=True):
            _verify_round_offline(
                view,
                record,
                checkpoint["delta"],
                ingests=provider is not None,
                evolves=provider_for is not None,
            )
    else:
        for record, checkpoint in zip(audit.records, checkpoints, strict=True):
            _restore_round(memory, record, checkpoint["delta"], provider, provider_for)
    state.checkpoint.reset_marks()
    if admission is not None:
        _replay_guard(memory, audit, expected["loop_id"])
        try:
            publish = _recover_plan_admission(state, marks)
            _check_positions(memory, marks, admission)
            _check_admission_checkpoints(audit, admission, marks)
        except (LedgerError, PlanAdmissionError) as exc:
            raise _refuse(f"typed-plan admission recovery refused: {exc}") from exc
        except (KeyError, LookupError, TypeError) as exc:
            raise _refuse(f"a checkpoint of {journal.path} names unreadable positions: {exc}") from exc
        _check_anchor(state, anchored)
        if publish:
            state.publish_anchor()
        # Recovery covers pure registration writes only; a started but unrecorded round still
        # follows the durable loop rule (and ADR-0070) and remains stopped for human review.
        if audit.open_round is not None:
            raise _refuse(
                f"round {audit.open_round} was started but never recorded after admission recovery; "
                "the loop does not resume or rerun it"
            )
        if recovering:
            # Unreachable (an unfinished admission outside the open round is refused above), and
            # the research memory of this path was never restored: never hand it out.
            raise _refuse("a reopening that recovered a typed-plan admission cannot continue")
    _check_anchor(state, anchored)
    memory.reviews.observe(state)
    return state


def _plan_journal(
    path: Path, loop_id: str, *, create: bool, state_version: int = STATE_VERSION
) -> PlanAdmissionJournal:
    """Open the v4 / v5 plan admission journal; a reducer refusal is a state refusal."""
    try:
        return PlanAdmissionJournal(
            path, loop_id=loop_id, create=create, state_version=state_version
        )
    except (PlanAdmissionCorrupted, PlanAdmissionError) as exc:
        raise _refuse(
            f"{path} does not replay as this loop's v{state_version} plan admission journal: {exc}"
        ) from exc


def _replay_guard(memory: ResearchMemory, audit: LoopAuditLog, loop_id: str) -> None:
    """Cross-check 7 on the audit alone, before admission recovery writes or anchors anything
    (the composition's ``verify_guard`` runs only after ``open_state`` returned).

    Replays every audited transition into a fresh guard of the loop's own actor with the same
    step ``ResearchLoop`` uses when it continues an audit (``replay_transition``: automatable
    target, no human-approval edge, ADR-0053 evidence, legal edge from the subject's current
    state, ``triggered_by`` = the loop actor, same payload), then requires every replayed subject
    to be a registered hypothesis. Pure: no stage, Provider or experiment runs.
    """
    if audit.loop_id is not None and audit.loop_id != loop_id:
        raise _refuse(f"the audit belongs to loop {audit.loop_id!r}, not {loop_id!r}")
    guard = LifecycleGuard(actor=loop_actor(loop_id))
    for record in audit.records:
        for transition in record.transitions:
            try:
                replay_transition(guard, transition)
            except (LifecycleViolation, TypeError, ValueError) as exc:
                raise _refuse(
                    f"audit round {record.round_index}: a lifecycle transition of "
                    f"{transition.subject} does not replay under the loop guard: {exc}"
                ) from exc
    registered = {str(h.ref) for h in memory.ledger.hypotheses}
    stray = sorted(str(h.subject) for h in guard.histories if str(h.subject) not in registered)
    if stray:
        raise _refuse(
            f"the audit moved {stray} through the lifecycle, but the trial ledger never "
            "registered them"
        )


def _admission_recovery_path(
    audit: LoopAuditLog,
    admission: PlanAdmissionJournal | None,
    marks: Sequence[tuple[str, JournalEntry]],
) -> bool:
    """Whether this v4 reopening may append admission recovery records (ADR-0073 §4).

    That is: an open round, a pending PREPARE, or a COMMIT without its admission checkpoint.
    Every such reopening ends refused — ``_recover_plan_admission`` refuses an unfinished
    admission outside the unique open round, and an open round is never resumed afterwards —
    so its rounds are verified without regenerating markets or rebuilding strategies.
    """
    if admission is None:
        return False
    checkpointed = {
        mark.payload["transaction_id"] for _, mark in marks if mark.type == PLAN_ADMISSION
    }
    return (
        audit.open_round is not None
        or admission.pending is not None
        or any(item.prepare.transaction_id not in checkpointed for item in admission.committed)
    )


def _check_fingerprint(root: Path, recorded: Any, expected: Any) -> None:
    """Cross-check 1, naming the fields that differ (budgets: a human decision)."""
    if recorded == expected:
        return
    if not isinstance(recorded, Mapping):
        raise _refuse(f"{root} has no readable configuration fingerprint")
    changed = sorted(k for k in set(recorded) | set(expected) if recorded.get(k) != expected.get(k))
    budgets = [_BUDGET_FIELDS[k] for k in changed if k in _BUDGET_FIELDS]
    if budgets:
        raise _refuse(
            f"{root} was opened with another loop configuration: {'; '.join(budgets)} differs "
            "from the one bound to this state directory. A budget is never changed on a running "
            "loop (not raised, not lowered): raising it is a human decision and takes a NEW "
            f"state_dir or loop_id (fields that differ: {changed})"
        )
    raise _refuse(
        f"{root} belongs to another loop configuration (fields that differ: {changed}); a changed "
        "configuration is a new state directory"
    )


def _behind(root: Path, rounds: int, lines: int, anchored: StateHead) -> LoopStateInconsistent:
    return _refuse(
        f"{root} holds {rounds} recorded round(s) ({lines} memory line(s)) but its external anchor "
        f"recorded {anchored.rounds} ({anchored.memory_seq}): the directory is behind its anchor "
        "(rolled back / consistently truncated, or replaced) and those rounds or between-round "
        "approvals would be lost or run again"
    )


def _check_anchored_files(
    root: Path,
    memory: ResearchMemory,
    anchored: StateHead,
    admission: PlanAdmissionJournal | None = None,
) -> None:
    """Cross-check 8, first part: every file is at or after its anchored position, with the same
    line there (the review journal at or after the anchored review head)."""
    if not anchored.heads:
        return
    try:
        for name, journal in _journals(memory, admission).items():
            entries = journal.entries
            seq, expected = anchored.heads[name]["seq"], anchored.heads[name]["hash"]
            if seq > len(entries):
                raise _refuse(
                    f"{name} in {root} holds {len(entries)} line(s) but its external anchor "
                    f"recorded {seq}: the directory is behind its anchor (rolled back or "
                    "truncated), and what those lines recorded (a human approval, a registered "
                    "trial, an unsealing) would be lost or done again"
                )
            if (entries[seq - 1].hash if seq else GENESIS_HASH) != expected:
                raise _refuse(
                    f"{name} in {root} has diverged from its external anchor (another line {seq})"
                )
        hashes = _failure_hashes(memory.failures.records())
        count, digest = anchored.heads["failures"]["count"], anchored.heads["failures"]["digest"]
    except (KeyError, TypeError) as exc:
        raise _refuse(f"the anchored head names unreadable file positions: {exc}") from exc
    if count > len(hashes):
        raise _refuse(
            f"failures in {root} holds {len(hashes)} record(s) but its external anchor recorded "
            f"{count}: the directory is behind its anchor (rolled back or truncated)"
        )
    if content_hash(hashes[:count]) != digest:
        raise _refuse(f"failures in {root} has diverged from its external anchor")


def _check_anchor(state: DurableState, anchored: StateHead | None) -> None:
    """Cross-check 8: the directory is at or after the anchored head, on the same history.

    An empty anchor is accepted only for a directory holding its header alone. A v4 admission
    checkpoint is never a first publication: PREPARE requires the anchor to hold the current head
    (``PlanAdmissionLease.prepare``), so a crash between the admission checkpoint and the
    anchor leaves the anchor at the pre-admission head (caught up after recovery), while an empty
    anchor beside any checkpoint line is a lost or replaced anchor and is refused.
    """
    if state.anchor is None:
        return
    records = state.audit.records
    entries = state.checkpoint.journal.entries
    if anchored is None:
        if records or len(entries) > 1:
            raise _refuse(
                f"the external anchor holds no head, but {state.root} holds {len(records)} "
                f"recorded round(s) ({len(entries)} memory line(s)): the anchor was lost or "
                "attached to a running directory (a human checks the directory and re-anchors "
                "it deliberately)"
            )
        return
    count, lines = anchored.rounds, anchored.memory_seq
    if count > len(records) or lines > len(entries):
        raise _behind(state.root, len(records), len(entries), anchored)
    entry = entries[lines - 1]
    same = (
        entry.hash == anchored.memory_head
        and sum(1 for e in entries[:lines] if e.type == ROUND_MEMORY) == count
        and (count == 0 or records[count - 1].record_hash == anchored.audit_head)
        and _line_heads(entry) == anchored.heads
    )
    if not same:
        raise _refuse(
            f"{state.root} has diverged from its external anchor: its history up to memory line "
            f"{lines} (round count {count}) differs from the anchored one (audit / memory / file "
            "positions)"
        )


def _require_empty(
    audit: LoopAuditLog,
    memory: ResearchMemory,
    admission: PlanAdmissionJournal | None = None,
) -> None:
    """A state directory without a memory header must hold no state at all."""
    held = [
        name
        for name, journal in _journals(memory, admission).items()
        if journal.entries
        and not (
            name == "plan_admission"
            and len(journal.entries) == 1
            and journal.entries[0].type == "plan_admission_header"
        )
    ]
    if audit.records or audit.open_round is not None:
        held.append("audit")
    if memory.failures.records():
        held.append("failures")
    if held:
        raise _refuse(
            f"the memory checkpoint file is missing or empty, but {sorted(held)} hold state: "
            "the directory was tampered with or mixed with another run"
        )


def _check_rounds(
    audit: LoopAuditLog,
    checkpoints: Sequence[Mapping[str, Any]],
    *,
    allow_open: bool = False,
) -> None:
    """Cross-checks 2 and 3."""
    if audit.open_round is not None and not allow_open:
        raise _refuse(
            f"round {audit.open_round} was started but never recorded (interrupted): what it "
            "spent and wrote is unknown, so the loop does not continue until a human reviews it"
        )
    records = audit.records
    if len(checkpoints) != len(records):
        raise _refuse(
            f"the audit records {len(records)} round(s) but the memory checkpoint holds "
            f"{len(checkpoints)}: one of them was truncated or deleted, or a round was interrupted"
        )
    for index, (record, checkpoint) in enumerate(zip(records, checkpoints, strict=True)):
        if checkpoint["round_index"] != index or checkpoint["record_hash"] != record.record_hash:
            raise _refuse(f"the memory checkpoint of round {index} names another audit record")


def _check_between_rounds(audit: LoopAuditLog, marks: Sequence[tuple[str, JournalEntry]]) -> None:
    """Every between-rounds checkpoint follows its round and names one human approval."""
    records = audit.records
    rounds = 0
    for label, mark in marks:
        if mark.type == ROUND_MEMORY:
            rounds += 1
            continue
        if mark.type == PLAN_ADMISSION:
            continue
        payload = mark.payload
        if (payload["rounds"], payload["audit_head"]) != (
            rounds,
            records[rounds - 1].record_hash if rounds else None,
        ):
            raise _refuse(f"{label} names another round boundary (round count / audit head)")
        action = payload["action"]
        if (
            not isinstance(action, Mapping)
            or set(action) != _ACTION_KEYS
            or action["type"] != REVIEW_APPROVED
        ):
            raise _refuse(f"{label} names no human approval (the only action between rounds)")


def _check_admission_checkpoints(
    audit: LoopAuditLog,
    admission: PlanAdmissionJournal | None,
    marks: Sequence[tuple[str, JournalEntry]],
) -> None:
    """Bind every v4 transaction and memory checkpoint to the exact started audit entry."""
    checkpoints = [mark for _, mark in marks if mark.type == PLAN_ADMISSION]
    if admission is None:
        if checkpoints:
            raise _refuse("a v3 state contains a typed-plan admission checkpoint")
        return
    # TrialLedger is validated by its own reducer; this cross-check uses its verified envelopes.
    prepared_by_id = {item.transaction_id: item for item in admission.prepared}
    committed_by_id = {item.prepare.transaction_id: item for item in admission.committed}
    if len(prepared_by_id) != len(admission.prepared):
        raise _refuse("plan admission transaction identities are not unique")
    round_ids: set[tuple[str, int]] = set()
    for prepared in admission.prepared:
        identity = prepared.round
        key = (identity.loop_id, identity.round_index)
        if key in round_ids:
            raise _refuse("more than one typed-plan admission is bound to the same round")
        round_ids.add(key)
        actual = audit.started_entry_identity(identity.loop_id, identity.round_index)
        if actual != (identity.started_entry_seq, identity.started_entry_hash):
            raise _refuse("a plan admission round identity differs from the audit start entry")
    if len(checkpoints) != len({mark.payload["transaction_id"] for mark in checkpoints}):
        raise _refuse("a plan admission transaction has more than one memory checkpoint")
    named: set[str] = set()
    for checkpoint in checkpoints:
        payload = checkpoint.payload
        transaction_id = payload["transaction_id"]
        committed = committed_by_id.get(transaction_id)
        if committed is None or transaction_id in named:
            raise _refuse("a plan admission checkpoint has no unique committed transaction")
        expected = {
            "round": committed.prepare.round.payload(),
            "transaction_id": transaction_id,
            "prepare_seq": committed.prepare.seq,
            "prepare_hash": committed.prepare.entry_hash,
            "commit_seq": committed.seq,
            "commit_hash": committed.entry_hash,
            "ledger_event_seq": committed.ledger_event_seq,
            "ledger_event_hash": committed.ledger_event_hash,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise _refuse("a plan admission checkpoint does not identify its exact PREPARE/COMMIT")
        rounds_recorded = sum(
            1 for _, mark in marks if mark.seq < checkpoint.seq and mark.type == ROUND_MEMORY
        )
        if rounds_recorded != committed.prepare.round.round_index:
            raise _refuse("a plan admission checkpoint is outside its started round boundary")
        heads_at_commit = payload.get("heads")
        if not isinstance(heads_at_commit, Mapping):
            raise _refuse("a plan admission checkpoint has no journal positions")
        ledger_position = heads_at_commit.get("trial_ledger")
        plan_position = heads_at_commit.get("plan_admission")
        if (
            not isinstance(ledger_position, Mapping)
            or (ledger_position.get("seq"), ledger_position.get("hash"))
            != (committed.ledger_event_seq, committed.ledger_event_hash)
            or not isinstance(plan_position, Mapping)
            or (plan_position.get("seq"), plan_position.get("hash"))
            != (committed.seq, committed.entry_hash)
        ):
            raise _refuse("a plan admission checkpoint does not cover its exact ledger / COMMIT heads")
        named.add(transaction_id)
    if len(named) > len(committed_by_id):
        raise _refuse("a plan admission checkpoint names an unknown COMMIT")
    # Writer symmetry (``MemoryCheckpoint.require_settled`` / ``plan_admission``): an admission
    # checkpoint starts exactly where the previous memory line left the TrialLedger (the PREPARE
    # baseline) and the plan journal (just before the PREPARE); no round or between-rounds line
    # covers a pending PREPARE or a COMMIT without its admission checkpoint.
    previous_ledger: Any = {"seq": 0, "hash": GENESIS_HASH}
    previous_plan: Any = {"seq": 1, "hash": admission.entries[0].hash}
    for _, mark in marks:
        position = mark.payload["heads"]
        if mark.type == PLAN_ADMISSION:
            prepare = committed_by_id[mark.payload["transaction_id"]].prepare
            if previous_ledger != {
                "seq": prepare.ledger_baseline_seq,
                "hash": prepare.ledger_baseline_hash,
            } or previous_plan != {
                "seq": prepare.seq - 1,
                "hash": admission.entries[prepare.seq - 1].prev_hash,
            }:
                raise _refuse(
                    "a plan admission checkpoint does not start where the previous memory line "
                    "left the TrialLedger and the plan admission journal"
                )
        previous_ledger = _json(position["trial_ledger"])
        previous_plan = _json(position["plan_admission"])
    unfinished = [
        item.prepare.seq for item in admission.committed if item.prepare.transaction_id not in named
    ]
    if admission.pending is not None:
        unfinished.append(admission.pending.seq)
    if any(seq <= previous_plan["seq"] for seq in unfinished):
        raise _refuse(
            "a round or between-rounds checkpoint covers an unfinished typed-plan admission "
            "(a pending PREPARE or a COMMIT without its admission checkpoint)"
        )


def _verify_committed_ledger(state: DurableState, committed: CommittedAdmission) -> JournalEntry:
    """Verify a historical COMMIT against the one exact TrialLedger batch event."""
    journal = state.memory.ledger.journal_snapshot()
    if journal is None:
        raise _refuse("typed-plan admission has no durable TrialLedger")
    prepare = committed.prepare
    index = prepare.ledger_baseline_seq
    entries = journal.entries
    if index >= len(entries):
        raise _refuse("a committed plan admission is missing its TrialLedger batch event")
    event = entries[index]
    if (
        event.seq != prepare.ledger_baseline_seq + 1
        or event.type != "register_batch"
        or event.prev_hash != prepare.ledger_baseline_hash
        or dict(event.payload) != prepare.batch_payload()
        or event.seq != committed.ledger_event_seq
        or event.hash != committed.ledger_event_hash
    ):
        raise _refuse("a committed plan admission differs from its exact TrialLedger event")
    expected = content_hash(
        {
            "seq": event.seq,
            "type": event.type,
            "payload": dict(event.payload),
            "prev_hash": event.prev_hash,
        }
    )
    if expected != event.hash:
        raise _refuse("a committed TrialLedger event hash does not reproduce")
    return event


def _recover_plan_admission(
    state: DurableState,
    marks: list[tuple[str, JournalEntry]],
) -> bool:
    """Finish only one exact tail transaction; never resumes the interrupted experiment round.

    Appends at most the missing batch event / COMMIT / admission checkpoint and never moves the
    anchor: it returns whether the caller should move it (the tail is an admission of the open
    round) once every re-check of the recovered files passed.
    """
    admission = state._plan_admission
    if admission is None:
        return False
    for committed in admission.committed:
        _verify_committed_ledger(state, committed)
    checkpointed = {
        mark.payload["transaction_id"] for _, mark in marks if mark.type == PLAN_ADMISSION
    }
    uncheckpointed = [
        item for item in admission.committed if item.prepare.transaction_id not in checkpointed
    ]
    if admission.pending is not None and uncheckpointed:
        raise _refuse("a pending PREPARE coexists with an uncheckpointed COMMIT")
    if admission.pending is not None or uncheckpointed:
        failed = _failed_experiment_round(state.audit)
        if failed is not None:
            # ADR-0070 / ADR-0073 §1: no admission while recovery is required; such a tail was
            # never admissible, so nothing is registered or committed for it.
            raise _refuse(
                f"round {failed} has a failed experiment stage (recovery required), yet a later "
                "typed-plan admission is unfinished: a human reviews the directory"
            )
    if admission.pending is not None:
        prepared = admission.pending
        current = _round_started_identity(
            state.audit, prepared.round.loop_id, prepared.round.round_index
        )
        if current != prepared.round:
            raise _refuse("pending PREPARE is not for the unique currently open round")
        with state._admission_lease() as lease:
            lease._committed = state._finish_admission(lease, prepared.transaction_id)
        line = state.checkpoint.journal.entries[-1]
        marks.append((f"recovered plan admission checkpoint (memory line {line.seq})", line))
        return True
    if uncheckpointed:
        if len(uncheckpointed) != 1:
            raise _refuse("multiple committed admissions lack memory checkpoints")
        committed = uncheckpointed[0]
        current = _round_started_identity(
            state.audit, committed.prepare.round.loop_id, committed.prepare.round.round_index
        )
        if current != committed.prepare.round:
            raise _refuse("uncheckpointed COMMIT is not for the unique currently open round")
        _verify_committed_ledger(state, committed)
        with state._admission_lease() as lease:
            line = state.checkpoint.plan_admission(committed, lease=lease)
            lease._committed = committed
        marks.append((f"recovered plan admission checkpoint (memory line {line.seq})", line))
        return True
    # The admission checkpoint itself was durable but its anchor write may have been interrupted:
    # the caller advances the anchor to that exact head after the re-checks. An open round
    # without a pure-admission tail is never resumed (and never anchors here).
    return state.audit.open_round is not None and any(
        mark.type == PLAN_ADMISSION
        and mark.payload["round"]["round_index"] == state.audit.open_round
        for _, mark in marks
    )


def _check_positions(
    memory: ResearchMemory,
    marks: Sequence[tuple[str, JournalEntry]],
    admission: PlanAdmissionJournal | None = None,
    *,
    allow_recovery: bool = False,
) -> None:
    """Cross-check 4: every file is exactly where the checkpoints say it was."""
    pending = None if admission is None else admission.pending
    committed_ids = set() if admission is None else {item.prepare.transaction_id for item in admission.committed}
    checkpointed_ids = {
        mark.payload["transaction_id"] for _, mark in marks if mark.type == PLAN_ADMISSION
    }
    uncheckpointed_commits = committed_ids - checkpointed_ids
    expected_head_keys = set(_journals(memory, admission)) | {"failures"}
    for name, journal in _journals(memory, admission).items():
        entries = journal.entries
        previous = 1 if name == "plan_admission" and admission is not None else 0
        for label, mark in marks:
            if admission is not None and set(mark.payload["heads"]) != expected_head_keys:
                raise _refuse(f"{label} has an incomplete or unexpected v4 journal-head set")
            position = mark.payload["heads"][name]
            seq = position["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < previous:
                raise _refuse(f"{label} moves {name} backwards")
            if seq > len(entries):
                raise _refuse(
                    f"{name} holds {len(entries)} line(s) but {label} recorded {seq}: {name} was "
                    "truncated or deleted (the audit is ahead of it)"
                )
            head = entries[seq - 1].hash if seq else GENESIS_HASH
            if head != position["hash"]:
                raise _refuse(f"{name} does not match {label}: its history was rewritten")
            if mark.type == BETWEEN_ROUNDS:
                _check_between_move(name, label, entries, previous, seq, mark.payload["action"])
            elif mark.type == PLAN_ADMISSION:
                changed = name in {"trial_ledger", "plan_admission"}
                if not changed and seq != previous:
                    raise _refuse(f"{label} unexpectedly moves {name}")
            previous = seq
        if name == "reviews":
            named = {
                mark.payload["action"]["seq"] for _, mark in marks if mark.type == BETWEEN_ROUNDS
            }
            stray = [e.seq for e in entries if e.type == REVIEW_APPROVED and e.seq not in named]
            if stray:
                raise _refuse(
                    f"reviews line(s) {stray} are human approvals no between-rounds checkpoint "
                    "names: appended past the review queue (forged), made while a round ran, or "
                    "the process died between approving and checkpointing (a human checks it)"
                )
        extra = entries[previous:]
        permitted = allow_recovery and (
            (name == "plan_admission" and admission is not None and len(uncheckpointed_commits) + (pending is not None) == 1
             and len(extra) == (2 if uncheckpointed_commits else 1))
            or (
                name == "trial_ledger"
                and (pending is not None or len(uncheckpointed_commits) == 1)
                and len(extra) <= 1
            )
        )
        if extra and not permitted:
            raise _refuse(
                f"{name} holds {len(extra)} line(s) no checkpoint accounts for: the audit or the "
                "memory checkpoint is behind it (truncated), or a round was interrupted"
            )
        if name == "trial_ledger" and admission is not None and (
            pending is not None or uncheckpointed_commits
        ):
            prepared = (
                pending
                if pending is not None
                else next(
                    item.prepare
                    for item in admission.committed
                    if item.prepare.transaction_id in uncheckpointed_commits
                )
            )
            if (prepared.ledger_baseline_seq, prepared.ledger_baseline_hash) != (
                previous,
                entries[previous - 1].hash if previous else GENESIS_HASH,
            ):
                raise _refuse("uncheckpointed admission does not start at the last checkpointed ledger head")
    hashes = _failure_hashes(memory.failures.records())
    previous = 0
    for label, mark in marks:
        position = mark.payload["heads"]["failures"]
        count = position["count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < previous:
            raise _refuse(f"{label} moves failures backwards")
        if count > len(hashes):
            raise _refuse(
                f"failures holds {len(hashes)} record(s) but {label} recorded {count}: failures "
                "was truncated or deleted (the audit is ahead of it)"
            )
        if content_hash(hashes[:count]) != position["digest"]:
            raise _refuse(f"failures does not match {label}")
        if mark.type == BETWEEN_ROUNDS and count != previous:
            raise _refuse(f"{label} moves failures: only human approvals are made between rounds")
        if mark.type == PLAN_ADMISSION and count != previous:
            raise _refuse(f"{label} moves failures: an admission moves only its ledger and plan")
        previous = count
    if len(hashes) != previous:
        raise _refuse(
            f"failures holds {len(hashes) - previous} record(s) no recorded round accounts for"
        )


def _check_between_move(
    name: str,
    label: str,
    entries: Sequence[JournalEntry],
    previous: int,
    seq: int,
    action: Mapping[str, Any],
) -> None:
    """A between-rounds checkpoint moves ``reviews`` by its one approval and nothing else."""
    if name != "reviews":
        if seq != previous:
            raise _refuse(f"{label} moves {name}: only human approvals are made between rounds")
        return
    line = entries[seq - 1] if seq else None
    if (
        seq != previous + 1
        or line is None
        or line.type != REVIEW_APPROVED
        or (action["seq"], action["hash"]) != (line.seq, line.hash)
        or (action["key"], action["reviewer"]) != (line.payload["key"], line.payload["reviewer"])
    ):
        raise _refuse(
            f"{label} does not name exactly the one human approval that follows the previous "
            "checkpoint in reviews"
        )


def _summary(record: LoopRecord, stage: str) -> Mapping[str, Any] | None:
    found = next((s for s in record.stages if s.name == stage), None)
    return None if found is None else found.summary


def _restore_round(
    memory: ResearchMemory,
    record: LoopRecord,
    delta: Any,
    provider: SyntheticMarketProvider | None,
    provider_for: Callable[[StrategySpec], Any] | None,
) -> None:
    """Re-apply one round's delta, checking it against the audit record (cross-check 5)."""
    index = record.round_index
    try:
        if set(delta) != _DELTA_KEYS:
            raise ValueError("the delta has other fields")
        _restore_markets(memory, record, delta, provider)
        _restore_strategies(memory, delta["strategies"], provider_for)
        _restore_trials(memory, record, delta)
        _check_stage_rows(record, delta)
    except (ArithmeticError, KeyError, LookupError, TypeError, ValueError) as exc:
        raise _refuse(f"the memory checkpoint of round {index} is inconsistent: {exc}") from exc
    memory.experiments.extend(delta["experiments"])
    memory.states.extend(delta["states"])
    memory.offspring.extend(delta["offspring"])


def _check_stage_rows(record: LoopRecord, delta: Any) -> None:
    """The delta's experiment / state / offspring rows are the audit record's stage summaries."""
    index = record.round_index
    experiments = _summary(record, "experiment")
    if experiments is not None and delta["experiments"] != [
        {"round": index, **row} for row in experiments["experiments"]
    ]:
        raise ValueError("the experiment summaries differ from the audit")
    state = _summary(record, "state")
    if state is not None and delta["states"] != [{"round": index, **state}]:
        raise ValueError("the state summary differs from the audit")
    evolution = _summary(record, "evolution")
    if evolution is not None and delta["offspring"] != [
        {"round": index, **row} for row in evolution["offspring"]
    ]:
        raise ValueError("the offspring rows differ from the audit")


def _strategy_facts(candidate: StrategyCandidate) -> tuple[str, str, str | None]:
    """A catalog entry as provider-free verification sees it: spec hash, family, risk hash."""
    risk = candidate.risk_policy
    return (
        candidate.spec.content_hash(),
        candidate.hypothesis_family_id,
        None if risk is None else risk.content_hash(),
    )


@dataclass(slots=True)
class _AuditView:
    """What ``_verify_round_offline`` carries from round to round (no market / strategy objects)."""

    strategies: dict[str, tuple[str, str, str | None]]
    market_hashes: list[str] = field(default_factory=list)
    research_pieces: int = 0
    trials: int = 0

    @classmethod
    def of(cls, memory: ResearchMemory) -> _AuditView:
        return cls({key: _strategy_facts(c) for key, c in memory.strategies.items()})


def _verify_round_offline(
    view: _AuditView, record: LoopRecord, delta: Any, *, ingests: bool, evolves: bool
) -> None:
    """Cross-check 5 without any Provider, for a reopening that may recover an admission.

    ADR-0073 §4: admission recovery runs no Provider, compiler or experiment, so neither
    ``provider.generate`` nor ``provider_for`` is called and no ``ResearchMemory`` content is
    restored (the reopening ends refused; nothing restored would ever be used). Checked as in
    ``_restore_round``: the delta's shape; its market rows (spec schema, the ingest summary's
    market hash, a single market per ingest); research pieces naming a restored market and the
    accumulated piece count; offspring specs (hash, parent in the catalog or an earlier round,
    inherited family / risk policy, no catalog ref with other content, an evolution plan
    configured); trial rows (catalog strategy and hash, hypothesis / experiment / run content
    hashes, reason and cutoff) equal to the audit's experiment rows; validation rows (an earlier
    trial, report hashes, reason) equal to the audit's reports; the experiment / state / offspring
    rows. Only regeneration proves the rest — that a market spec regenerates its ``market_hash``
    and ``spec_hash`` and that a piece's bars rebuild — and stays unproven on this path.
    """
    index = record.round_index
    try:
        if set(delta) != _DELTA_KEYS:
            raise ValueError("the delta has other fields")
        ingest = _summary(record, "ingest")
        if not ingests:
            if delta["markets"] or delta["research_data"]:
                raise ValueError("this loop's ingest keeps no markets or research pieces")
        else:
            for row in delta["markets"]:
                SyntheticMarketSpec.model_validate(row["spec"])
                if not isinstance(row["market_hash"], str):
                    raise ValueError("a market row names no market hash")
                view.market_hashes.append(row["market_hash"])
            if ingest is not None:
                [row] = delta["markets"]
                if ingest["market_hash"] != row["market_hash"]:
                    raise ValueError("the ingested market differs from the audit")
            for row in delta["research_data"]:
                market, first, identity = row["market"], row["first"], row["identity"]
                if (
                    type(market) is not int
                    or not 0 <= market < len(view.market_hashes)
                    or identity["market_hash"] != view.market_hashes[market]
                    or type(first) is not int
                    or first < 0
                    or type(identity["bars"]) is not int
                    or identity["bars"] < 1
                ):
                    raise ValueError("a research piece does not name a restored market")
                view.research_pieces += 1
            if ingest is not None and ingest["research_pieces"] != view.research_pieces:
                raise ValueError("the accumulated research pieces differ from the audit")
        for row in delta["strategies"]:
            spec = StrategySpec.model_validate(row["spec"])
            if spec.content_hash() != row["spec_hash"]:
                raise ValueError(f"{spec.ref} does not reproduce its spec hash")
            parent = view.strategies.get(str(row["parent"]))
            if not evolves or parent is None:
                raise ValueError(f"{spec.ref} cannot be rebuilt: no evolution plan or no parent")
            if parent[1:] != (row["hypothesis_family_id"], row["risk_policy_hash"]):
                raise ValueError(f"{spec.ref} does not inherit its parent's family and risk policy")
            known = view.strategies.get(str(spec.ref))
            if known is not None and known[0] != row["spec_hash"]:
                raise ValueError(f"{spec.ref} is already in the catalog with other content")
            view.strategies[str(spec.ref)] = (row["spec_hash"], parent[1], parent[2])
        summaries: list[Any] = []
        for row in delta["trials"]:
            strategy = row["strategy"]
            if strategy is not None and view.strategies[strategy][0] != row["strategy_hash"]:
                raise ValueError(f"trial strategy {strategy} is another spec in the catalog")
            _checked(Hypothesis, row["hypothesis"], row["hypothesis_hash"], "a hypothesis")
            _checked(ExperimentSpec, row["experiment"], row["experiment_hash"], "an experiment")
            _checked(ExperimentRun, row["run"], row["run_hash"], "a run")
            if row["reason"] is not None:
                ReasonCode(row["reason"])
            if row["knowledge_cutoff"] is not None:
                datetime.fromisoformat(row["knowledge_cutoff"])
            summaries.append(dict(row["summary"]))
        experiment = _summary(record, "experiment")
        if experiment is not None and summaries != experiment["experiments"]:
            raise ValueError("the restored trials differ from the audit's experiment rows")
        view.trials += len(delta["trials"])
        reports: list[Any] = []
        for row in delta["validations"]:
            trial = row["trial"]
            if type(trial) is not int or not 0 <= trial < view.trials:
                raise ValueError("a validation row names no restored trial")
            _report(row["report"])
            _report(row["sealed_report"])
            if row["failure_reason"] is not None:
                ReasonCode(row["failure_reason"])
            reports.append(dict(row["summary"]))
        validation = _summary(record, "validation")
        if validation is not None and reports != validation["reports"]:
            raise ValueError(f"the restored validations of round {index} differ from the audit")
        _check_stage_rows(record, delta)
    except (ArithmeticError, KeyError, LookupError, TypeError, ValueError) as exc:
        raise _refuse(f"the memory checkpoint of round {index} is inconsistent: {exc}") from exc


def _restore_markets(
    memory: ResearchMemory,
    record: LoopRecord,
    delta: Any,
    provider: SyntheticMarketProvider | None,
) -> None:
    if provider is None:  # a source without ingest memory (dataset-backed loop): nothing to add
        if delta["markets"] or delta["research_data"]:
            raise ValueError("this loop's ingest keeps no markets or research pieces")
        return
    for row in delta["markets"]:
        spec = SyntheticMarketSpec.model_validate(row["spec"])
        market = provider.generate(spec)
        if market.market_hash != row["market_hash"]:
            raise ValueError(f"market {len(memory.markets)} does not regenerate from its spec")
        memory.add_market(spec, market)
    ingest = _summary(record, "ingest")
    if ingest is not None:
        [row] = delta["markets"]
        if (ingest["market_hash"], ingest["spec_hash"]) != (
            row["market_hash"],
            memory.markets[-1].spec_hash,
        ):
            raise ValueError("the ingested market differs from the audit")
    for row in delta["research_data"]:
        market = memory.markets[row["market"]]
        count = row["identity"]["bars"]
        bars = tuple(market.bars[row["first"] : row["first"] + count])
        piece = ResearchPiece(row["identity"]["round"], market, bars)
        if len(bars) != count or _json(piece.identity()) != row["identity"]:
            raise ValueError("a research piece does not rebuild from its market")
        memory.research_data.append(piece)
    if ingest is not None and ingest["research_pieces"] != len(memory.research_data):
        raise ValueError("the accumulated research pieces differ from the audit")


def _restore_strategies(
    memory: ResearchMemory,
    rows: Sequence[Mapping[str, Any]],
    provider_for: Callable[[StrategySpec], Any] | None,
) -> None:
    for row in rows:
        spec = StrategySpec.model_validate(row["spec"])
        if spec.content_hash() != row["spec_hash"]:
            raise ValueError(f"{spec.ref} does not reproduce its spec hash")
        parent = memory.strategies.get(str(row["parent"]))
        if provider_for is None or parent is None:
            raise ValueError(f"{spec.ref} cannot be rebuilt: no evolution plan or no parent")
        risk_hash = None if parent.risk_policy is None else parent.risk_policy.content_hash()
        if (parent.hypothesis_family_id, risk_hash) != (
            row["hypothesis_family_id"],
            row["risk_policy_hash"],
        ):
            raise ValueError(f"{spec.ref} does not inherit its parent's family and risk policy")
        memory.add_strategy(
            StrategyCandidate(
                spec=spec,
                strategy=provider_for(spec),
                hypothesis_family_id=parent.hypothesis_family_id,
                risk_policy=parent.risk_policy,
                risk=parent.risk,
            )
        )


def _checked(model: Any, payload: Any, expected: Any, what: str) -> Any:
    value = model.model_validate(payload)
    if value.content_hash() != expected:
        raise ValueError(f"{what} does not reproduce its content hash")
    return value


def _report(payload: Any) -> ValidationReport | None:
    if payload is None:
        return None
    report: ValidationReport = _checked(
        ValidationReport, payload["record"], payload["hash"], "a validation report"
    )
    return report


def _restore_trials(memory: ResearchMemory, record: LoopRecord, delta: Any) -> None:
    index = record.round_index
    restored: list[TrialOutcome] = []
    for row in delta["trials"]:
        strategy = row["strategy"]
        candidate = None if strategy is None else memory.strategies[strategy]
        if candidate is not None and candidate.spec.content_hash() != row["strategy_hash"]:
            raise ValueError(f"trial strategy {strategy} is another spec in the catalog")
        cutoff = row["knowledge_cutoff"]
        restored.append(
            TrialOutcome(
                round_index=row["round_index"],
                hypothesis=_checked(
                    Hypothesis, row["hypothesis"], row["hypothesis_hash"], "a hypothesis"
                ),
                origin=row["origin"],
                experiment=_checked(
                    ExperimentSpec, row["experiment"], row["experiment_hash"], "an experiment"
                ),
                run=_checked(ExperimentRun, row["run"], row["run_hash"], "a run"),
                candidate=candidate,
                request_params=FrozenMapping(row["request_params"]),
                inputs=None,
                trial=None,
                validation_seed=row["validation_seed"],
                summary=row["summary"],
                error=row["error"],
                reason=None if row["reason"] is None else ReasonCode(row["reason"]),
                attempt=row["attempt"],
                knowledge_cutoff=None if cutoff is None else datetime.fromisoformat(cutoff),
            )
        )
    experiment = _summary(record, "experiment")
    if experiment is not None and [dict(o.summary) for o in restored] != experiment["experiments"]:
        raise ValueError("the restored trials differ from the audit's experiment rows")
    memory.trials.extend(restored)
    results: list[ValidationOutcome] = []
    for row in delta["validations"]:
        failure = row["failure_reason"]
        results.append(
            ValidationOutcome(
                round_index=row["round_index"],
                outcome=memory.trials[row["trial"]],
                report=_report(row["report"]),
                failure_reason=None if failure is None else ReasonCode(failure),
                sealed_report=_report(row["sealed_report"]),
                sealed_status=row["sealed_status"],
                summary=row["summary"],
                error=row["error"],
            )
        )
    validation = _summary(record, "validation")
    if validation is not None and [dict(v.summary) for v in results] != validation["reports"]:
        raise ValueError(f"the restored validations of round {index} differ from the audit")
    memory.validations.extend(results)


def _check_ledgers(memory: ResearchMemory, records: Sequence[LoopRecord]) -> None:
    """Cross-check 6: what the audit says happened is in the ledgers."""
    hypotheses = {str(h.ref): h for h in memory.ledger.hypotheses}
    trials = {(e.name, e.version, e.attempt) for e in memory.ledger.trial_log}
    lineage = {str(spec.ref): spec for spec in memory.lineage}
    approvals = {a.key: a for a in memory.reviews.approvals}
    failures = set(_failure_hashes(memory.failures.records()))

    def registered(ref: str, attempt: str | None, index: int) -> Hypothesis:
        hypothesis = hypotheses.get(ref)
        if hypothesis is None or (hypothesis.name, hypothesis.version, attempt) not in trials:
            what = ref if attempt is None else f"{ref} ({attempt})"
            raise _refuse(f"audit round {index} registered {what}, but the trial ledger has not")
        return hypothesis

    for record in records:
        index = record.round_index
        stage = _summary(record, "hypothesis")
        if stage is not None:
            for ref in stage["registered"]:
                registered(ref, None, index)
            for ref in stage["reevaluations"]:
                registered(ref, stage["reevaluation_attempt"], index)
        stage = _summary(record, "evolution")
        for row in [] if stage is None else stage["offspring"]:
            registered(row["hypothesis"], None, index)
            child = lineage.get(row["child"])
            if (
                child is None
                or child.content_hash() != row["child_spec_hash"]
                or row["parent"] not in lineage
            ):
                raise _refuse(f"audit round {index} evolved {row['child']}, not in the lineage")
        stage = _summary(record, "experiment")
        for row in [] if stage is None else stage["experiments"]:
            conditional = row.get("conditional")  # opt-in ConditionalPlan rows only
            for cell in [] if not conditional else conditional["cells"]:
                registered(cell["hypothesis"], conditional["attempt"], index)
        stage = _summary(record, "validation")
        for row in [] if stage is None else stage["reports"]:
            cells = row.get("conditional_cells")  # opt-in validate_cells rows only
            for cell in [] if not cells else cells["cells"]:
                registered(cell["hypothesis"], row["attempt"], index)
            sealed = row.get("sealed_oos") or {}
            if sealed.get("status") not in _UNSEALED:
                continue
            family = registered(row["hypothesis"], row["attempt"], index).family_id
            unsealing = memory.oos_ledger.get(family)
            if (
                unsealing is None
                or unsealing.approved_by != sealed.get("approved_by")
                or not memory.oos_ledger.is_evaluated(family)
            ):
                raise _refuse(
                    f"audit round {index} unsealed the sealed OOS window of {family}, but the "
                    "unsealing ledger does not hold that evaluated unsealing"
                )
        stage = _summary(record, "memory")
        missing = [
            h for h in ([] if stage is None else stage["failure_records"]) if h not in failures
        ]
        if missing:
            raise _refuse(f"audit round {index} filed failure records {missing} not in failures")
        for transition in record.transitions:
            subject = str(transition.subject)
            for evidence in transition.evidence:
                if not evidence.startswith("human_review:"):
                    continue
                key = subject.split(":", 1)[1]
                approval = approvals.get(key)
                if (
                    approval is None
                    or approval.reviewer != evidence.removeprefix("human_review:")
                    or key not in memory.reviews.taken
                ):
                    raise _refuse(
                        f"audit round {index} registered {subject} on {evidence!r}, but the "
                        "review queue holds no such approval"
                    )
