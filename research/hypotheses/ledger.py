"""Pre-registration and trial counting (Constitution A1-A3, C-T1; 04-research-loop.md §5).

A hypothesis is registered **before** its experiment and is immutable: the same ``name@version``
with other content is refused (a change is a new hypothesis). Every registration counts as a trial
of its family, failures included; the family count is what multiple-testing correction uses.
Nothing is ever removed.

A registered hypothesis evaluated **again** (e.g. the continuous loop re-evaluating an
INCONCLUSIVE hypothesis on a grown research window, ADR-0049 accumulated-window note) is another
trial: ``register_reevaluation`` pre-registers it under an explicit ``attempt`` key before it runs,
and it counts towards the family exactly like a registration. The trial log (``trial_log``) keeps
every trial in order, so ``trial_index`` / ``trials`` include every re-evaluation.

Durability (debugging pass, 2026-09-25, ADR-0040 implementation note): an optional ``path``
backs the ledger with a hash-chained append-only file (``research.persistence.AppendOnlyJournal``).
A single registration (``register``), an all-at-once batch registration (``register_batch``), and
a pre-registered re-evaluation (``reevaluate``) each use one journal line, so a family's trial
count — re-evaluations included — continues across a process restart instead of resetting to zero.
Omit ``path`` and the ledger is purely in memory, as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.domain.research import Hypothesis, HypothesisOrigin
from research.persistence import AppendOnlyJournal, JournalCorrupted

if TYPE_CHECKING:
    from research.hypotheses.generator import HypothesisDraft

__all__ = ["LedgerError", "TrialEntry", "TrialLedger"]


class LedgerError(ValueError):
    """A registration would rewrite history."""


@dataclass(frozen=True, slots=True)
class TrialEntry:
    """One counted trial: a hypothesis's registration or one of its pre-registered re-evaluations.

    ``attempt`` is ``None`` for the trial the registration itself stands for, otherwise the
    re-evaluation's key (unique per hypothesis).
    """

    name: str
    version: str
    family_id: str
    hypothesis_hash: str
    attempt: str | None


class TrialLedger:
    def __init__(self, path: Path | None = None) -> None:
        self._registered: dict[tuple[str, str], Hypothesis] = {}
        self._order: list[tuple[str, str]] = []
        self._log: list[TrialEntry] = []
        self._attempts: set[tuple[str, str, str | None]] = set()
        self._journal: AppendOnlyJournal | None = None
        if path is not None:
            journal = AppendOnlyJournal(path)
            for entry in journal.entries:
                self._replay(path, entry.type, entry.payload)
            self._journal = journal  # set after replay: replaying never re-appends

    def _replay(self, path: Path, kind: str, payload: object) -> None:
        """Re-apply one verified journal line; any inconsistency is corruption (fail closed)."""
        try:
            if kind == "register":
                if not self._register(Hypothesis.model_validate(payload)):
                    raise JournalCorrupted(f"{path}: duplicate registration line")
            elif kind == "register_batch":
                self._replay_batch(path, payload)
            elif kind == "reevaluate" and isinstance(payload, dict):
                hypothesis = Hypothesis.model_validate(payload["hypothesis"])
                if not self.register_reevaluation(hypothesis, str(payload["attempt"])):
                    raise JournalCorrupted(f"{path}: duplicate re-evaluation line")
            else:
                raise JournalCorrupted(f"{path}: unknown record type {kind!r}")
        except (LedgerError, KeyError, ValueError) as exc:  # JournalCorrupted passes through
            raise JournalCorrupted(f"{path}: inconsistent ledger line: {exc}") from exc

    @property
    def journal(self) -> AppendOnlyJournal | None:
        """The backing journal (``None``: in memory); read-only use, for cross-file checks."""
        return self._journal

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

    def register_batch(self, hypotheses: Iterable[Hypothesis]) -> tuple[Hypothesis, ...]:
        """Atomically pre-register a batch in one journal event.

        Every conflict and duplicate within the input is checked before the journal is touched.
        Identical hypotheses already in the ledger are idempotent and are omitted from the new
        event. If the journal is durable, one ``register_batch`` line is fsync'd before any
        in-memory state changes; an append failure therefore leaves this instance unchanged.

        LLM-originated hypotheses are refused here: this API accepts hypotheses, not the reviewed
        ``HypothesisDraft`` evidence required by ``register_draft``.
        """
        try:
            batch = tuple(hypotheses)
        except TypeError as exc:
            raise LedgerError("a batch must be an iterable of Hypothesis values") from exc
        if not batch:
            return ()

        keys: set[tuple[str, str]] = set()
        pending: list[Hypothesis] = []
        for hypothesis in batch:
            if not isinstance(hypothesis, Hypothesis):
                raise LedgerError("a batch contains only Hypothesis values")
            if hypothesis.origin is HypothesisOrigin.LLM:
                raise LedgerError(
                    f"an LLM hypothesis is registered only as a reviewed draft: {hypothesis.ref}"
                )
            key = (hypothesis.name, hypothesis.version)
            if key in keys:
                raise LedgerError(f"a registration batch lists {hypothesis.ref} more than once")
            keys.add(key)
            existing = self._registered.get(key)
            if existing is None:
                pending.append(hypothesis)
            elif existing.content_hash() != hypothesis.content_hash():
                raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")

        if not pending:
            return ()

        payload = {
            "hypotheses": [hypothesis.model_dump(mode="json") for hypothesis in pending]
        }
        if self._journal is not None:
            # The journal append is the durable commit point. Do not expose partial in-memory
            # registration if the append fails (including a stale-writer refusal).
            self._journal.append("register_batch", payload)
        for hypothesis in pending:
            self._apply_registration(hypothesis)
        return tuple(pending)

    def _replay_batch(self, path: Path, payload: object) -> None:
        """Replay one strictly shaped batch record; any duplicate or invalid member is corruption."""
        if not isinstance(payload, dict) or set(payload) != {"hypotheses"}:
            raise JournalCorrupted(f"{path}: malformed batch registration payload")
        raw_hypotheses = payload["hypotheses"]
        if not isinstance(raw_hypotheses, list) or not raw_hypotheses:
            raise JournalCorrupted(f"{path}: a batch registration must contain hypotheses")

        hypotheses: list[Hypothesis] = []
        for raw in raw_hypotheses:
            if not isinstance(raw, dict):
                raise JournalCorrupted(f"{path}: a batch hypothesis payload must be an object")
            hypothesis = Hypothesis.model_validate(raw)
            # Prevent permissive model parsing from silently dropping fields or coercing a
            # different serialized value while replaying a supposedly canonical journal event.
            if hypothesis.model_dump(mode="json") != raw:
                raise JournalCorrupted(f"{path}: non-canonical batch hypothesis payload")
            if hypothesis.origin is HypothesisOrigin.LLM:
                raise JournalCorrupted(f"{path}: batch registration contains an LLM hypothesis")
            hypotheses.append(hypothesis)

        keys = [(hypothesis.name, hypothesis.version) for hypothesis in hypotheses]
        if len(keys) != len(set(keys)):
            raise JournalCorrupted(f"{path}: batch registration repeats a hypothesis identity")
        for hypothesis in hypotheses:
            if (hypothesis.name, hypothesis.version) in self._registered:
                raise JournalCorrupted(f"{path}: batch registration duplicates an earlier entry")
        self._apply_batch(hypotheses)

    def register_reevaluation(self, hypothesis: Hypothesis, attempt: str) -> bool:
        """Pre-register one more evaluation of a registered hypothesis as its own trial.

        ``hypothesis`` must be registered with exactly this content (a changed hypothesis is a
        new version, never a re-evaluation); ``attempt`` is a non-empty key naming this
        evaluation. ``False`` if exactly this attempt was already registered (not a new trial).
        """
        key = (hypothesis.name, hypothesis.version)
        existing = self._registered.get(key)
        if existing is None:
            raise LedgerError(f"{hypothesis.ref} is not registered: register it first")
        if existing.content_hash() != hypothesis.content_hash():
            raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")
        label = attempt.strip() if isinstance(attempt, str) else ""
        if not label:
            raise LedgerError("a re-evaluation needs a non-empty attempt key")
        if (*key, label) in self._attempts:
            return False
        if self._journal is not None:
            self._journal.append(
                "reevaluate", {"hypothesis": hypothesis.model_dump(mode="json"), "attempt": label}
            )
        self._append(hypothesis, label)
        return True

    def _register(self, hypothesis: Hypothesis) -> bool:
        key = (hypothesis.name, hypothesis.version)
        existing = self._registered.get(key)
        if existing is not None:
            if existing.content_hash() != hypothesis.content_hash():
                raise LedgerError(f"{hypothesis.ref} is registered with other content: new version")
            return False
        if self._journal is not None:
            self._journal.append("register", hypothesis.model_dump(mode="json"))
        self._apply_registration(hypothesis)
        return True

    def _apply_batch(self, hypotheses: Iterable[Hypothesis]) -> None:
        for hypothesis in hypotheses:
            self._apply_registration(hypothesis)

    def _apply_registration(self, hypothesis: Hypothesis) -> None:
        key = (hypothesis.name, hypothesis.version)
        self._registered[key] = hypothesis
        self._order.append(key)
        self._append(hypothesis, None)

    def _append(self, hypothesis: Hypothesis, attempt: str | None) -> None:
        self._attempts.add((hypothesis.name, hypothesis.version, attempt))
        self._log.append(
            TrialEntry(
                name=hypothesis.name,
                version=hypothesis.version,
                family_id=hypothesis.family_id,
                hypothesis_hash=hypothesis.content_hash(),
                attempt=attempt,
            )
        )

    def is_registered(self, hypothesis: Hypothesis, attempt: str | None = None) -> bool:
        """``hypothesis`` (exactly this content) has the trial ``attempt`` (``None``: its
        registration) in the ledger."""
        existing = self._registered.get((hypothesis.name, hypothesis.version))
        return (
            existing is not None
            and existing.content_hash() == hypothesis.content_hash()
            and (hypothesis.name, hypothesis.version, attempt) in self._attempts
        )

    def trials(self, family_id: str) -> int:
        """Every trial of the family: registrations and re-evaluations, failures included."""
        return sum(1 for entry in self._log if entry.family_id == family_id)

    def trial_index(self, hypothesis: Hypothesis, attempt: str | None = None) -> int:
        """1-based position of the trial ``(hypothesis, attempt)`` among its family's trials."""
        family = [e for e in self._log if e.family_id == hypothesis.family_id]
        for index, entry in enumerate(family, start=1):
            if (entry.name, entry.version, entry.attempt) == (
                hypothesis.name,
                hypothesis.version,
                attempt,
            ):
                return index
        raise LedgerError(f"{hypothesis.ref} has no registered trial {attempt!r}")

    @property
    def trial_log(self) -> tuple[TrialEntry, ...]:
        """Every trial in registration order (nothing is ever removed)."""
        return tuple(self._log)

    @property
    def hypotheses(self) -> tuple[Hypothesis, ...]:
        return tuple(self._registered[key] for key in self._order)
