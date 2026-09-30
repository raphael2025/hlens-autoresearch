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
cleanly. Nothing but ``prepare`` / ``complete`` may run inside the scope: every other write of a
durable store — the sealed-OOS, lineage and failure stores included — is refused before it writes
while the lease is active (**One admission gate**).

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
write (``PlanAdmissionLease``).

Every durable store whose position the checkpoint records enters the same gate before it writes
(review fix, 2026-09-28): ``open_state`` binds the TrialLedger, the sealed-OOS unsealing ledger,
the lineage graph and the failure registry to it (``bind_write_gate``;
``research.persistence.gate``), and the review
queue already writes inside ``review_scope``. So no store write — through
``state.memory.ledger.register`` / ``register_batch`` / ``register_reevaluation`` from any thread,
``oos_ledger.record`` / ``mark_evaluated``, ``lineage_graph.add`` or ``failures.append`` — can run
between a checkpoint's (round, between-rounds or admission) reading of the store positions and its
memory line, nor between a round checkpoint and its audit record, nor between a PREPARE's position
check and its admission checkpoint. Outside those spans an ordinary store write (a round's stages)
takes the gate briefly and runs as before. While a lease is active or after an interrupted admission
every ordinary store write is refused before it writes (``LoopStateLocked`` /
``LoopStateInconsistent``); the only store write admitted then is the lease's own
``recover_register_batch(..., lease=...)``, whose TrialLedger lease the gate matches to the active
lease and its thread. Lock order is always gate → store lock → journal lock; no store enters the
gate while holding its own lock. No store hands out its writable journal, and neither does the
``MemoryCheckpoint``: positions and entries are read through ``journal_head()`` /
``journal_snapshot()`` (detached, read-only; the checkpoint also has ``header()``).

Store writes are round writes (review fix 2, 2026-09-28): the trial ledger, sealed-OOS, lineage and
failure stores, and the review queue's enqueue / take, are refused before they write unless the
audit has its open round (``LoopStateInconsistent``) — between rounds the opener accepts no line of
theirs past the last checkpoint, and a between-rounds checkpoint would record it. A human approval
stays the one write between rounds; before it is journaled ``before_approval`` also requires every
file (reviews and failures included) to be exactly where the memory journal's last line left it.
The audit is bound too (``LoopAuditLog.bind_write_scope``): every ``begin_round`` / ``append`` — the
loop's or a direct call on ``state.audit`` — runs with the gate held and is refused before it
writes while a lease is active, after an interrupted one, while an admission is unfinished or once
the state is closed, and a record is refused unless the memory journal's last line is that
round's checkpoint. Writes the opener makes before it binds the gate (headers, replay) never enter
it; its admission recovery runs after binding, inside the open round, through the lease.

**Closing** (review fix 2, 2026-09-28). Every write scope of the gate also requires ``state.lock``
to be held. Releasing it — ``DurableLoop.close()``, ``StateLock.release()`` by anyone, the opener's
failure path, or the collection of the restored audit log (``StateLock.release_collected``) — first
closes the gate for good (every later scope is refused, ``LoopStateLocked``), then drops the lock
only once no scope is in flight: ``release`` waits for another thread's scope to finish; a scope of
the releasing thread, or the collector's release (which never blocks), leaves the drop to the
outermost scope's exit. So a store, review, audit or checkpoint reference kept past ``close()``
(``state.memory``, ``loop.memory``, ``state.audit``) can no longer write, and no write runs after
the lock is released.

**Failed-round retry admission** (ADR-0083, state version 6, 2026-09-28). A v6 directory is a v4
directory (its plan admission journal keeps the v4 format and semantics) plus
``retry_admission/<failed record_hash>.jsonl``: one ``RetryJournal`` per retried failed round, each
holding at most ``retry_prepare`` → ``retry_commit`` (``research.loop.retry_admission``). Every
checkpoint's ``heads`` gains ``retry_admission``: the sorted positions of the non-empty retry
journals. ``DurableState.admit_failed_round_retry`` is the only writer. Between rounds, with the
state lock and the admission gate held (``_AdmissionGate.retry_scope``: no lease, no open round,
no other store write), after the final record is an experiment ``FAILED`` round whose ADR-0071
packet the caller rebuilt on this very state, it writes PREPARE (packet, reviewer, manifest,
TrialLedger baseline) → one TrialLedger ``reevaluate`` line per manifest item → COMMIT → a
``retry_admission`` memory line (``MemoryCheckpoint.retry_admission``) → the anchor. It never runs
a stage or a Provider. The manifest is checked before PREPARE: every item an already registered
hypothesis with exactly that content (G1), fresh attempt keys, and the declared trials within the
bound ``LoopBudget``'s round cap and remaining total (G2). The next round runs exactly those trials
(``HypothesisStage`` retry round) once the worker's fail-stop was lifted explicitly
(``ResearchLoop.authorize_failed_round_retry``).

Reopening a v6 directory verifies every retry journal (event shapes, file-name binding, the saved
packet against the packet rebuilt from the audit / memory / ledger prefix it was prepared on, the
exact ``reevaluate`` lines, COMMIT and checkpoint positions, G2) before anything is written. An
unfinished retry — PREPARE without COMMIT, or COMMIT without its checkpoint — is recovered only as
the directory's tail, only for the final failed record, and only by appending the unique missing
suffix the reducer names, then COMMIT, checkpoint and anchor; the reopening then ends refused
(reopen to continue; no Provider ran). Anything else is refused and every file is kept as it is.
v3 / v4 / v5 directories never contain retry state, and their bytes and reopening are unchanged.

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
from threading import Condition, Lock, RLock, get_ident
from typing import Any, Final, Protocol, cast

from apps.worker.loop import (
    ROUND_RECORDED,
    ROUND_STARTED,
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
from research.hypotheses import LedgerError, LedgerLease, TrialLedger
from research.hypotheses.typed_plan import TypedPlan
from research.hypotheses.typed_plan_audit import (
    CommittedAdmission,
    PlanAdmissionCorrupted,
    PlanAdmissionError,
    PlanAdmissionEvidence,
    PlanAdmissionJournal,
    PreparedAdmission,
    RoundStartedIdentity,
)
from research.loop.memory import REVIEW_APPROVED, ResearchMemory, ReviewApproval, ReviewQueue
from research.loop.retry_admission import (
    RETRY_DIR,
    RETRY_STATE_VERSION,
    RetryAdmissionError,
    RetryJournal,
    RetryJournals,
    RetryManifestItem,
    RetryRecovery,
    check_retry_budget,
    manifest_items,
    reduce_retry_ledger_tail,
    resolve_manifest,
    retry_commit_payload,
    retry_prepare_payload,
    retry_summary_rows,
    validate_reviewer,
)
from research.loop.segment import ResearchPiece
from research.loop.trials import TrialOutcome, ValidationOutcome
from research.persistence import (
    GENESIS_HASH,
    AppendOnlyJournal,
    JournalEntry,
    JournalSnapshot,
    detached_entry,
    journal_snapshot,
)
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
    "RETRY_ADMISSION",
    "RETRY_DIR",
    "RETRY_STATE_VERSION",
    "REVIEWS_FILE",
    "ROUND_MEMORY",
    "SEALED_OOS_FILE",
    "STATE_VERSION",
    "DurableState",
    "FileAnchor",
    "LoopStateInconsistent",
    "MemoryCheckpoint",
    "PlanAdmissionLease",
    "RetryAdmissionReceipt",
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
#: v6 only: the memory checkpoint of one completed failed-round retry admission (ADR-0083).
RETRY_ADMISSION: Final = "retry_admission"
#: Anchor journal line type (``FileAnchor``).
ANCHOR_HEAD: Final = "loop_state_head"
#: Current memory/checkpoint layout. v3 remains a read/write compatibility path with its original
#: checkpoint shape; v4 adds typed-plan admission and operator-only v5 binds operator identity.
LEGACY_STATE_VERSION: Final = 3
STATE_VERSION: Final = 4
OPERATOR_STATE_VERSION: Final = 5
#: ``RETRY_STATE_VERSION`` (6, imported above): v4 plus ADR-0083 failed-round retry admission.
#: The versions with a plan admission journal (v6 keeps its v4 format: ``_PLAN_FORMAT``).
_ADMISSION_VERSIONS: Final = frozenset({STATE_VERSION, OPERATOR_STATE_VERSION, RETRY_STATE_VERSION})
_PLAN_FORMAT: Final = {STATE_VERSION: 4, OPERATOR_STATE_VERSION: 5, RETRY_STATE_VERSION: 4}
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
        "round",
        "transaction_id",
        "prepare_seq",
        "prepare_hash",
        "commit_seq",
        "commit_hash",
        "ledger_event_seq",
        "ledger_event_hash",
        "heads",
    }
)
#: The fields of a v6 ``retry_admission`` memory line (``MemoryCheckpoint.retry_admission``).
_RETRY_ADMISSION_KEYS: Final = frozenset(
    {
        "retry_id",
        "failed_record_hash",
        "prepare_seq",
        "prepare_hash",
        "commit_seq",
        "commit_hash",
        "heads",
    }
)
#: The only store write a retry scope admits: ``TrialLedger.register_reevaluation`` (its gate
#: label). Anything else inside the scope is refused before it writes.
_RETRY_WRITE_PREFIX: Final = "a re-evaluation of "
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
    """The held ``state.lock`` of one opened directory; ``release`` is idempotent.

    Bound to its state's admission gate (``open_state``), every release path — ``release``
    (``DurableLoop.close``, the opener's failure path, a caller) and the release when the restored
    audit log is garbage-collected — first closes the gate for good and lets every write scope in
    flight finish, and only then drops the ``flock`` (module docs, **Closing**).
    """

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
        self._fd_lock = Lock()
        self._gate: _AdmissionGate | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def bind_gate(self, gate: _AdmissionGate) -> None:
        """Serialize every later release with ``gate`` (once; ``open_state``)."""
        if self._gate is not None and self._gate is not gate:
            raise ValueError("the state lock is already bound to an admission gate")
        self._gate = gate

    def release(self) -> None:
        """Close the bound gate, wait for the write scopes of other threads to finish, then drop
        the lock (a scope of the calling thread defers the drop to its outermost exit)."""
        self._close(wait=True)

    def release_collected(self) -> None:
        """``release`` for a garbage-collection finalizer: never blocks (any thread may run it);
        a write scope in flight drops the lock at its outermost exit instead."""
        self._close(wait=False)

    def _close(self, *, wait: bool) -> None:
        gate = self._gate
        if gate is None:
            self._unlock()
        else:
            gate.close(self._unlock, wait=wait)

    def _unlock(self) -> None:
        with self._fd_lock:
            fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class _AdmissionGate:
    """In-process admission exclusion of one opened directory (module docs, **Typed-plan
    admission lease**, **One admission gate**, **Closing**): ``owner`` is the active
    ``PlanAdmissionLease``; ``poisoned`` says why the state refuses every later write after an
    interrupted admission; ``closed`` says why it refuses every later write once its
    ``state.lock`` is released (or being released).

    Every holder enters through ``hold`` (never ``lock`` directly), which refuses a new scope once
    the gate is closed or the state lock is no longer held, and counts the scopes in flight so a
    release can wait for them (``close``).
    """

    __slots__ = (
        "_depth",
        "_holder",
        "_idle",
        "_mutex",
        "_pending_release",
        "audit",
        "closed",
        "lock",
        "owner",
        "owner_thread",
        "owner_token",
        "poisoned",
        "retry_thread",
        "state_lock",
    )

    def __init__(self, state_lock: StateLock | None) -> None:
        self.lock = RLock()
        self.owner: object | None = None
        #: The active lease's thread and its TrialLedger ``LedgerLease`` (``write_scope``).
        self.owner_thread: int | None = None
        self.owner_token: object | None = None
        self.poisoned: str | None = None
        #: The thread inside ``retry_scope`` (ADR-0083); ``None``: no retry admission runs.
        self.retry_thread: int | None = None
        self.closed: str | None = None
        #: The directory's single-writer lock; ``None``: no scope is ever admitted.
        self.state_lock = state_lock
        #: The restored audit (weakly: the gate never keeps it alive, so its collection still
        #: releases the lock): its open round admits ordinary store writes (``write_scope``).
        self.audit: weakref.ref[LoopAuditLog] | None = None
        #: ``_depth`` scopes in flight, all of thread ``_holder`` (they hold ``lock``). Re-entrant:
        #: a collector finalizer (``StateLock.release_collected``) may run on a thread inside it.
        self._mutex = RLock()
        self._idle = Condition(self._mutex)
        self._depth = 0
        self._holder: int | None = None
        self._pending_release: Callable[[], None] | None = None

    @contextmanager
    def hold(self, what: str, *, closing: bool = False) -> Iterator[None]:
        """The gate, held across one scope. Refused (``LoopStateLocked``) once the gate is closed
        or the state lock is not held — except a scope nested in one already in flight on this
        thread (it finishes the write the release waits for). ``closing=True`` only for the
        end of an admission lease, which must release the in-process locks even then."""
        with self.lock:
            with self._mutex:
                if not closing:
                    self._require_live(what)
                if self._depth == 0:
                    self._holder = get_ident()
                self._depth += 1
            try:
                yield
            finally:
                with self._mutex:
                    self._depth -= 1
                    release: Callable[[], None] | None = None
                    if self._depth == 0:
                        self._holder = None
                        release, self._pending_release = self._pending_release, None
                        self._idle.notify_all()
                if release is not None:
                    release()

    def _require_live(self, what: str) -> None:
        """(``_mutex`` held) Refuse ``what`` once closed / without the held state lock."""
        nested = self._depth > 0 and self._holder == get_ident()
        if self.closed is not None and not nested:
            raise LoopStateLocked(f"{what} is refused: {self.closed}")
        if self.state_lock is None or not self.state_lock.held:
            raise LoopStateLocked(f"{what} requires the held loop state lock")

    def close(self, release: Callable[[], None], *, wait: bool) -> None:
        """Refuse every later scope for good, then run ``release`` (drop the ``flock``) once no
        scope is in flight: after waiting for another thread's scopes (``wait``), or — a scope of
        this thread, or ``wait=False`` — at the outermost scope's exit."""
        with self._mutex:
            if self.closed is None:
                self.closed = (
                    "the loop state lock was released (the loop was closed or dropped); reopen "
                    "the state directory to continue"
                )
            if wait and self._holder != get_ident():
                while self._depth > 0:
                    self._idle.wait()
            if self._depth > 0:
                self._pending_release = release
                return
        release()

    def open_round(self) -> int | None:
        """The restored audit's round started and not recorded (``None``: none, or no audit)."""
        audit = None if self.audit is None else self.audit()
        return None if audit is None else audit.open_round

    def round_open(self) -> bool:
        """Whether the restored audit has a round started and not recorded."""
        return self.open_round() is not None

    @contextmanager
    def write_scope(self, what: str, token: object | None = None) -> Iterator[None]:
        """``research.persistence.WriteGate``: one durable store write with the gate held, refused
        before the store writes anything (module docs, **One admission gate**).

        Every write is refused once the gate is closed or the state lock is not held, and outside
        the unique open round of the audit: between rounds no store but the review queue (whose
        approvals write between-rounds checkpoints; ``review_scope``) may move, since the opener
        refuses any such tail. An ordinary write (``token=None``) is refused while a lease is
        active or after an interrupted admission. A ``token`` is admitted only as the active
        lease's own TrialLedger lease, presented from the thread that entered that lease's scope.
        """
        with self.hold(what):
            if token is None:
                self.require_open(what)
            else:
                self.require_open(what, self.owner)
                if (
                    self.owner is None
                    or token is not self.owner_token
                    or get_ident() != self.owner_thread
                ):
                    raise LoopStateLocked(
                        f"{what} requires this state's active typed-plan admission lease (its "
                        "own TrialLedger lease, from the thread that holds it)"
                    )
            if self.retry_thread is not None and self.retry_thread == get_ident():
                # ADR-0083: inside ``retry_scope`` (between rounds, gate held by this thread)
                # only the retry's own TrialLedger re-evaluations are written.
                if token is not None or not what.startswith(_RETRY_WRITE_PREFIX):
                    raise LoopStateLocked(
                        f"{what} is refused: a failed-round retry admission writes only its "
                        "TrialLedger re-evaluations"
                    )
                if self.round_open():
                    raise LoopStateInconsistent(
                        f"{what} is refused: a retry admission never writes during a round"
                    )
            else:
                self.require_round_open(what)
            yield

    @contextmanager
    def retry_scope(self) -> Iterator[None]:
        """ADR-0083: the gate, held by this thread across one whole failed-round retry admission
        (PREPARE → TrialLedger re-evaluations → COMMIT → checkpoint → anchor), or across the
        opener's exact recovery of one. Refused unless the state is idle between rounds: no
        active or interrupted typed-plan lease, no other retry, no open round."""
        what = "a failed-round retry admission"
        with self.hold(what):
            self.require_open(what)
            if self.owner is not None or self.retry_thread is not None:
                raise LoopStateLocked(f"{what} is refused: another admission is active")
            if self.round_open():
                raise LoopStateInconsistent(f"{what} is refused: a loop round is open")
            self.retry_thread = get_ident()
            try:
                yield
            finally:
                self.retry_thread = None

    def require_round_open(self, what: str) -> None:
        """Refuse ``what`` unless the audit has its unique open round (store writes are round
        writes; module docs, **Approvals between rounds**)."""
        if not self.round_open():
            raise LoopStateInconsistent(
                f"{what} is refused: no loop round is open, and between rounds only a human "
                "approval (with its between-rounds checkpoint) may be written; reopening would "
                "refuse any other line past the last checkpoint"
            )

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
    """On-disk admission file sizes (``-1``: missing); partial writes count."""
    sizes: list[int] = []
    for name in (PLAN_ADMISSION_FILE, LEDGER_FILE, MEMORY_FILE):
        try:
            sizes.append(os.stat(root / name).st_size)
        except FileNotFoundError:
            sizes.append(-1)
    return tuple(sizes)


def _retry_file_sizes(root: Path, journal: RetryJournal) -> tuple[int, ...]:
    """On-disk sizes of the files a retry admission appends to (``-1``: missing); an unreadable
    size counts as a write (the caller poisons the state)."""
    sizes: list[int] = []
    for path in (journal.path, root / LEDGER_FILE, root / MEMORY_FILE):
        try:
            sizes.append(os.stat(path).st_size)
        except FileNotFoundError:
            sizes.append(-1)
        except OSError:
            sizes.append(-2)
    return tuple(sizes)


# ------------------------------------------------------------------------------------ positions


def _stores(memory: ResearchMemory) -> tuple[LineageGraph, DurableUnsealingLedger]:
    """The lineage graph and unsealing ledger of a journal-backed memory (refused otherwise).

    No store hands out its writable journal (module docs, **One admission gate**): positions and
    entries are read only through each store's ``journal_head()`` / ``journal_snapshot()``.
    """
    graph, oos = memory.lineage_graph, memory.oos_ledger
    if (
        not memory.ledger.durable
        or not memory.reviews.durable
        or graph is None
        or not graph.durable
        or not isinstance(oos, DurableUnsealingLedger)
    ):
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    return graph, oos


def _durable[T](value: T | None) -> T:
    """A store's journal view or position; ``None`` (an in-memory store) is refused."""
    if value is None:
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    return value


def _review_snapshot(memory: ResearchMemory) -> JournalSnapshot:
    return _durable(memory.reviews.journal_snapshot())


def _journals(
    memory: ResearchMemory, admission: PlanAdmissionJournal | None = None
) -> dict[str, JournalSnapshot | PlanAdmissionJournal]:
    """Every positioned journal, read-only: each store's as a detached snapshot."""
    graph, oos = _stores(memory)
    journals: dict[str, JournalSnapshot | PlanAdmissionJournal] = {
        "trial_ledger": _durable(memory.ledger.journal_snapshot()),
        "sealed_oos": oos.journal_snapshot(),
        "lineage": _durable(graph.journal_snapshot()),
        "reviews": _review_snapshot(memory),
    }
    if admission is not None:
        journals["plan_admission"] = admission
    return journals


def _failure_hashes(records: Sequence[FailureRecord]) -> list[str]:
    return [record.content_hash() for record in records]


def heads(
    memory: ResearchMemory,
    admission: PlanAdmissionJournal | None = None,
    retry: RetryJournals | None = None,
) -> dict[str, Any]:
    """The position of every file of the state directory but the audit and the memory journal.

    Callers that append a line naming these positions hold the admission gate, which every store
    write enters first (module docs, **One admission gate**): no store moves between this read and
    that append.
    """
    graph, oos = _stores(memory)
    positions: dict[str, tuple[int, str]] = {
        "trial_ledger": _durable(memory.ledger.journal_head()),
        "sealed_oos": oos.journal_head(),
        "lineage": _durable(graph.journal_head()),
        "reviews": _durable(memory.reviews.journal_head()),
    }
    if admission is not None:
        positions["plan_admission"] = (len(admission.entries), admission.head_hash)
    out: dict[str, Any] = {
        name: {"seq": seq, "hash": head} for name, (seq, head) in positions.items()
    }
    hashes = _failure_hashes(memory.failures.records())
    out["failures"] = {"count": len(hashes), "digest": content_hash(hashes)}
    if retry is not None:  # v6 only: the sorted positions of every non-empty retry journal
        out["retry_admission"] = retry.positions()
    return out


def _checkpointed_heads(
    entries: Sequence[JournalEntry],
    admission: PlanAdmissionJournal | None,
    retry: RetryJournals | None = None,
) -> dict[str, Any]:
    """Where the memory journal's last line leaves every other file of the directory.

    The header names no positions; for it this is the genesis: every journal empty (v4 / v5: but
    the plan admission journal's header line), no failure records (v6: no retry journal).
    ``admission=None``: a v3 directory (no plan admission journal).
    """
    last = entries[-1]
    if last.type != LOOP_STATE_OPENED:
        return cast(dict[str, Any], _json(dict(last.payload["heads"])))
    out: dict[str, Any] = {name: {"seq": 0, "hash": GENESIS_HASH} for name, _ in _JOURNALS}
    if admission is not None:
        out["plan_admission"] = {"seq": 1, "hash": admission.entries[0].hash}
    out["failures"] = {"count": 0, "digest": content_hash([])}
    if retry is not None:
        out["retry_admission"] = []
    return cast(dict[str, Any], _json(out))


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
        RETRY_STATE_VERSION,
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
    return sum(1 for entry in _review_snapshot(memory).entries if entry.type == REVIEW_APPROVED)


class MemoryCheckpoint:
    """``ResearchLoop(checkpoint=...)``: one ``round_memory`` line per finished round; plus one
    ``between_rounds`` line per human approval between rounds (``between_rounds``).

    The memory journal is only ever appended here, with the admission gate held; it is never
    handed out: ``journal_head()`` / ``journal_snapshot()`` are detached read-only views.
    """

    def __init__(
        self,
        journal: AppendOnlyJournal,
        memory: ResearchMemory,
        admission: PlanAdmissionJournal | None = None,
        state_lock: StateLock | None = None,
        retry: RetryJournals | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self._admission = admission
        #: v6 only: the directory's retry journals (ADR-0083); ``None`` for v3 / v4 / v5.
        self._retry = retry
        self._state_lock = state_lock
        #: In-process admission exclusion, shared with the ``DurableState`` (module docs).
        self.admission_gate = _AdmissionGate(state_lock)
        self._marks = _Marks.of(memory)
        self._covered = _approval_lines(memory)  # opening verified every one is checkpointed
        #: Transactions a ``plan_admission`` line of this memory journal already names.
        self._checkpointed = {
            entry.payload.get("transaction_id")
            for entry in journal.entries
            if entry.type == PLAN_ADMISSION
        }
        #: Failed records whose retry a ``retry_admission`` line of this journal already names.
        self._retried = {
            entry.payload.get("failed_record_hash")
            for entry in journal.entries
            if entry.type == RETRY_ADMISSION
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
        retry = self._retry
        if retry is not None:
            unfinished = [
                journal.failed_record_hash
                for journal in retry.journals
                if journal.failed_record_hash not in self._retried
            ]
            if unfinished:
                raise LoopStateInconsistent(
                    f"{what}: the failed-round retry admission of {unfinished} has no memory "
                    "checkpoint (close and reopen the state directory to recover it)"
                )
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

    def require_checkpointed(self, what: str) -> None:
        """Refuse ``what`` unless every file is exactly where the memory journal's last line left
        it (the review journal and the failure registry included; with the gate held).

        Between rounds nothing but an approval and its own between-rounds line may move a file,
        and its checkpoint would record any other tail, which reopening refuses for good: this
        is checked before the approval is journaled (fail closed).
        """
        current = _json(heads(self._memory, self._admission, self._retry))
        last = _checkpointed_heads(self._journal.entries, self._admission, self._retry)
        moved = sorted(name for name in current if last.get(name) != current[name])
        if moved:
            raise LoopStateInconsistent(
                f"{what} is refused: {moved} moved since the memory journal's last line (a "
                "write outside a round, or past its store); a between-rounds checkpoint would "
                "record that tail and reopening would refuse it — a human reviews the directory"
            )

    @contextmanager
    def audit_write_scope(
        self, line_type: str, round_index: int, record_hash: str | None
    ) -> Iterator[None]:
        """``apps.worker.AuditWriteScope`` of the restored audit (module docs, **One admission
        gate**): every ``begin_round`` / ``append`` — the loop's own or a direct call — runs with
        the gate held and is refused before it writes once the gate is closed or the state lock
        released, while a lease is active, after an interrupted one or while an admission is
        unfinished; a record is refused unless the memory journal's last line is that round's
        checkpoint (``round_index`` and ``record_hash``)."""
        what = f"audit line {line_type} of round {round_index}"
        with self.admission_gate.hold(what):
            self.require_settled(what)
            if line_type == ROUND_RECORDED:
                last = self._journal.entries[-1]
                if (
                    last.type != ROUND_MEMORY
                    or last.payload.get("round_index") != round_index
                    or last.payload.get("record_hash") != record_hash
                ):
                    raise LoopStateInconsistent(
                        f"{what} is refused: the memory journal's last line is not this round's "
                        "checkpoint (a round is recorded only right after its checkpoint)"
                    )
            elif line_type != ROUND_STARTED:
                raise LoopStateInconsistent(f"{what} is not a loop audit line")
            yield

    def journal_head(self) -> tuple[int, str]:
        """``(line count, chain head)`` of the memory journal."""
        entries = self._journal.entries  # one consistent tuple: count and head from one instant
        return len(entries), (entries[-1].hash if entries else GENESIS_HASH)

    def journal_snapshot(self) -> JournalSnapshot:
        """Detached read-only copy of the memory journal's verified lines (never the journal)."""
        return journal_snapshot(self._journal)

    def header(self) -> JournalEntry:
        """Detached copy of the memory journal's first line (the ``loop_state_opened`` header)."""
        return detached_entry(self._journal.entries[0])

    def _entries(self) -> tuple[JournalEntry, ...]:
        """The memory journal's lines, for this module's read-only checks (never mutated)."""
        return self._journal.entries

    def __call__(self, record: LoopRecord) -> None:
        memory = self._memory
        with self.admission_gate.hold(f"round {record.round_index} checkpoint"):
            self.require_settled(f"round {record.round_index} checkpoint")
            # a direct call too: only the open round, once (reopening refuses anything else)
            if (
                self.admission_gate.open_round() != record.round_index
                or sum(1 for entry in self._journal.entries if entry.type == ROUND_MEMORY)
                != record.round_index
            ):
                raise LoopStateInconsistent(
                    f"round {record.round_index} checkpoint is refused: it is not the audit's "
                    "open round, or that round is already checkpointed"
                )
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
                    "heads": heads(memory, self._admission, self._retry),
                    "delta": _delta(memory, self._marks),
                },
            )
            self._marks = _Marks.of(memory)

    def between_rounds(self, rounds: int, audit_head: str | None, approval: ReviewApproval) -> None:
        """Checkpoint the human approval just journaled (the review journal's last line)."""
        with self.admission_gate.hold("between-rounds checkpoint"):
            self.require_settled("between-rounds checkpoint")
            if self.admission_gate.round_open():
                raise LoopStateInconsistent(
                    "a between-rounds checkpoint is refused while a loop round is open"
                )
            line = _review_snapshot(self._memory).entries[-1]
            if line.type != REVIEW_APPROVED or line.payload.get("key") != approval.key:
                raise LoopStateInconsistent("the review journal's last line is not this approval")
            # exactly this one uncovered approval line moved since the last memory line
            last = _checkpointed_heads(self._journal.entries, self._admission, self._retry)
            current = _json(heads(self._memory, self._admission, self._retry))
            moved = sorted(name for name in current if last.get(name) != current[name])
            if (
                _approval_lines(self._memory) != self._covered + 1
                or moved != ["reviews"]
                or last["reviews"]["seq"] + 1 != line.seq
            ):
                raise LoopStateInconsistent(
                    "a between-rounds checkpoint names exactly one new approval, the only line "
                    f"written since the memory journal's last line (moved: {moved})"
                )
            self._journal.append(
                BETWEEN_ROUNDS,
                {
                    "rounds": rounds,
                    "audit_head": audit_head,
                    "heads": heads(self._memory, self._admission, self._retry),
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
        with self.admission_gate.hold("plan admission checkpoint"):
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
        expected = _checkpointed_heads(self._journal.entries, self._admission, self._retry)
        expected["trial_ledger"] = {
            "seq": committed.ledger_event_seq,
            "hash": committed.ledger_event_hash,
        }
        expected["plan_admission"] = {"seq": committed.seq, "hash": committed.entry_hash}
        current = _json(heads(self._memory, self._admission, self._retry))
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

    def retry_admission(self, journal: RetryJournal) -> JournalEntry:
        """Checkpoint one committed failed-round retry (ADR-0083; v6 only, gate held).

        Accepted only when the TrialLedger moved from the PREPARE baseline (the previous memory
        line's position) to exactly the COMMIT's ledger head, the retry journals moved by exactly
        this journal's PREPARE and COMMIT, nothing else moved, and the COMMIT predicted this line's
        sequence number — what the opener requires of the line.
        """
        with self.admission_gate.hold("retry admission checkpoint"):
            what = "retry admission checkpoint"
            self.admission_gate.require_open(what)
            retry = self._retry
            if retry is None:
                raise LoopStateInconsistent(f"{what}: failed-round retry requires v6 state")
            if self._state_lock is None or not self._state_lock.held:
                raise LoopStateLocked(f"{what} requires the held loop state lock")
            if self.admission_gate.round_open():
                raise LoopStateInconsistent(f"{what} is refused while a loop round is open")
            failed = journal.failed_record_hash
            prepare, commit = journal.prepare, journal.commit
            if retry.get(failed) is not journal or prepare is None or commit is None:
                raise LoopStateInconsistent(f"{what} requires this state's committed retry")
            if failed in self._retried:
                raise LoopStateInconsistent(f"{what}: this retry already has its checkpoint")
            last = _checkpointed_heads(self._journal.entries, self._admission, retry)
            baseline = {
                "seq": prepare.payload["ledger_baseline_seq"],
                "hash": prepare.payload["ledger_baseline_hash"],
            }
            if last["trial_ledger"] != baseline:
                raise LoopStateInconsistent(
                    f"{what}: the PREPARE baseline is not where the last memory line left the "
                    "TrialLedger"
                )
            expected = dict(last)
            expected["trial_ledger"] = {
                "seq": commit.payload["ledger_head_seq"],
                "hash": commit.payload["ledger_head_hash"],
            }
            expected["retry_admission"] = _json(
                sorted(
                    [*last["retry_admission"], journal.position()],
                    key=lambda position: str(position["failed_record_hash"]),
                )
            )
            current = _json(heads(self._memory, self._admission, retry))
            if current != expected:
                moved = sorted(
                    k for k in set(current) | set(expected) if current.get(k) != expected.get(k)
                )
                raise LoopStateInconsistent(
                    f"{what}: {moved} are not where the previous memory line plus this retry's "
                    "TrialLedger re-evaluations and COMMIT leave them"
                )
            seq = len(self._journal.entries) + 1
            if commit.payload["memory_checkpoint_seq"] != seq:
                raise LoopStateInconsistent(f"{what}: the COMMIT predicted another memory line")
            entry = self._journal.append(
                RETRY_ADMISSION,
                {
                    "retry_id": prepare.payload["retry_id"],
                    "failed_record_hash": failed,
                    "prepare_seq": prepare.seq,
                    "prepare_hash": prepare.hash,
                    "commit_seq": commit.seq,
                    "commit_hash": commit.hash,
                    "heads": current,
                },
            )
            self._retried.add(failed)
            return entry


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


@dataclass(frozen=True, slots=True)
class RetryAdmissionReceipt:
    """Proof of one completed, checkpointed ADR-0083 retry admission of the final failed round.

    The worker (``ResearchLoop.authorize_failed_round_retry``) lifts the ADR-0070 fail-stop only
    for a receipt naming its audit head; it is a durable fact of this state, not a credential.
    """

    retry_id: str
    failed_record_hash: str
    commit_seq: int
    commit_hash: str
    checkpoint_seq: int
    checkpoint_hash: str


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
    #: when the audit log is garbage-collected, or at process exit; each release closes the
    #: admission gate first: module docs, **Closing**).
    lock: StateLock | None = None
    #: v6 only: the directory's retry journals (ADR-0083); ``None`` for v3 / v4 / v5.
    _retry_journals: RetryJournals | None = None

    # -- ADR-0083 failed-round retry admission (v6) --------------------------------------------

    def admit_failed_round_retry(
        self,
        *,
        packet: object,
        reviewer: str,
        manifest: Sequence[RetryManifestItem],
    ) -> RetryAdmissionReceipt:
        """Durably admit one explicit, human-reviewed retry of the final failed experiment round.

        ``packet``: the ADR-0071 ``FailedRoundReviewPacket`` rebuilt on this opened state (a stale
        or foreign one is refused); ``reviewer``: a non-empty human declaration (an automation
        identity is refused; it is not authenticated); ``manifest``: the ordered trials to run
        again — each an already registered hypothesis with exactly its content hash (G1) and a
        fresh attempt key, declared within the bound budget (G2).

        Writes PREPARE → one TrialLedger ``reevaluate`` per item → COMMIT → the ``retry_admission``
        memory line → the anchor, all with the admission gate held (module docs). Runs no stage and
        no Provider; the returned receipt is what ``ResearchLoop.authorize_failed_round_retry``
        needs to let the next round run the admitted trials. Every refusal happens before the
        first byte; a failure after it poisons this state (close and reopen: the opener recovers
        the exact transaction). Raises ``RetryAdmissionError`` for the request and
        ``LoopStateInconsistent`` / ``LoopStateLocked`` for the state.
        """
        retry = self._retry_journals
        if self.state_version != RETRY_STATE_VERSION or retry is None:
            raise LoopStateInconsistent(
                "failed-round retry admission requires a v6 state directory (ADR-0083); "
                f"this one is v{self.state_version}"
            )
        if self.lock is None or not self.lock.held:
            raise LoopStateLocked("failed-round retry admission requires the held loop state lock")
        gate = self.checkpoint.admission_gate
        with gate.retry_scope():
            journal, payload, pairs = self._retry_request(retry, packet, reviewer, manifest)
            sizes = _retry_file_sizes(self.root, journal)
            try:
                prepare = journal.append_prepare(payload)
                ledger = self.memory.ledger
                for hypothesis, attempt in pairs:
                    if not ledger.register_reevaluation(hypothesis, attempt):
                        raise RetryAdmissionError(f"retry attempt {attempt!r} is registered")
                snapshot = _durable(ledger.journal_snapshot())
                recovery = reduce_retry_ledger_tail(
                    journal, snapshot.entries, loop_id=str(payload["loop_id"])
                )
                if recovery.missing_items:
                    raise RetryAdmissionError("the TrialLedger misses a retry re-evaluation")
                commit = journal.append_commit(
                    retry_commit_payload(
                        prepare,
                        recovery.existing_entries,
                        memory_checkpoint_seq=len(self.checkpoint._entries()) + 1,
                    )
                )
                line = self.checkpoint.retry_admission(journal)
                self.publish_anchor()
            except BaseException:
                if _retry_file_sizes(self.root, journal) != sizes:
                    gate.poisoned = (
                        "a failed-round retry admission stopped after writing part of its "
                        "PREPARE → TrialLedger → COMMIT → checkpoint → anchor sequence"
                    )
                raise
            self.memory.retry_reevaluations[:] = list(pairs)
            return RetryAdmissionReceipt(
                retry_id=str(payload["retry_id"]),
                failed_record_hash=journal.failed_record_hash,
                commit_seq=commit.seq,
                commit_hash=commit.hash,
                checkpoint_seq=line.seq,
                checkpoint_hash=line.hash,
            )

    def _retry_request(
        self,
        retry: RetryJournals,
        packet: object,
        reviewer: str,
        manifest: Sequence[RetryManifestItem],
    ) -> tuple[RetryJournal, dict[str, Any], tuple[tuple[Hypothesis, str], ...]]:
        """Every read-only check of ``admit_failed_round_retry`` (gate held), before any byte."""
        what = "failed-round retry admission"
        if self.audit.open_round is not None:
            raise LoopStateInconsistent(f"{what} is refused: round {self.audit.open_round} is open")
        records = self.audit.records
        if _failed_experiment_round(self.audit) is None:
            raise LoopStateInconsistent(
                f"{what} is refused: the final audit record is not a failed experiment round"
            )
        failed = records[-1]
        known = retry.get(failed.record_hash)
        if known is not None and known.entries:
            raise LoopStateInconsistent(
                f"{what} is refused: round {failed.round_index} already has a retry admission (a "
                "completed admission is never consumed twice)"
            )
        if self.memory.retry_reevaluations:
            raise LoopStateInconsistent(f"{what} is refused: an admitted retry has not run yet")
        self.checkpoint.require_settled(what)
        self.checkpoint.require_checkpointed(what)
        if self.anchor is not None and self.anchor.load() != self.head():
            raise LoopStateInconsistent(
                f"{what} requires the external anchor to hold this directory's current head"
            )
        from research.loop.recovery_review import (
            FailedRoundReviewPacket,
            FailedRoundReviewRefused,
            failed_round_review_packet,
        )

        if not isinstance(packet, FailedRoundReviewPacket):
            raise RetryAdmissionError(
                "a retry needs the ADR-0071 review packet of the failed round (no review, no "
                "admission)"
            )
        try:
            current = failed_round_review_packet(self)
        except FailedRoundReviewRefused as exc:
            raise LoopStateInconsistent(f"{what}: no current review packet: {exc}") from exc
        if current is None or packet != current:
            raise RetryAdmissionError(
                "the review packet is stale or not this state's failed round: rebuild it"
            )
        declared = validate_reviewer(reviewer)
        items = manifest_items(manifest)
        ledger = self.memory.ledger
        hypotheses = resolve_manifest(ledger.hypotheses, items)  # G1, before PREPARE
        used = {entry.attempt for entry in ledger.trial_log if entry.attempt is not None}
        reused = sorted(item.attempt for item in items if item.attempt in used)
        if reused:
            raise RetryAdmissionError(f"retry attempt keys {reused} are already in the TrialLedger")
        fingerprint = self.checkpoint.header().payload.get("fingerprint")
        if not isinstance(fingerprint, Mapping) or not isinstance(fingerprint.get("loop_id"), str):
            raise LoopStateInconsistent(f"{what}: the state header binds no loop fingerprint")
        check_retry_budget(  # G2, before PREPARE
            fingerprint.get("budget"),
            total_trials_spent=failed.total_usage.trials,
            requested=len(items),
        )
        baseline = _durable(ledger.journal_head())
        journal = retry.create(failed.record_hash)
        payload = retry_prepare_payload(
            loop_id=fingerprint["loop_id"],
            packet=current.payload(),
            packet_hash=current.packet_hash,
            failed_record_hash=failed.record_hash,
            reviewer=declared,
            manifest=items,
            ledger_baseline=baseline,
        )
        pairs = tuple(zip(hypotheses, (item.attempt for item in items), strict=True))
        return journal, payload, pairs

    def failed_round_retry_receipt(self) -> RetryAdmissionReceipt:
        """The receipt of the completed retry admission of the final failed round, after a
        reopening (``DurableLoop.resume_failed_round_retry``); refused once a later round ran it.
        """
        retry = self._retry_journals
        what = "activating a failed-round retry"
        if self.state_version != RETRY_STATE_VERSION or retry is None:
            raise LoopStateInconsistent(f"{what} requires a v6 state directory (ADR-0083)")
        gate = self.checkpoint.admission_gate
        with gate.hold(what):
            gate.require_open(what)
            self.checkpoint.require_settled(what)
            if self.audit.open_round is not None or _failed_experiment_round(self.audit) is None:
                raise LoopStateInconsistent(
                    f"{what} is refused: the final audit record is not a failed experiment round"
                )
            failed = self.audit.records[-1]
            journal = retry.get(failed.record_hash)
            line = next(
                (
                    entry
                    for entry in self.checkpoint._entries()
                    if entry.type == RETRY_ADMISSION
                    and entry.payload["failed_record_hash"] == failed.record_hash
                ),
                None,
            )
            if (
                journal is None
                or journal.prepare is None
                or journal.commit is None
                or line is None
                or (line.payload["prepare_hash"], line.payload["commit_hash"])
                != (journal.prepare.hash, journal.commit.hash)
            ):
                raise LoopStateInconsistent(
                    f"{what} is refused: round {failed.round_index} has no completed retry "
                    "admission"
                )
            if not self.memory.retry_reevaluations:
                raise LoopStateInconsistent(f"{what} is refused: its trials were already run")
            if self.anchor is not None and self.anchor.load() != self.head():
                raise LoopStateInconsistent(
                    f"{what} requires the external anchor to hold this directory's current head"
                )
            return RetryAdmissionReceipt(
                retry_id=str(journal.prepare.payload["retry_id"]),
                failed_record_hash=failed.record_hash,
                commit_seq=journal.commit.seq,
                commit_hash=journal.commit.hash,
                checkpoint_seq=line.seq,
                checkpoint_hash=line.hash,
            )

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
        if self.state_version not in _ADMISSION_VERSIONS or (self._plan_admission is None):
            raise LoopStateInconsistent(
                f"typed-plan admission is disabled in state version {self.state_version}"
            )
        if self.lock is None or not self.lock.held:
            raise LoopStateLocked("typed-plan admission requires the held loop state lock")
        gate = self.checkpoint.admission_gate
        ledger = self.memory.ledger
        with gate.hold("a typed-plan admission lease"):
            gate.require_open("a typed-plan admission lease")
            gate.require_round_open("a typed-plan admission lease")
            try:
                ledger_lease = ledger.acquire_write_lease()
            except LedgerError as exc:
                raise LoopStateLocked(f"a typed-plan admission lease is refused: {exc}") from exc
            try:
                lease = PlanAdmissionLease(self, ledger_lease, _admission_file_sizes(self.root))
            except BaseException:
                ledger.release_write_lease(ledger_lease)
                raise
            gate.owner, gate.owner_token, gate.owner_thread = lease, ledger_lease, lease._thread
        try:
            yield lease
        except BaseException:
            self._end_admission_lease(lease, failed=True)
            raise
        self._end_admission_lease(lease, failed=False)

    def _end_admission_lease(self, lease: PlanAdmissionLease, *, failed: bool) -> None:
        """Release the lease; poison the state and seal the ledger if it stopped half-written."""
        gate = self.checkpoint.admission_gate
        # ``closing``: the in-process locks are released even once the state lock was released.
        with gate.hold("ending a typed-plan admission lease", closing=True):
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
            gate.owner, gate.owner_token, gate.owner_thread = None, None, None
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
        if self.state_version not in _ADMISSION_VERSIONS or admission is None:
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
        current = _json(heads(self.memory, admission, self._retry_journals))
        if current != _checkpointed_heads(
            self.checkpoint._entries(), admission, self._retry_journals
        ):
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
        with self.checkpoint.admission_gate.hold("typed-plan COMMIT"):
            admission = self._plan_admission
            if self.state_version not in _ADMISSION_VERSIONS or admission is None:
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
        entries = self.checkpoint._entries()
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
        checkpoint and its audit record (it would see the checkpointed round still open), and no
        durable store write (every one enters the gate first) lands between the checkpoint's
        position read and its line or before the audit record.
        """
        with self.checkpoint.admission_gate.hold("a loop round start or record"):
            self.checkpoint.require_settled("a loop round start or record")
            yield

    @contextmanager
    def review_scope(self) -> Iterator[None]:
        """``ReviewObserver.review_scope``: the admission gate, held across one review write
        (refused once the gate is closed or the state lock released)."""
        with self.checkpoint.admission_gate.hold("a review write"):
            yield

    def before_review_write(self, what: str) -> None:
        """Refuse an enqueue / take while an admission lease is active, after an interrupted one
        or outside the audit's open round (called inside ``review_scope``, before anything is
        written): between rounds only an approval may move the review journal."""
        gate = self.checkpoint.admission_gate
        gate.require_open(what)
        gate.require_round_open(what)

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
        # and its between-rounds line must record no other tail (fail closed, module docs)
        self.checkpoint.require_checkpointed(f"approval of {key}")

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
        with gate.hold("moving the external anchor"):
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
            with self._state.checkpoint.admission_gate.hold(what):
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
            root,
            fingerprint,
            strategies,
            provider,
            provider_for,
            anchor,
            anchored,
            state_version,
            lock,
        )
    except BaseException:
        lock.release()
        raise
    object.__setattr__(state, "lock", lock)
    # Never blocks: the collector may run in any thread (module docs, **Closing**).
    weakref.finalize(state.audit, lock.release_collected)
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
        if state_version == RETRY_STATE_VERSION and set(header.payload) != {
            "state_version",
            "fingerprint",
        }:
            raise _refuse(f"{journal.path} has a v6 header with other fields")
    else:
        state_version = requested_state_version
    if state_version not in {
        LEGACY_STATE_VERSION,
        STATE_VERSION,
        OPERATOR_STATE_VERSION,
        RETRY_STATE_VERSION,
    }:
        raise _refuse(f"unsupported loop state version {state_version!r}")
    # v5 and v6 are explicit opt-ins: never opened as, nor from, another version (no migration).
    if state_version != requested_state_version and (
        OPERATOR_STATE_VERSION in {state_version, requested_state_version}
        or RETRY_STATE_VERSION in {state_version, requested_state_version}
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
    retry: RetryJournals | None = None
    if state_version == RETRY_STATE_VERSION:
        retry = _retry_journal_set(root, expected)
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
                    f"a v{state_version} state directory is missing its required "
                    "plan admission journal"
                )
            admission = _plan_journal(
                admission_path,
                loop_id=loop_id,
                state_version=_PLAN_FORMAT[state_version],
                create=False,
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
        _require_empty(audit, memory, retry=retry)
        if anchored is not None and (
            state_version in _ADMISSION_VERSIONS or anchored.memory_seq > 1
        ):
            raise _behind(root, 0, 0, anchored)
        if state_version in _ADMISSION_VERSIONS:
            loop_id = expected["loop_id"]
            admission = _plan_journal(
                admission_path,
                loop_id=loop_id,
                create=True,
                state_version=_PLAN_FORMAT[state_version],
            )
        journal.append(LOOP_STATE_OPENED, {"state_version": state_version, "fingerprint": expected})
        state = DurableState(
            root,
            memory,
            audit,
            MemoryCheckpoint(journal, memory, admission, state_lock, retry),
            anchor,
            admission,
            state_version,
            state_lock,
            retry,
        )
        _bind_store_gates(state)
        _check_anchor(state, anchored)
        memory.reviews.observe(state)
        return state
    header = entries[0]
    _check_fingerprint(root, header.payload.get("fingerprint"), expected)
    if anchored is not None:
        _check_anchored_files(root, memory, anchored, admission, retry)
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
        elif (
            entry.type == RETRY_ADMISSION
            and retry is not None
            and set(entry.payload) == _RETRY_ADMISSION_KEYS
        ):
            marks.append((f"retry admission checkpoint (memory line {entry.seq})", entry))
        else:
            raise _refuse(
                f"{journal.path}:{entry.seq} is not a recognized v{state_version} checkpoint"
            )
    _check_rounds(audit, checkpoints, allow_open=admission is not None)
    retry_tail: RetryRecovery | None = None
    retry_pending: tuple[RetryManifestItem, ...] = ()
    try:
        _check_between_rounds(audit, marks)
        _check_admission_checkpoints(audit, admission, marks)
        if retry is not None:
            retry_tail, retry_pending = _check_retry_state(
                audit, memory, entries, retry, marks, expected
            )
        _check_positions(
            memory,
            marks,
            admission,
            allow_recovery=admission is not None,
            retry=retry,
            retry_tail=retry_tail,
        )
    except RetryAdmissionError as exc:
        raise _refuse(f"failed-round retry admission state refused: {exc}") from exc
    except (KeyError, LookupError, TypeError) as exc:
        raise _refuse(f"a checkpoint of {journal.path} names unreadable positions: {exc}") from exc
    _check_ledgers(memory, audit.records)
    state = DurableState(
        root,
        memory,
        audit,
        MemoryCheckpoint(journal, memory, admission, state_lock, retry),
        anchor,
        admission,
        state_version,
        state_lock,
        retry,
    )
    _bind_store_gates(state)
    if admission is not None:
        _check_anchor(state, anchored)
    # Every read-only cross-check (round contents and the lifecycle replay included) runs before
    # admission recovery appends anything, and recovery moves the anchor only after the re-checks
    # below. ADR-0073 §4: a reopening that may recover (and always ends refused: an open round is
    # never resumed) verifies the rounds without any Provider; see ``_verify_round_offline``.
    # ADR-0083: so does a reopening that recovers an unfinished failed-round retry.
    recovering = _admission_recovery_path(audit, admission, marks) or retry_tail is not None
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
            _check_positions(memory, marks, admission, retry=retry, retry_tail=retry_tail)
            _check_admission_checkpoints(audit, admission, marks)
        except (LedgerError, PlanAdmissionError) as exc:
            raise _refuse(f"typed-plan admission recovery refused: {exc}") from exc
        except (KeyError, LookupError, TypeError) as exc:
            raise _refuse(
                f"a checkpoint of {journal.path} names unreadable positions: {exc}"
            ) from exc
        _check_anchor(state, anchored)
        if publish:
            state.publish_anchor()
        # Recovery covers pure registration writes only; a started but unrecorded round still
        # follows the durable loop rule (and ADR-0070) and remains stopped for human review.
        if audit.open_round is not None:
            raise _refuse(
                f"round {audit.open_round} was interrupted: it started but was never recorded "
                "after admission recovery; "
                "the loop does not resume or rerun it"
            )
        if recovering and retry_tail is None:
            # Unreachable (an unfinished admission outside the open round is refused above), and
            # the research memory of this path was never restored: never hand it out.
            raise _refuse("a reopening that recovered a typed-plan admission cannot continue")
    if retry is not None and retry_tail is not None:
        _finish_retry_recovery(state, retry, retry_tail, marks, expected, anchored)
    if retry is not None:  # the admitted, not yet run retry trials of the next round (ADR-0083)
        try:
            pending = resolve_manifest(memory.ledger.hypotheses, retry_pending)
        except RetryAdmissionError as exc:
            raise _refuse(f"failed-round retry admission state refused: {exc}") from exc
        memory.retry_reevaluations[:] = [
            (hypothesis, item.attempt)
            for hypothesis, item in zip(pending, retry_pending, strict=True)
        ]
    _check_anchor(state, anchored)
    memory.reviews.observe(state)
    return state


def _bind_store_gates(state: DurableState) -> None:
    """Bind every durable store the checkpoints position — and the audit — to the state's
    admission gate, and the gate to the state lock's release (module docs, **One admission
    gate**, **Closing**); the review queue is bound as the state's observer instead."""
    gate = state.checkpoint.admission_gate
    if gate.state_lock is None:
        raise LoopStateLocked("a durable loop state needs its held state lock")
    gate.state_lock.bind_gate(gate)
    gate.audit = weakref.ref(state.audit)
    state.audit.bind_write_scope(state.checkpoint.audit_write_scope)
    memory = state.memory
    graph, oos = _stores(memory)
    memory.ledger.bind_write_gate(gate)
    oos.bind_write_gate(gate)
    graph.bind_write_gate(gate)
    memory.failures.bind_write_gate(gate)


def _retry_journal_set(root: Path, expected: Any) -> RetryJournals:
    """Open every retry journal of a v6 directory; a malformed one is a state refusal."""
    loop_id = expected.get("loop_id") if isinstance(expected, Mapping) else None
    if not isinstance(loop_id, str) or not loop_id:
        raise _refuse("a v6 loop fingerprint must bind a loop_id")
    try:
        return RetryJournals(root / RETRY_DIR, loop_id=loop_id)
    except RetryAdmissionError as exc:
        raise _refuse(
            f"{root / RETRY_DIR} does not replay as this loop's retry journals: {exc}"
        ) from exc


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
    retry: RetryJournals | None = None,
) -> None:
    """Cross-check 8, first part: every file is at or after its anchored position, with the same
    line there (the review journal at or after the anchored review head; v6: every anchored retry
    journal)."""
    if not anchored.heads:
        return
    try:
        if retry is not None:
            for position in anchored.heads["retry_admission"]:
                known = retry.get(position["failed_record_hash"])
                seq = position["seq"]
                if known is None or seq > len(known.entries):
                    raise _refuse(
                        f"retry journal {position['failed_record_hash'][:12]} in {root} is behind "
                        "its external anchor (deleted, rolled back or truncated)"
                    )
                if (known.entries[seq - 1].hash if seq else GENESIS_HASH) != position["hash"]:
                    raise _refuse(
                        f"retry journal {position['failed_record_hash'][:12]} in {root} has "
                        "diverged from its external anchor"
                    )
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
    entries = state.checkpoint._entries()
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
    *,
    retry: RetryJournals | None = None,
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
    if retry is not None and retry.journals:
        held.append("retry_admission")
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
        if mark.type in {PLAN_ADMISSION, RETRY_ADMISSION}:  # retry: ``_check_retry_state``
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
            raise _refuse(
                "a plan admission checkpoint does not cover its exact ledger / COMMIT heads"
            )
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


def _check_retry_state(
    audit: LoopAuditLog,
    memory: ResearchMemory,
    entries: Sequence[JournalEntry],
    retry: RetryJournals,
    marks: Sequence[tuple[str, JournalEntry]],
    expected: Any,
) -> tuple[RetryRecovery | None, tuple[RetryManifestItem, ...]]:
    """ADR-0083 cross-check of a v6 directory, read-only and before any recovery write.

    Every ``retry_admission`` memory line names one complete journal (PREPARE and COMMIT hashes,
    the COMMIT predicting this very line), sits right after the checkpoint of the failed round it
    retries (only approvals in between), starts at the previous line's TrialLedger position, and
    moves the TrialLedger by exactly the manifest's ``reevaluate`` lines (reducer); its saved
    packet is the packet rebuilt from the audit / memory / ledger prefix it was prepared on, and
    its trials fit the budget (G2). Each admitted retry is run by exactly the next hypothesis stage
    that records ``retry_reevaluations`` (and by no other). At most one journal lacks its line:
    the recoverable tail, which must belong to the final failed record with no open round.

    Returns the tail's reducer view (``None``: nothing to recover) and the admitted trials not run
    yet (empty unless the last retry still waits for its round).
    """
    records = audit.records
    loop_id = expected["loop_id"]
    budget = expected.get("budget")
    ledger = _durable(memory.ledger.journal_snapshot()).entries
    checkpointed: set[str] = set()
    pending: tuple[RetryManifestItem, ...] = ()
    rounds = 0
    previous_ledger: Any = {"seq": 0, "hash": GENESIS_HASH}
    for label, mark in marks:
        if mark.type == ROUND_MEMORY:
            record = records[rounds]
            rounds += 1
            summary = _summary(record, "hypothesis")
            rows = None if summary is None else summary.get("retry_reevaluations")
            if rows is not None:
                if not pending or rows != _retry_rows(memory, pending):
                    raise _refuse(
                        f"audit round {record.round_index} ran retry trials that are not the "
                        "admitted retry waiting for it"
                    )
                pending = ()
        elif mark.type == RETRY_ADMISSION:
            payload = mark.payload
            failed = payload["failed_record_hash"]
            journal = retry.get(failed) if isinstance(failed, str) else None
            if (
                journal is None
                or journal.prepare is None
                or journal.commit is None
                or failed in checkpointed
            ):
                raise _refuse(f"{label} names no unique committed retry journal")
            prepare, commit = journal.prepare, journal.commit
            if (
                payload["retry_id"],
                payload["prepare_seq"],
                payload["prepare_hash"],
                payload["commit_seq"],
                payload["commit_hash"],
            ) != (
                prepare.payload["retry_id"],
                prepare.seq,
                prepare.hash,
                commit.seq,
                commit.hash,
            ):
                raise _refuse(f"{label} does not identify its journal's exact PREPARE and COMMIT")
            if commit.payload["memory_checkpoint_seq"] != mark.seq:
                raise _refuse(f"{label} is not the memory line its COMMIT predicted")
            if pending:
                raise _refuse(f"{label} admits a retry while an earlier one has not run")
            if rounds == 0 or records[rounds - 1].record_hash != failed:
                raise _refuse(f"{label} does not follow the failed round it retries")
            if previous_ledger != _retry_baseline(journal):
                raise _refuse(f"{label}: its PREPARE does not start at the previous ledger line")
            recovery = reduce_retry_ledger_tail(
                journal, ledger, loop_id=loop_id, allow_later_entries=True
            )
            if payload["heads"]["trial_ledger"] != {
                "seq": commit.payload["ledger_head_seq"],
                "hash": commit.payload["ledger_head_hash"],
            }:
                raise _refuse(f"{label} does not cover its COMMIT's TrialLedger head")
            _check_retry_request(journal, records[:rounds], entries[: mark.seq - 1], ledger, budget)
            checkpointed.add(failed)
            pending = recovery.items
        previous_ledger = _json(mark.payload["heads"]["trial_ledger"])
    tails = [item for item in retry.journals if item.failed_record_hash not in checkpointed]
    if not tails:
        return None, pending
    if len(tails) != 1:
        raise _refuse(f"{len(tails)} retry journals lack their checkpoint (at most one may)")
    journal = tails[0]
    if (
        audit.open_round is not None
        or pending
        or _failed_experiment_round(audit) is None
        or records[-1].record_hash != journal.failed_record_hash
    ):
        raise _refuse(
            "an unfinished retry admission is recoverable only as the tail of a directory whose "
            "final record is the failed round it retries (no open round, no other pending retry)"
        )
    if previous_ledger != _retry_baseline(journal):
        raise _refuse("the unfinished retry does not start at the last checkpointed ledger line")
    tail = reduce_retry_ledger_tail(journal, ledger, loop_id=loop_id)
    if (
        journal.commit is not None
        and journal.commit.payload["memory_checkpoint_seq"] != len(entries) + 1
    ):
        raise _refuse("the unfinished retry's COMMIT predicts another memory line")
    _check_retry_request(journal, records, entries, ledger, budget)
    resolve_manifest(memory.ledger.hypotheses, tail.items)  # G1 again, before any recovery write
    return tail, ()


def _retry_baseline(journal: RetryJournal) -> dict[str, Any]:
    prepare = _durable(journal.prepare).payload
    return {"seq": prepare["ledger_baseline_seq"], "hash": prepare["ledger_baseline_hash"]}


def _retry_rows(memory: ResearchMemory, items: Sequence[RetryManifestItem]) -> list[dict[str, str]]:
    """The ``retry_reevaluations`` rows the hypothesis stage records for ``items``."""
    hypotheses = resolve_manifest(memory.ledger.hypotheses, items)
    return retry_summary_rows(zip(hypotheses, (item.attempt for item in items), strict=True))


def _check_retry_request(
    journal: RetryJournal,
    records: Sequence[LoopRecord],
    memory_entries: Sequence[JournalEntry],
    ledger: Sequence[JournalEntry],
    budget: Any,
) -> None:
    """A retry PREPARE's saved ADR-0071 packet is exactly the packet of ``records`` (ending in the
    failed round), the memory lines before the retry and the TrialLedger up to its baseline; and
    its declared trials fit the bound budget as it stood then (G2)."""
    from research.loop.recovery_review import FailedRoundReviewRefused, review_packet_payload

    prepare = _durable(journal.prepare).payload
    try:
        packet = review_packet_payload(
            records, memory_entries, ledger[: prepare["ledger_baseline_seq"]]
        )
    except FailedRoundReviewRefused as exc:
        raise _refuse(f"a retry's review packet cannot be rebuilt: {exc}") from exc
    if (
        packet is None
        or content_hash(packet) != prepare["packet_hash"]
        or _json(packet) != prepare["packet"]
    ):
        raise _refuse("a retry PREPARE's packet is not the failed round's ADR-0071 packet")
    check_retry_budget(
        budget,
        total_trials_spent=records[-1].total_usage.trials,
        requested=len(prepare["manifest"]),
    )


def _finish_retry_recovery(
    state: DurableState,
    retry: RetryJournals,
    tail: RetryRecovery,
    marks: list[tuple[str, JournalEntry]],
    expected: Any,
    anchored: StateHead | None,
) -> None:
    """ADR-0083 exact recovery of the unfinished retry ``tail``, after every read-only check.

    Appends only the reducer's missing ``reevaluate`` suffix, then the COMMIT if absent, then the
    ``retry_admission`` checkpoint — with the gate's retry scope held, no stage and no Provider —,
    re-checks the whole directory, moves the anchor and always ends refused: the caller reopens.
    """
    journal = retry.get(tail.failed_record_hash)
    ledger = state.memory.ledger
    try:
        if journal is None or journal.prepare is None:
            raise RetryAdmissionError("the unfinished retry journal vanished")
        with state.checkpoint.admission_gate.retry_scope():
            hypotheses = resolve_manifest(ledger.hypotheses, tail.missing_items)
            for hypothesis, item in zip(hypotheses, tail.missing_items, strict=True):
                if not ledger.register_reevaluation(hypothesis, item.attempt):
                    raise RetryAdmissionError(f"retry attempt {item.attempt!r} is registered")
            snapshot = _durable(ledger.journal_snapshot())
            done = reduce_retry_ledger_tail(journal, snapshot.entries, loop_id=expected["loop_id"])
            if done.missing_items:
                raise RetryAdmissionError("the TrialLedger still misses a retry re-evaluation")
            if journal.commit is None:
                journal.append_commit(
                    retry_commit_payload(
                        journal.prepare,
                        done.existing_entries,
                        memory_checkpoint_seq=len(state.checkpoint._entries()) + 1,
                    )
                )
            line = state.checkpoint.retry_admission(journal)
        marks.append((f"recovered retry admission checkpoint (memory line {line.seq})", line))
        remaining, _ = _check_retry_state(
            state.audit, state.memory, state.checkpoint._entries(), retry, marks, expected
        )
        if remaining is not None:
            raise RetryAdmissionError("a retry is still unfinished after its recovery")
        _check_positions(state.memory, marks, state._plan_admission, retry=retry)
    except (RetryAdmissionError, LedgerError) as exc:
        raise _refuse(f"failed-round retry recovery refused: {exc}") from exc
    except (KeyError, LookupError, TypeError) as exc:
        raise _refuse(f"failed-round retry recovery found unreadable positions: {exc}") from exc
    _check_anchor(state, anchored)
    state.publish_anchor()
    raise _refuse(
        "the unfinished failed-round retry admission was recovered exactly (ADR-0083: no stage or "
        "Provider ran); reopen the state directory to continue"
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
        line = state.checkpoint._entries()[-1]
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
    retry: RetryJournals | None = None,
    retry_tail: RetryRecovery | None = None,
) -> None:
    """Cross-check 4: every file is exactly where the checkpoints say it was.

    v6 (``retry``): a ``retry_admission`` line moves only the TrialLedger (and the retry journals,
    ``_check_retry_positions``); ``retry_tail``: the one unfinished retry the opener may recover,
    whose already written ``reevaluate`` lines are the only TrialLedger tail accepted for it.
    """
    pending = None if admission is None else admission.pending
    committed_ids = (
        set()
        if admission is None
        else {item.prepare.transaction_id for item in admission.committed}
    )
    checkpointed_ids = {
        mark.payload["transaction_id"] for _, mark in marks if mark.type == PLAN_ADMISSION
    }
    uncheckpointed_commits = committed_ids - checkpointed_ids
    expected_head_keys = set(_journals(memory, admission)) | {"failures"}
    if retry is not None:
        expected_head_keys.add("retry_admission")
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
            elif mark.type == RETRY_ADMISSION and name != "trial_ledger" and seq != previous:
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
            (
                name == "plan_admission"
                and admission is not None
                and len(uncheckpointed_commits) + (pending is not None) == 1
                and len(extra) == (2 if uncheckpointed_commits else 1)
            )
            or (
                name == "trial_ledger"
                and (pending is not None or len(uncheckpointed_commits) == 1)
                and len(extra) <= 1
            )
        )
        if name == "trial_ledger" and retry_tail is not None:
            # ADR-0083: exactly the reducer's verified re-evaluations, from the PREPARE baseline
            baseline = retry_tail.prepare.payload
            if (baseline["ledger_baseline_seq"], baseline["ledger_baseline_hash"]) != (
                previous,
                entries[previous - 1].hash if previous else GENESIS_HASH,
            ):
                raise _refuse("the unfinished retry does not start at the last checkpointed ledger")
            if [entry.hash for entry in extra] != [
                entry.hash for entry in retry_tail.existing_entries
            ]:
                raise _refuse("the TrialLedger tail is not the unfinished retry's re-evaluations")
            permitted = True
        if extra and not permitted:
            raise _refuse(
                f"{name} holds {len(extra)} line(s) no checkpoint accounts for: the audit or the "
                "memory checkpoint is behind it (truncated), or a round was interrupted"
            )
        if (
            name == "trial_ledger"
            and admission is not None
            and (pending is not None or uncheckpointed_commits)
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
                raise _refuse(
                    "uncheckpointed admission does not start at the last checkpointed ledger head"
                )
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
        if mark.type == RETRY_ADMISSION and count != previous:
            raise _refuse(f"{label} moves failures: a retry moves only its ledger and journal")
        previous = count
    if len(hashes) != previous:
        raise _refuse(
            f"failures holds {len(hashes) - previous} record(s) no recorded round accounts for"
        )
    if retry is not None:
        _check_retry_positions(retry, marks, retry_tail)


def _check_retry_positions(
    retry: RetryJournals,
    marks: Sequence[tuple[str, JournalEntry]],
    tail: RetryRecovery | None,
) -> None:
    """Cross-check 4 for v6 retry journals: every checkpoint's ``retry_admission`` list is the
    previous one, except a ``retry_admission`` line adds exactly its own complete journal; the
    last list is every non-empty journal on disk, but for the one unfinished ``tail``."""
    previous: list[Any] = []
    for label, mark in marks:
        position = mark.payload["heads"]["retry_admission"]
        if (
            not isinstance(position, list)
            or any(
                not isinstance(item, Mapping) or set(item) != {"failed_record_hash", "seq", "hash"}
                for item in position
            )
            or [item["failed_record_hash"] for item in position]
            != sorted({str(item["failed_record_hash"]) for item in position})
        ):
            raise _refuse(f"{label} has an unreadable retry journal position list")
        if mark.type == RETRY_ADMISSION:
            failed = mark.payload["failed_record_hash"]
            journal = retry.get(failed)
            if (
                journal is None
                or journal.commit is None
                or any(item["failed_record_hash"] == failed for item in previous)
            ):
                raise _refuse(f"{label} names no new committed retry journal")
            expected = sorted(
                [*previous, journal.position()], key=lambda item: str(item["failed_record_hash"])
            )
            if position != _json(expected):
                raise _refuse(f"{label} does not move the retry journals by exactly its own")
        elif position != previous:
            raise _refuse(f"{label} moves a retry journal (only its retry checkpoint may)")
        previous = list(position)
    recorded = {str(item["failed_record_hash"]): item for item in previous}
    unrecorded: list[str] = []
    for journal in retry.journals:
        known = recorded.pop(journal.failed_record_hash, None)
        if known is None:
            unrecorded.append(journal.failed_record_hash)
        elif known != journal.position():
            raise _refuse(f"retry journal {journal.failed_record_hash[:12]} moved past its line")
    if recorded:
        raise _refuse(f"checkpointed retry journal(s) {sorted(recorded)} are missing or emptied")
    expected_tail = [] if tail is None else [tail.failed_record_hash]
    if unrecorded != expected_tail:
        raise _refuse(
            f"retry journal(s) {unrecorded} have no checkpoint and are not the unique recoverable "
            "tail of this directory"
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
            for row in stage.get("retry_reevaluations", ()):  # ADR-0083 retry rounds only
                retried = registered(row["hypothesis"], row["attempt"], index)
                if retried.content_hash() != row["hypothesis_hash"]:
                    raise _refuse(f"audit round {index} retried another {row['hypothesis']}")
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
