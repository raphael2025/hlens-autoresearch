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
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

from core.domain.specs import StrategySpec
from research.hypotheses import HypothesisDraft, TrialLedger
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import StrategyCandidate
from research.validation.sealed_oos import InMemoryUnsealingLedger

if TYPE_CHECKING:
    from research.loop.trials import TrialOutcome, ValidationOutcome

__all__ = ["AUTOMATION_ACTOR_PREFIX", "ResearchMemory", "ReviewApproval", "ReviewQueue"]

#: Identities in this namespace are automation (the loop's ``LifecycleGuard`` actor), never humans.
AUTOMATION_ACTOR_PREFIX: Final = "research_loop:"


@dataclass(frozen=True, slots=True)
class ReviewApproval:
    """One human approval of an LLM draft (append-only record)."""

    key: str
    reviewer: str
    draft_hash: str
    call_hash: str


class ReviewQueue:
    """LLM drafts awaiting human review, keyed by ``name@version`` (first draft wins)."""

    def __init__(self) -> None:
        self._drafts: dict[str, HypothesisDraft] = {}
        self._reviewers: dict[str, str] = {}
        self._approvals: list[ReviewApproval] = []
        self._taken: set[str] = set()
        self._automation: set[str] = set()

    @staticmethod
    def _key(draft: HypothesisDraft) -> str:
        return f"{draft.hypothesis.name}@{draft.hypothesis.version}"

    def bind_loop_actor(self, actor: str) -> None:
        """Declare an automation identity (the loop's actor) that may never approve a draft."""
        if not actor.strip():
            raise ValueError("the loop actor must be non-empty")
        self._automation.add(actor.strip())

    def enqueue(self, draft: HypothesisDraft) -> bool:
        if draft.reviewed:
            raise ValueError("the loop only enqueues unreviewed drafts")
        key = self._key(draft)
        if key in self._drafts:
            return False
        self._drafts[key] = draft
        return True

    def approve(self, key: str, *, reviewer: str) -> HypothesisDraft:
        """A human approves a pending draft (called outside the loop, never by a stage).

        ``reviewer`` must be a non-empty identity that is not the loop's own actor (nor any
        ``research_loop:`` automation identity); it is recorded with the draft and call hashes.
        """
        identity = reviewer.strip() if isinstance(reviewer, str) else ""
        if not identity:
            raise ValueError("a review needs the reviewer's identity")
        if identity in self._automation or identity.startswith(AUTOMATION_ACTOR_PREFIX):
            raise ValueError(f"{identity!r} is an automation identity; only a human may approve")
        draft = self._drafts[key]
        if draft.reviewed:
            raise ValueError(f"{key} was already approved by {self._reviewers[key]!r}")
        reviewed = replace(draft, reviewed=True)
        self._drafts[key] = reviewed
        self._reviewers[key] = identity
        self._approvals.append(
            ReviewApproval(
                key=key,
                reviewer=identity,
                draft_hash=draft.hypothesis.content_hash(),
                call_hash=draft.call.content_hash(),
            )
        )
        return reviewed

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(key for key, draft in self._drafts.items() if not draft.reviewed)

    @property
    def approvals(self) -> tuple[ReviewApproval, ...]:
        return tuple(self._approvals)

    def reviewed_untaken(self) -> tuple[HypothesisDraft, ...]:
        return tuple(
            draft
            for key, draft in self._drafts.items()
            if draft.reviewed and key not in self._taken
        )

    def mark_taken(self, draft: HypothesisDraft) -> None:
        self._taken.add(self._key(draft))

    def reviewer_of(self, draft: HypothesisDraft) -> str | None:
        return self._reviewers.get(self._key(draft))


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
    oos_ledger: InMemoryUnsealingLedger = field(default_factory=InMemoryUnsealingLedger)

    def add_strategy(self, candidate: StrategyCandidate) -> None:
        """Add a candidate to the catalog; the same ref with other content is refused."""
        key = str(candidate.spec.ref)
        known = self.strategies.get(key)
        if known is not None and known.spec.content_hash() != candidate.spec.content_hash():
            raise ValueError(f"{key} is already in the catalog with other content")
        self.strategies[key] = candidate
