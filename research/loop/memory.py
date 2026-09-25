"""Research memory shared by the loop's stages across rounds (Phase 11; ADR-0049).

- ``TrialLedger`` (Phase 7): pre-registration and trial counting; nothing is removed.
- ``ReviewQueue``: LLM hypothesis drafts wait here for a **human** review (ADR-0040, 09-security.md
  §3). The loop only enqueues and later registers drafts a human approved; it never approves.
- ``FailureRegistry`` (Phase 5): append-only record of REJECTED / FAILED subjects (H6).
- ``experiments``: every experiment summary, failures included, in round order (append-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from research.hypotheses import HypothesisDraft, TrialLedger
from research.strategies.failure_registry import FailureRegistry

__all__ = ["ResearchMemory", "ReviewQueue"]


class ReviewQueue:
    """LLM drafts awaiting human review, keyed by ``name@version`` (first draft wins)."""

    def __init__(self) -> None:
        self._drafts: dict[str, HypothesisDraft] = {}
        self._reviewers: dict[str, str] = {}
        self._taken: set[str] = set()

    @staticmethod
    def _key(draft: HypothesisDraft) -> str:
        return f"{draft.hypothesis.name}@{draft.hypothesis.version}"

    def enqueue(self, draft: HypothesisDraft) -> bool:
        if draft.reviewed:
            raise ValueError("the loop only enqueues unreviewed drafts")
        key = self._key(draft)
        if key in self._drafts:
            return False
        self._drafts[key] = draft
        return True

    def approve(self, key: str, *, reviewer: str) -> HypothesisDraft:
        """A human approves a pending draft (called outside the loop, never by a stage)."""
        if not reviewer.strip():
            raise ValueError("a review needs the reviewer's identity")
        draft = self._drafts[key]
        reviewed = replace(draft, reviewed=True)
        self._drafts[key] = reviewed
        self._reviewers[key] = reviewer
        return reviewed

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(key for key, draft in self._drafts.items() if not draft.reviewed)

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
