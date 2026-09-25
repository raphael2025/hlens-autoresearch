"""Pre-registration and trial counting (Constitution A1-A3, C-T1; 04-research-loop.md §5).

A hypothesis is registered **before** its experiment and is immutable: the same ``name@version``
with other content is refused (a change is a new hypothesis). Every registration counts as a trial
of its family, failures included; the family count is what multiple-testing correction uses.
Nothing is ever removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.domain.research import Hypothesis, HypothesisOrigin

if TYPE_CHECKING:
    from research.hypotheses.generator import HypothesisDraft

__all__ = ["LedgerError", "TrialLedger"]


class LedgerError(ValueError):
    """A registration would rewrite history."""


class TrialLedger:
    def __init__(self) -> None:
        self._registered: dict[tuple[str, str], Hypothesis] = {}
        self._order: list[tuple[str, str]] = []

    def register(self, hypothesis: Hypothesis) -> bool:
        """Register (pre-register) ``hypothesis``; ``False`` if exactly it was already registered.

        LLM-originated hypotheses go through ``register_draft`` (a human review is required).
        """
        if hypothesis.origin is HypothesisOrigin.LLM:
            raise LedgerError("an LLM hypothesis is registered only as a reviewed draft")
        return self._register(hypothesis)

    def register_draft(self, draft: HypothesisDraft) -> bool:
        if not draft.reviewed:
            raise LedgerError(f"{draft.hypothesis.ref} has not been reviewed by a human")
        return self._register(draft.hypothesis)

    def _register(self, hypothesis: Hypothesis) -> bool:
        key = (hypothesis.name, hypothesis.version)
        existing = self._registered.get(key)
        if existing is not None:
            if existing.content_hash() != hypothesis.content_hash():
                raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")
            return False
        self._registered[key] = hypothesis
        self._order.append(key)
        return True

    def trials(self, family_id: str) -> int:
        return sum(1 for h in self._registered.values() if h.family_id == family_id)

    @property
    def hypotheses(self) -> tuple[Hypothesis, ...]:
        return tuple(self._registered[key] for key in self._order)
