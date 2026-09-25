"""Pre-registration and trial counting (Constitution A1-A3, C-T1; 04-research-loop.md §5).

A hypothesis is registered **before** its experiment and is immutable: the same ``name@version``
with other content is refused (a change is a new hypothesis). Every registration counts as a trial
of its family, failures included; the family count is what multiple-testing correction uses.
Nothing is ever removed.

Durability (debugging pass, 2026-09-25, ADR-0040 implementation note): an optional ``path``
backs the ledger with a hash-chained append-only file (``research.persistence.AppendOnlyJournal``),
so a family's trial count continues across a process restart instead of resetting to zero. Omit
``path`` and the ledger is exactly the in-memory dict it always was.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.domain.research import Hypothesis, HypothesisOrigin
from research.persistence import AppendOnlyJournal, JournalCorrupted

if TYPE_CHECKING:
    from research.hypotheses.generator import HypothesisDraft

__all__ = ["LedgerError", "TrialLedger"]


class LedgerError(ValueError):
    """A registration would rewrite history."""


class TrialLedger:
    def __init__(self, path: Path | None = None) -> None:
        self._registered: dict[tuple[str, str], Hypothesis] = {}
        self._order: list[tuple[str, str]] = []
        self._journal = AppendOnlyJournal(path) if path is not None else None
        if self._journal is not None:
            for entry in self._journal.entries:
                if entry.type != "register":
                    raise JournalCorrupted(f"{path}: unknown record type {entry.type!r}")
                hypothesis = Hypothesis.model_validate(entry.payload)
                self._insert(hypothesis, corrupted_path=path)

    def _insert(self, hypothesis: Hypothesis, *, corrupted_path: Path | None) -> None:
        """Add an already-decided registration to memory (replay or a fresh ``_register``)."""
        key = (hypothesis.name, hypothesis.version)
        existing = self._registered.get(key)
        if existing is not None:
            if existing.content_hash() != hypothesis.content_hash():
                if corrupted_path is not None:
                    raise JournalCorrupted(
                        f"{corrupted_path}: {hypothesis.ref} registered twice with other content"
                    )
                raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")
            return
        self._registered[key] = hypothesis
        self._order.append(key)

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
        if self._journal is not None:
            self._journal.append("register", hypothesis.model_dump(mode="json"))
        self._registered[key] = hypothesis
        self._order.append(key)
        return True

    def trials(self, family_id: str) -> int:
        return sum(1 for h in self._registered.values() if h.family_id == family_id)

    @property
    def hypotheses(self) -> tuple[Hypothesis, ...]:
        return tuple(self._registered[key] for key in self._order)
