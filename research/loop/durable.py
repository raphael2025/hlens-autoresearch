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
   (the re-evaluation under its attempt); every offspring's child and parent spec is in the lineage
   with the recorded spec hash; every ``human_review:<who>`` evidence has that human's approval of
   that draft in the review queue, taken; every unsealed / consumed sealed-OOS report has the
   family's unsealing by the same approver, marked evaluated; every failure record hash the audit
   lists is in the failure registry;
7. after the loop replayed the audit into its guard: every lifecycle subject is a registered
   hypothesis;
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

The LLM provider is external: its own state (e.g. a scripted provider's position) is not loop
state and is the caller's to resume.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

from apps.worker.loop import LifecycleGuard, LoopAuditLog, LoopRecord
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
from core.errors import ReasonCode
from research.evolution import LineageGraph
from research.hypotheses import TrialLedger
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
    "REVIEWS_FILE",
    "ROUND_MEMORY",
    "SEALED_OOS_FILE",
    "STATE_VERSION",
    "DurableState",
    "FileAnchor",
    "LoopStateInconsistent",
    "MemoryCheckpoint",
    "StateAnchor",
    "StateHead",
    "open_state",
]

AUDIT_FILE: Final = "audit.jsonl"
MEMORY_FILE: Final = "memory.jsonl"
LEDGER_FILE: Final = "trial_ledger.jsonl"
SEALED_OOS_FILE: Final = "sealed_oos.jsonl"
LINEAGE_FILE: Final = "lineage.jsonl"
REVIEWS_FILE: Final = "reviews.jsonl"
FAILURES_FILE: Final = "failures.jsonl"

#: Memory journal line types.
LOOP_STATE_OPENED: Final = "loop_state_opened"
ROUND_MEMORY: Final = "round_memory"
BETWEEN_ROUNDS: Final = "between_rounds"
#: Anchor journal line type (``FileAnchor``).
ANCHOR_HEAD: Final = "loop_state_head"
#: Version of the memory journal's payload layout (2: the fingerprint binds the budgets and the
#: exact cadence; a version-1 directory is refused, its budgets were never bound. 3: every human
#: approval between rounds has a ``between_rounds`` checkpoint; a version-2 directory is refused,
#: its between-round approvals were never checkpointed).
STATE_VERSION: Final = 3

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


# ------------------------------------------------------------------------------------ positions


def _journals(memory: ResearchMemory) -> dict[str, AppendOnlyJournal]:
    ledger, reviews = memory.ledger.journal, memory.reviews.journal
    graph, oos = memory.lineage_graph, memory.oos_ledger
    if (
        ledger is None
        or reviews is None
        or graph is None
        or graph.journal is None
        or not isinstance(oos, DurableUnsealingLedger)
    ):
        raise ValueError("a durable loop state needs journal-backed memory throughout")
    return {
        "trial_ledger": ledger,
        "sealed_oos": oos.journal,
        "lineage": graph.journal,
        "reviews": reviews,
    }


def _failure_hashes(records: Sequence[FailureRecord]) -> list[str]:
    return [record.content_hash() for record in records]


def heads(memory: ResearchMemory) -> dict[str, Any]:
    """The position of every file of the state directory but the audit and the memory journal."""
    out: dict[str, Any] = {
        name: {"seq": len(journal.entries), "hash": journal.head_hash}
        for name, journal in _journals(memory).items()
    }
    hashes = _failure_hashes(memory.failures.records())
    out["failures"] = {"count": len(hashes), "digest": content_hash(hashes)}
    return out


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
    return sum(1 for entry in _journals(memory)["reviews"].entries if entry.type == REVIEW_APPROVED)


class MemoryCheckpoint:
    """``ResearchLoop(checkpoint=...)``: one ``round_memory`` line per finished round; plus one
    ``between_rounds`` line per human approval between rounds (``between_rounds``)."""

    def __init__(self, journal: AppendOnlyJournal, memory: ResearchMemory) -> None:
        self._journal = journal
        self._memory = memory
        self._marks = _Marks.of(memory)
        self._covered = _approval_lines(memory)  # opening verified every one is checkpointed

    def __call__(self, record: LoopRecord) -> None:
        memory = self._memory
        if _approval_lines(memory) != self._covered:
            raise LoopStateInconsistent(
                f"round {record.round_index}: the review journal holds a human approval no "
                "between-rounds checkpoint names (its checkpoint failed, or it was written past "
                "the review queue); the round is not recorded"
            )
        self._journal.append(
            ROUND_MEMORY,
            {
                "round_index": record.round_index,
                "record_hash": record.record_hash,
                "heads": heads(memory),
                "delta": _delta(memory, self._marks),
            },
        )
        self._marks = _Marks.of(memory)

    def between_rounds(self, rounds: int, audit_head: str | None, approval: ReviewApproval) -> None:
        """Checkpoint the human approval just journaled (the review journal's last line)."""
        line = _journals(self._memory)["reviews"].entries[-1]
        if line.type != REVIEW_APPROVED or line.payload.get("key") != approval.key:
            raise LoopStateInconsistent("the review journal's last line is not this approval")
        self._journal.append(
            BETWEEN_ROUNDS,
            {
                "rounds": rounds,
                "audit_head": audit_head,
                "heads": heads(self._memory),
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

    # -- ReviewObserver: human approvals between rounds (module docs) --------------------------

    def before_approval(self, key: str) -> None:
        """Refuse an approval while a round is running (approvals are made between rounds)."""
        if self.audit.open_round is not None:
            raise ValueError(
                f"round {self.audit.open_round} is running (or was interrupted): {key} can only "
                "be approved between rounds"
            )

    def after_approval(self, approval: ReviewApproval) -> None:
        """Checkpoint the journaled approval and move the anchor up to it, immediately."""
        self.checkpoint.between_rounds(len(self.audit.records), self.audit.head, approval)
        self.publish_anchor()

    def publish_anchor(self, record: LoopRecord | None = None) -> None:
        """Move the anchor up to the current head (no anchor: nothing).

        ``ResearchLoop(after_record=...)`` calls it with every recorded round; the composition
        calls it without a record once the reopened directory passed every cross-check.
        """
        if self.anchor is None:
            return
        if record is not None and record.record_hash != self.audit.head:
            raise _refuse(f"round {record.round_index} is not the audit head: nothing to anchor")
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


def _refuse(message: str) -> LoopStateInconsistent:
    return LoopStateInconsistent(message)


def _line_heads(entry: JournalEntry) -> Any:
    """The file positions a memory journal line names (none for the header)."""
    return {} if entry.type == LOOP_STATE_OPENED else _json(dict(entry.payload["heads"]))


def open_state(
    state_dir: Path,
    *,
    fingerprint: Mapping[str, Any],
    strategies: Sequence[StrategyCandidate],
    provider: SyntheticMarketProvider | None,
    provider_for: Callable[[StrategySpec], Any] | None,
    anchor: StateAnchor | None = None,
) -> DurableState:
    """Open (or create) a loop state directory and restore the research memory from it.

    ``strategies``: the configured library catalog (added before the restored offspring);
    ``provider``: regenerates the ingested markets (``None``: a round data source that keeps no
    ingest memory — the dataset-backed loop re-reads each round's verified manifests — so every
    checkpoint's ``markets`` / ``research_data`` must be empty); ``provider_for``: serves an
    offspring spec
    (the evolution plan's; ``None`` without evolution); ``anchor``: the optional external anchor
    (module docs; a ``FileAnchor`` must lie outside ``state_dir``). Raises
    ``LoopStateInconsistent`` when the files disagree with each other, the configuration or the
    anchor (see module docs), ``JournalCorrupted`` when one file is itself corrupt. The anchor is
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
    anchored = None if anchor is None else anchor.load()
    root.mkdir(parents=True, exist_ok=True)
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
    expected = _json(fingerprint)
    entries = journal.entries
    if not entries:
        _require_empty(audit, memory)
        if anchored is not None and anchored.memory_seq > 1:
            raise _behind(root, 0, 0, anchored)
        journal.append(LOOP_STATE_OPENED, {"state_version": STATE_VERSION, "fingerprint": expected})
        state = DurableState(root, memory, audit, MemoryCheckpoint(journal, memory), anchor)
        _check_anchor(state, anchored)
        memory.reviews.observe(state)
        return state
    header = entries[0]
    if header.type != LOOP_STATE_OPENED:
        raise _refuse(f"{journal.path} does not start with a loop state header")
    if header.payload.get("state_version") != STATE_VERSION:
        raise _refuse(
            f"{journal.path} has state version {header.payload.get('state_version')!r}, this "
            f"code writes {STATE_VERSION}: open it with the code that wrote it, or start a new "
            "state directory"
        )
    _check_fingerprint(root, header.payload.get("fingerprint"), expected)
    if anchored is not None:
        _check_anchored_files(root, memory, anchored)
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
        else:
            raise _refuse(
                f"{journal.path}:{entry.seq} is neither a round nor a between-rounds checkpoint"
            )
    _check_rounds(audit, checkpoints)
    try:
        _check_between_rounds(audit, marks)
        _check_positions(memory, marks)
    except (KeyError, LookupError, TypeError) as exc:
        raise _refuse(f"a checkpoint of {journal.path} names unreadable positions: {exc}") from exc
    for record, checkpoint in zip(audit.records, checkpoints, strict=True):
        _restore_round(memory, record, checkpoint["delta"], provider, provider_for)
    _check_ledgers(memory, audit.records)
    state = DurableState(root, memory, audit, MemoryCheckpoint(journal, memory), anchor)
    _check_anchor(state, anchored)
    memory.reviews.observe(state)
    return state


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


def _check_anchored_files(root: Path, memory: ResearchMemory, anchored: StateHead) -> None:
    """Cross-check 8, first part: every file is at or after its anchored position, with the same
    line there (the review journal at or after the anchored review head)."""
    if not anchored.heads:
        return
    try:
        for name, journal in _journals(memory).items():
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
    """Cross-check 8: the directory is at or after the anchored head, on the same history."""
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


def _require_empty(audit: LoopAuditLog, memory: ResearchMemory) -> None:
    """A state directory without a memory header must hold no state at all."""
    held = [name for name, journal in _journals(memory).items() if journal.entries]
    if audit.records or audit.open_round is not None:
        held.append("audit")
    if memory.failures.records():
        held.append("failures")
    if held:
        raise _refuse(
            f"the memory checkpoint file is missing or empty, but {sorted(held)} hold state: "
            "the directory was tampered with or mixed with another run"
        )


def _check_rounds(audit: LoopAuditLog, checkpoints: Sequence[Mapping[str, Any]]) -> None:
    """Cross-checks 2 and 3."""
    if audit.open_round is not None:
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


def _check_positions(memory: ResearchMemory, marks: Sequence[tuple[str, JournalEntry]]) -> None:
    """Cross-check 4: every file is exactly where the checkpoints say it was."""
    for name, journal in _journals(memory).items():
        entries = journal.entries
        previous = 0
        for label, mark in marks:
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
        if extra:
            raise _refuse(
                f"{name} holds {len(extra)} line(s) no checkpoint accounts for: the audit or the "
                "memory checkpoint is behind it (truncated), or a round was interrupted"
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
    except (ArithmeticError, KeyError, LookupError, TypeError, ValueError) as exc:
        raise _refuse(f"the memory checkpoint of round {index} is inconsistent: {exc}") from exc
    memory.experiments.extend(delta["experiments"])
    memory.states.extend(delta["states"])
    memory.offspring.extend(delta["offspring"])


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
        stage = _summary(record, "validation")
        for row in [] if stage is None else stage["reports"]:
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
