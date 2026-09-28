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

Write lease (ADR-0073 admission lease, 2026-09-28). ``acquire_write_lease`` hands one caller an
opaque ``LedgerLease``; while it is held, every mutation entry refuses any write that does not
present that exact token (``register``, ``register_draft``, ``register_batch`` and
``register_reevaluation`` never accept one, so they are refused in every thread, the holder's own
included), and only ``recover_register_batch(..., lease=...)`` writes. Releasing with ``seal=``
refuses every later mutation of this instance: an interrupted leased transaction leaves the journal
exactly where a reopened state directory can recover it. The lease serializes this instance only; it
is not a cross-process lock (the loop state directory's ``state.lock`` is).

Read-only journal view (ADR-0073 admission lease review, 2026-09-28). The backing
``AppendOnlyJournal`` is never handed out: its ``append`` would bypass the write lease, the seal and
the replayed in-memory state. Cross-file checks read ``durable``, ``journal_head()`` (sequence and
chain head) or ``journal_snapshot()`` (a ``LedgerJournalSnapshot``: detached copies of the verified
entries, taken under the ledger lock) instead.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from core.domain.base import SHA256_PATTERN, canonical_json
from core.domain.research import Hypothesis, HypothesisOrigin
from research.persistence import GENESIS_HASH, AppendOnlyJournal, JournalCorrupted, JournalEntry

if TYPE_CHECKING:
    from research.hypotheses.generator import HypothesisDraft

__all__ = ["LedgerError", "LedgerJournalSnapshot", "LedgerLease", "TrialEntry", "TrialLedger"]


class LedgerError(ValueError):
    """A registration would rewrite history."""


class LedgerLease:
    """Opaque write-lease token of one ``TrialLedger`` (``TrialLedger.acquire_write_lease``).

    Compared by identity only: a token is valid exactly while it is the ledger's current lease.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return f"<LedgerLease {id(self):#x}>"


@dataclass(frozen=True, slots=True)
class LedgerJournalSnapshot:
    """A durable ``TrialLedger``'s verified journal at one instant (``journal_snapshot``).

    Read-only and detached: ``entries`` are copies (payloads included), so nothing done to them
    reaches the ledger, and there is no way to append through it.
    """

    path: Path
    entries: tuple[JournalEntry, ...]

    @property
    def head_hash(self) -> str:
        """The chain's tip at the snapshot: ``GENESIS_HASH`` for an empty journal."""
        return self.entries[-1].hash if self.entries else GENESIS_HASH


def _detached(entry: JournalEntry) -> JournalEntry:
    """A copy of ``entry`` sharing no mutable payload with the journal's own record."""
    return JournalEntry(
        entry.seq, entry.type, copy.deepcopy(entry.payload), entry.prev_hash, entry.hash
    )


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
        # A ledger-level lock covers the check/append/apply sequence. RLock allows public
        # mutation methods to delegate to shared helpers without opening an interleaving gap.
        self._lock = RLock()
        self._registered: dict[tuple[str, str], Hypothesis] = {}
        self._order: list[tuple[str, str]] = []
        self._log: list[TrialEntry] = []
        self._attempts: set[tuple[str, str, str | None]] = set()
        self._journal: AppendOnlyJournal | None = None
        #: The held write lease (module docs) and, once a leased transaction was interrupted, why
        #: this instance refuses every later mutation.
        self._lease: LedgerLease | None = None
        self._sealed: str | None = None
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
    def durable(self) -> bool:
        """Whether a journal backs this ledger (module docs, **Read-only journal view**)."""
        with self._lock:
            return self._journal is not None

    def journal_head(self) -> tuple[int, str] | None:
        """``(entry count, chain head)`` of the backing journal; ``None``: in memory."""
        with self._lock:
            journal = self._journal
            if journal is None:
                return None
            return len(journal.entries), journal.head_hash

    def journal_snapshot(self) -> LedgerJournalSnapshot | None:
        """Detached read-only copy of the backing journal's verified entries; ``None``: in memory.

        Taken under the ledger lock, so it never interleaves with a registration's append.
        """
        with self._lock:
            journal = self._journal
            if journal is None:
                return None
            return LedgerJournalSnapshot(
                journal.path, tuple(_detached(entry) for entry in journal.entries)
            )

    def acquire_write_lease(self) -> LedgerLease:
        """Reserve every later write of this instance for the returned token (module docs).

        Refused (``LedgerError``) while another lease is held or after a sealed release. The holder
        must call ``release_write_lease`` exactly once, also when its transaction fails.
        """
        with self._lock:
            self._check_writer(None, "a write lease")
            lease = LedgerLease()
            self._lease = lease
            return lease

    def release_write_lease(self, lease: LedgerLease, *, seal: str | None = None) -> None:
        """End ``lease``; with ``seal`` (a reason), refuse every later mutation of this instance.

        Sealing is final for the instance: only reopening the ledger from its journal clears it.
        """
        with self._lock:
            if self._lease is not lease:
                raise LedgerError("this write lease is not the TrialLedger's current lease")
            if seal is not None:
                self._sealed = seal
            self._lease = None

    def _check_writer(self, lease: LedgerLease | None, what: str) -> None:
        """Refuse ``what`` unless ``lease`` is exactly the held lease (``None``: none is held)."""
        if self._sealed is not None:
            raise LedgerError(
                f"{what} is refused: this TrialLedger stopped accepting writes ({self._sealed}); "
                "reopen the loop state directory to recover it"
            )
        if lease is not self._lease:
            raise LedgerError(
                f"{what} is refused: "
                + (
                    "a typed-plan admission holds this TrialLedger's write lease"
                    if self._lease is not None
                    else "the write lease it presents is not held"
                )
            )

    def register(self, hypothesis: Hypothesis) -> bool:
        """Register (pre-register) ``hypothesis``; ``False`` if exactly it was already registered.

        LLM-originated hypotheses go through ``register_draft`` (a human review is required).
        """
        with self._lock:
            self._check_writer(None, f"registering {hypothesis.ref}")
            if hypothesis.origin is HypothesisOrigin.LLM:
                raise LedgerError("an LLM hypothesis is registered only as a reviewed draft")
            return self._register(hypothesis)

    def register_draft(self, draft: HypothesisDraft) -> bool:
        with self._lock:
            self._check_writer(None, "registering a reviewed draft")
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
        ``HypothesisDraft`` evidence required by ``register_draft``. Refused while a write lease is
        held (module docs).
        """
        with self._lock:
            self._check_writer(None, "a batch registration")
            return self._register_batch(hypotheses)

    def _register_batch(self, hypotheses: Iterable[Hypothesis]) -> tuple[Hypothesis, ...]:
        """``register_batch`` once the caller checked the writer under ``self._lock``."""
        with self._lock:
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

    def recover_register_batch(
        self,
        hypotheses: Iterable[Hypothesis],
        *,
        baseline_seq: int,
        baseline_hash: str,
        lease: LedgerLease,
    ) -> JournalEntry:
        """Append or recognize exactly one prepared batch at its recorded journal baseline.

        This is the ledger half of ADR-0073's PREPARE / batch / COMMIT protocol. If the journal
        remains at ``(baseline_seq, baseline_hash)``, every requested identity must still be new
        and the method appends one normal ``register_batch`` event. If the journal is exactly one
        event past that baseline, the event must match this full batch byte-for-byte in content,
        sequence, previous hash, type, and recomputed entry hash; it is then returned without
        counting the trials again. Any other tail or an in-memory identity reuse is refused.

        The method requires a durable ledger and the held write ``lease`` (module docs), and holds
        the ledger RLock across inspection and append. It does not repair a corrupt / partial
        journal or reload a stale journal object; callers must reopen the ledger after process
        restart so its verified replay is current.
        """
        with self._lock:
            self._check_writer(lease, "a prepared batch recovery")
            if type(baseline_seq) is not int or baseline_seq < 0:
                raise LedgerError("baseline_seq must be a non-negative integer")
            if (
                not isinstance(baseline_hash, str)
                or re.fullmatch(SHA256_PATTERN, baseline_hash) is None
            ):
                raise LedgerError("baseline_hash must be a lowercase SHA-256 hash")
            journal = self._journal
            if journal is None:
                raise LedgerError("prepared batch recovery requires a durable TrialLedger")

            try:
                batch = tuple(hypotheses)
            except TypeError as exc:
                raise LedgerError(
                    "a recovery batch must be an iterable of Hypothesis values"
                ) from exc
            if not batch:
                raise LedgerError("a prepared batch must contain at least one hypothesis")

            keys: set[tuple[str, str]] = set()
            for hypothesis in batch:
                if not isinstance(hypothesis, Hypothesis):
                    raise LedgerError("a recovery batch contains only Hypothesis values")
                if hypothesis.origin is HypothesisOrigin.LLM:
                    raise LedgerError(
                        "an LLM hypothesis is registered only as a reviewed draft: "
                        f"{hypothesis.ref}"
                    )
                key = (hypothesis.name, hypothesis.version)
                if key in keys:
                    raise LedgerError(f"a recovery batch repeats {hypothesis.ref}")
                keys.add(key)

            payload = {
                "hypotheses": [hypothesis.model_dump(mode="json") for hypothesis in batch]
            }
            payload_json = json.loads(canonical_json(payload))
            entries = journal.entries

            if len(entries) == baseline_seq:
                head = entries[-1].hash if entries else "0" * 64
                if head != baseline_hash:
                    raise LedgerError("the TrialLedger does not match the prepared baseline hash")
                reused = [
                    hypothesis.ref
                    for hypothesis in batch
                    if (hypothesis.name, hypothesis.version) in self._registered
                ]
                if reused:
                    raise LedgerError(
                        "a prepared recovery batch cannot reuse registered identities: "
                        + ", ".join(reused)
                    )
                registered = self._register_batch(batch)
                if len(registered) != len(batch):
                    raise LedgerError("the prepared recovery batch was not wholly registered")
                return _detached(journal.entries[-1])

            if len(entries) != baseline_seq + 1:
                raise LedgerError(
                    "the TrialLedger has entries beyond the one event allowed by this prepare"
                )

            entry = entries[baseline_seq]
            if (
                entry.seq != baseline_seq + 1
                or entry.type != "register_batch"
                or entry.prev_hash != baseline_hash
                or dict(entry.payload) != payload_json
            ):
                raise LedgerError(
                    "the TrialLedger tail does not exactly match the prepared batch event"
                )
            expected_hash = hashlib.sha256(
                canonical_json(
                    {
                        "seq": entry.seq,
                        "type": entry.type,
                        "payload": payload_json,
                        "prev_hash": baseline_hash,
                    }
                ).encode("utf-8")
            ).hexdigest()
            if entry.hash != expected_hash:
                raise LedgerError("the prepared batch event hash does not match its envelope")
            if any(
                self._registered.get((hypothesis.name, hypothesis.version)) != hypothesis
                for hypothesis in batch
            ):
                raise LedgerError("the replayed TrialLedger state differs from its matching event")
            return _detached(entry)

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
        with self._lock:
            self._check_writer(None, f"a re-evaluation of {hypothesis.ref}")
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
        with self._lock:
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
        with self._lock:
            existing = self._registered.get((hypothesis.name, hypothesis.version))
            return (
                existing is not None
                and existing.content_hash() == hypothesis.content_hash()
                and (hypothesis.name, hypothesis.version, attempt) in self._attempts
            )

    def trials(self, family_id: str) -> int:
        """Every trial of the family: registrations and re-evaluations, failures included."""
        with self._lock:
            return sum(1 for entry in self._log if entry.family_id == family_id)

    def trial_index(self, hypothesis: Hypothesis, attempt: str | None = None) -> int:
        """1-based position of the trial ``(hypothesis, attempt)`` among its family's trials."""
        with self._lock:
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
        with self._lock:
            return tuple(self._log)

    @property
    def hypotheses(self) -> tuple[Hypothesis, ...]:
        with self._lock:
            return tuple(self._registered[key] for key in self._order)
