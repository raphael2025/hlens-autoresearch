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
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from core.contracts.synthetic import SyntheticMarket, SyntheticMarketSpec
from core.domain.research import Hypothesis, LlmCall
from core.domain.specs import StrategySpec
from research.evolution import LineageGraph
from research.hypotheses import HypothesisDraft, TrialLedger
from research.loop.segment import ResearchPiece
from research.persistence import AppendOnlyJournal, JournalCorrupted
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


class ReviewQueue:
    """LLM drafts awaiting human review, keyed by ``name@version`` (first draft wins).

    ``path=None`` (the default): in memory only. With a ``path`` every enqueue, approval and take
    is first appended to a hash-chained journal (see module docs) and reopening replays it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._drafts: dict[str, HypothesisDraft] = {}
        self._reviewers: dict[str, str] = {}
        self._approvals: list[ReviewApproval] = []
        self._taken: set[str] = set()
        self._automation: set[str] = set()
        self._journal: AppendOnlyJournal | None = None
        if path is not None:
            journal = AppendOnlyJournal(path)
            for entry in journal.entries:
                self._replay(f"{journal.path}:{entry.seq}", entry.type, entry.payload)
            self._journal = journal  # set after replay: replaying never re-appends

    @property
    def journal(self) -> AppendOnlyJournal | None:
        """The backing journal (``None``: in memory); read-only use, for cross-file checks."""
        return self._journal

    @staticmethod
    def _key(draft: HypothesisDraft) -> str:
        return f"{draft.hypothesis.name}@{draft.hypothesis.version}"

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
        key = self._key(draft)
        if key in self._drafts:
            return False
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
        draft = self._drafts[key]
        if draft.reviewed:
            raise ValueError(f"{key} was already approved by {self._reviewers[key]!r}")
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
        return self._admit(approval)

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
        key = self._key(draft)
        if key in self._taken:
            return
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
