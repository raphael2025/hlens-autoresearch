"""Research memory shared by the loop's stages across rounds (Phase 11; ADR-0049).

- ``TrialLedger`` (Phase 7): pre-registration and trial counting; nothing is removed. Every trial
  the loop runs (knowledge hypothesis, reviewed LLM draft, evolution offspring) is a registered
  hypothesis **before** its experiment runs.
- ``ReviewQueue``: LLM hypothesis drafts wait here for a **human** review (ADR-0040, 09-security.md
  §3). The loop only enqueues and later registers drafts a human approved; it never approves, and a
  reviewer identity that is an automation identity (the loop's own actor) is refused.
- ``FailureRegistry`` (Phase 5): append-only record of REJECTED / FAILED subjects (H6).
- ``strategies``: the strategy catalog trials resolve against (library candidates + offspring).
- ``trials`` / ``validations``: every experiment and validation outcome, failures included, in
  round order (append-only); ``experiments`` / ``states`` are their JSON summaries.
- ``lineage``: every strategy spec the loop evolved from or into (for ``LineageGraph``).
- ``oos_ledger``: the sealed-OOS unsealing ledger (one unsealing per family, append-only).
- ``markets`` / ``research_data``: every market the ingest stage generated (``market_specs``: the
  spec each was generated from) and the research-window bars each contributed (``ResearchPiece``,
  oldest first): the accumulated research data every round evaluates on (ADR-0049
  accumulated-window note). Sealed-window bars are never in it.

Durable memory (ADR-0049 implementation note, durable composition, 2026-09-26): with a state
directory (``research.loop.durable``) the ledger, the review queue, the unsealing ledger and the
lineage are journal-backed (``research.persistence.AppendOnlyJournal``: hash-chained, append-only,
fsync'd), the failure registry is its own append-only file, and everything else here is restored
from the per-round memory checkpoint. ``ReviewQueue(path)`` journals every enqueue, approval and
take; reopening replays and re-verifies them (an approval by an automation identity, or whose
draft / call hash does not match the enqueued draft, is corruption — refused).

Approvals between rounds (ADR-0049 implementation note, approvals between rounds, 2026-09-26): a
durable state directory binds a ``ReviewObserver`` to the queue (``ReviewQueue.observe``). Every
``approve`` then asks it first (``before_approval``: refused while a round is running) and tells it
once the approval is journaled (``after_approval``: the state directory writes a between-rounds
checkpoint line and moves its external anchor), so no human approval exists that no checkpoint
names.

Review writes are serialized with the state (ADR-0073 admission lease review, 2026-09-28). With an
observer, every journaled review write runs inside ``observer.review_scope()``: an approval holds it
across ``before_approval`` → journal line → in-memory admission → ``after_approval``, and an
enqueue or take across ``before_review_write`` → journal line → in-memory update. A durable state
holds its admission gate there, so no typed-plan admission lease, round checkpoint or anchor move
can start between an approval's journal line and its between-rounds checkpoint, and an enqueue /
take is refused (before anything is written) while an admission lease is active, after an
interrupted one, outside the loop's open round (between rounds only an approval may move the review
journal) or once the state is closed. The backing journal is never handed out (its ``append``
would bypass the observer's scope and the replayed queue): cross-file checks read ``durable``,
``journal_head()`` or ``journal_snapshot()`` (``research.persistence.JournalSnapshot``: detached,
read-only).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from core.contracts.synthetic import SyntheticMarket, SyntheticMarketSpec
from core.domain.research import Hypothesis, LlmCall
from core.domain.specs import StrategySpec
from research.evolution import LineageGraph
from research.hypotheses import HypothesisDraft, TrialLedger
from research.loop.segment import ResearchPiece
from research.persistence import (
    GENESIS_HASH,
    AppendOnlyJournal,
    JournalCorrupted,
    JournalSnapshot,
    journal_snapshot,
)
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import StrategyCandidate
from research.validation.sealed_oos import InMemoryUnsealingLedger, UnsealingLedger

if TYPE_CHECKING:
    from research.loop.trials import TrialOutcome, ValidationOutcome

__all__ = [
    "AUTOMATION_ACTOR_PREFIX",
    "REVIEW_APPROVED",
    "REVIEW_ENQUEUED",
    "REVIEW_TAKEN",
    "ResearchMemory",
    "ReviewApproval",
    "ReviewObserver",
    "ReviewQueue",
]

#: Identities in this namespace are automation (the loop's ``LifecycleGuard`` actor), never humans.
AUTOMATION_ACTOR_PREFIX: Final = "research_loop:"

#: Review journal line types (``ReviewQueue(path)``).
REVIEW_ENQUEUED: Final = "review_enqueued"
REVIEW_APPROVED: Final = "review_approved"
REVIEW_TAKEN: Final = "review_taken"
_APPROVAL_KEYS: Final = frozenset({"key", "reviewer", "draft_hash", "call_hash"})


@dataclass(frozen=True, slots=True)
class ReviewApproval:
    """One human approval of an LLM draft (append-only record)."""

    key: str
    reviewer: str
    draft_hash: str
    call_hash: str


class ReviewObserver(Protocol):
    """Told about every human approval of a queue it observes (a durable state directory), and
    asked before every other journaled review write (module docs)."""

    def review_scope(self) -> AbstractContextManager[object]:
        """Held across one whole review write (module docs); entered before any check of it."""
        ...

    def before_review_write(self, what: str) -> None:
        """Called before an enqueue / take is journaled; raising refuses it (nothing is written)."""
        ...

    def before_approval(self, key: str) -> None:
        """Called before the approval is journaled; raising refuses it (nothing is written)."""
        ...

    def after_approval(self, approval: ReviewApproval) -> None:
        """Called once the approval is journaled and admitted (checkpoint it, anchor it)."""
        ...


class ReviewQueue:
    """LLM drafts awaiting human review, keyed by ``name@version`` (first draft wins).

    ``path=None`` (the default): in memory only. With a ``path`` every enqueue, approval and take
    is first appended to a hash-chained journal (see module docs) and reopening replays it.
    ``observe`` binds the one ``ReviewObserver`` told about every later approval.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._drafts: dict[str, HypothesisDraft] = {}
        self._reviewers: dict[str, str] = {}
        self._approvals: list[ReviewApproval] = []
        self._taken: set[str] = set()
        self._automation: set[str] = set()
        self._journal: AppendOnlyJournal | None = None
        self._observer: ReviewObserver | None = None
        if path is not None:
            journal = AppendOnlyJournal(path)
            for entry in journal.entries:
                self._replay(f"{journal.path}:{entry.seq}", entry.type, entry.payload)
            self._journal = journal  # set after replay: replaying never re-appends

    @property
    def durable(self) -> bool:
        """Whether a journal backs this queue."""
        return self._journal is not None

    def journal_head(self) -> tuple[int, str] | None:
        """``(entry count, chain head)`` of the backing journal; ``None``: in memory."""
        journal = self._journal
        if journal is None:
            return None
        entries = journal.entries  # one consistent tuple: count and head from the same instant
        return len(entries), (entries[-1].hash if entries else GENESIS_HASH)

    def journal_snapshot(self) -> JournalSnapshot | None:
        """Detached read-only copy of the backing journal's verified entries; ``None``: in memory.

        Never the writable journal (module docs): a review write goes through ``enqueue`` /
        ``approve`` / ``mark_taken`` and so through the observer's ``review_scope``.
        """
        return None if self._journal is None else journal_snapshot(self._journal)

    @staticmethod
    def _key(draft: HypothesisDraft) -> str:
        return f"{draft.hypothesis.name}@{draft.hypothesis.version}"

    def observe(self, observer: ReviewObserver) -> None:
        """Bind the observer of every later approval (once; a second observer is refused)."""
        if self._observer is not None:
            raise ValueError("the review queue already has an observer")
        self._observer = observer

    def bind_loop_actor(self, actor: str) -> None:
        """Declare an automation identity (the loop's actor) that may never approve a draft."""
        identity = actor.strip()
        if not identity:
            raise ValueError("the loop actor must be non-empty")
        if any(approval.reviewer == identity for approval in self._approvals):
            raise ValueError(f"{identity!r} already approved a draft, yet it is automation")
        self._automation.add(identity)

    def enqueue(self, draft: HypothesisDraft) -> bool:
        if draft.reviewed:
            raise ValueError("the loop only enqueues unreviewed drafts")
        if self._observer is None:
            return self._enqueue(draft)
        with self._observer.review_scope():
            return self._enqueue(draft)

    def _enqueue(self, draft: HypothesisDraft) -> bool:
        key = self._key(draft)
        if key in self._drafts:
            return False
        if self._observer is not None:
            self._observer.before_review_write(f"enqueueing {key} for review")
        if self._journal is not None:
            self._journal.append(
                REVIEW_ENQUEUED,
                {
                    "hypothesis": draft.hypothesis.model_dump(mode="json"),
                    "call": draft.call.model_dump(mode="json"),
                },
            )
        self._drafts[key] = draft
        return True

    def approve(self, key: str, *, reviewer: str) -> HypothesisDraft:
        """A human approves a pending draft (called outside the loop, never by a stage).

        ``reviewer`` must be a non-empty identity that is not the loop's own actor (nor any
        ``research_loop:`` automation identity); it is recorded with the draft and call hashes.
        """
        identity = self._human(reviewer)
        observer = self._observer
        if observer is None:
            return self._approve(key, identity)
        # One scope from the checks to the checkpoint (module docs): nothing the observer
        # coordinates can interleave between the approval line and its checkpoint.
        with observer.review_scope():
            return self._approve(key, identity)

    def _approve(self, key: str, identity: str) -> HypothesisDraft:
        """``approve`` (inside the observer's ``review_scope`` when there is one)."""
        draft = self._drafts[key]
        if draft.reviewed:
            raise ValueError(f"{key} was already approved by {self._reviewers[key]!r}")
        if self._observer is not None:
            self._observer.before_approval(key)
        approval = ReviewApproval(
            key=key,
            reviewer=identity,
            draft_hash=draft.hypothesis.content_hash(),
            call_hash=draft.call.content_hash(),
        )
        if self._journal is not None:
            self._journal.append(
                REVIEW_APPROVED,
                {
                    "key": key,
                    "reviewer": identity,
                    "draft_hash": approval.draft_hash,
                    "call_hash": approval.call_hash,
                },
            )
        reviewed = self._admit(approval)
        if self._observer is not None:
            self._observer.after_approval(approval)
        return reviewed

    def _human(self, reviewer: object) -> str:
        identity = reviewer.strip() if isinstance(reviewer, str) else ""
        if not identity:
            raise ValueError("a review needs the reviewer's identity")
        if identity in self._automation or identity.startswith(AUTOMATION_ACTOR_PREFIX):
            raise ValueError(f"{identity!r} is an automation identity; only a human may approve")
        return identity

    def _admit(self, approval: ReviewApproval) -> HypothesisDraft:
        reviewed = replace(self._drafts[approval.key], reviewed=True)
        self._drafts[approval.key] = reviewed
        self._reviewers[approval.key] = approval.reviewer
        self._approvals.append(approval)
        return reviewed

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(key for key, draft in self._drafts.items() if not draft.reviewed)

    @property
    def approvals(self) -> tuple[ReviewApproval, ...]:
        return tuple(self._approvals)

    @property
    def taken(self) -> frozenset[str]:
        """Keys of approved drafts the loop has already registered."""
        return frozenset(self._taken)

    def reviewed_untaken(self) -> tuple[HypothesisDraft, ...]:
        return tuple(
            draft
            for key, draft in self._drafts.items()
            if draft.reviewed and key not in self._taken
        )

    def mark_taken(self, draft: HypothesisDraft) -> None:
        if self._observer is None:
            self._mark_taken(draft)
            return
        with self._observer.review_scope():
            self._mark_taken(draft)

    def _mark_taken(self, draft: HypothesisDraft) -> None:
        key = self._key(draft)
        if key in self._taken:
            return
        if self._observer is not None:
            self._observer.before_review_write(f"marking {key} taken")
        if self._journal is not None:
            self._journal.append(REVIEW_TAKEN, {"key": key})
        self._taken.add(key)

    def reviewer_of(self, draft: HypothesisDraft) -> str | None:
        return self._reviewers.get(self._key(draft))

    # -- replay ---------------------------------------------------------------------------------

    def _replay(self, where: str, kind: str, payload: Any) -> None:
        """Re-apply one verified journal line; any inconsistency is corruption (fail closed)."""
        try:
            if kind == REVIEW_ENQUEUED:
                draft = HypothesisDraft(
                    hypothesis=Hypothesis.model_validate(payload["hypothesis"]),
                    call=LlmCall.model_validate(payload["call"]),
                )
                if self._key(draft) in self._drafts:
                    raise ValueError("the draft was enqueued twice")
                self._drafts[self._key(draft)] = draft
            elif kind == REVIEW_APPROVED:
                if set(payload) != _APPROVAL_KEYS:
                    raise ValueError("an approval line has other fields")
                key = str(payload["key"])
                draft = self._drafts[key]
                if draft.reviewed:
                    raise ValueError(f"{key} was approved twice")
                identity = self._human(payload["reviewer"])
                if identity != payload["reviewer"]:
                    raise ValueError("the reviewer identity is not normalized")
                approval = ReviewApproval(
                    key=key,
                    reviewer=identity,
                    draft_hash=str(payload["draft_hash"]),
                    call_hash=str(payload["call_hash"]),
                )
                if (approval.draft_hash, approval.call_hash) != (
                    draft.hypothesis.content_hash(),
                    draft.call.content_hash(),
                ):
                    raise ValueError(f"the approval of {key} names another draft or call")
                self._admit(approval)
            elif kind == REVIEW_TAKEN:
                key = str(payload["key"])
                if not self._drafts[key].reviewed or key in self._taken:
                    raise ValueError(f"{key} was taken before its approval, or twice")
                self._taken.add(key)
            else:
                raise ValueError(f"unknown record type {kind!r}")
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalCorrupted(f"{where} is not a valid review line: {exc}") from exc


@dataclass
class ResearchMemory:
    failures: FailureRegistry
    ledger: TrialLedger = field(default_factory=TrialLedger)
    reviews: ReviewQueue = field(default_factory=ReviewQueue)
    experiments: list[dict[str, Any]] = field(default_factory=list)
    states: list[dict[str, Any]] = field(default_factory=list)
    strategies: dict[str, StrategyCandidate] = field(default_factory=dict)
    trials: list[TrialOutcome] = field(default_factory=list)
    validations: list[ValidationOutcome] = field(default_factory=list)
    lineage: list[StrategySpec] = field(default_factory=list)
    offspring: list[dict[str, Any]] = field(default_factory=list)
    oos_ledger: UnsealingLedger = field(default_factory=InMemoryUnsealingLedger)
    markets: list[SyntheticMarket] = field(default_factory=list)
    market_specs: list[SyntheticMarketSpec] = field(default_factory=list)
    research_data: list[ResearchPiece] = field(default_factory=list)
    #: v6 retry attempts pre-registered between rounds and consumed by the next hypothesis stage.
    retry_attempts: dict[tuple[str, str], str] = field(default_factory=dict)
    #: The durable lineage journal (``None``: in memory only); ``add_lineage`` feeds both.
    lineage_graph: LineageGraph | None = None

    def add_strategy(self, candidate: StrategyCandidate) -> None:
        """Add a candidate to the catalog; the same ref with other content is refused."""
        key = str(candidate.spec.ref)
        known = self.strategies.get(key)
        if known is not None and known.spec.content_hash() != candidate.spec.content_hash():
            raise ValueError(f"{key} is already in the catalog with other content")
        self.strategies[key] = candidate

    def add_market(self, spec: SyntheticMarketSpec, market: SyntheticMarket) -> None:
        """Record an ingested market and the spec it was generated from (a durable restore
        regenerates the market from the spec and requires the same ``market_hash``)."""
        self.market_specs.append(spec)
        self.markets.append(market)

    def add_lineage(self, spec: StrategySpec) -> None:
        """Record a spec the loop evolved from or into (once per ref; durable when journaled)."""
        if any(known.ref == spec.ref for known in self.lineage):
            return
        if self.lineage_graph is not None:
            self.lineage_graph.add(spec)
        self.lineage.append(spec)
