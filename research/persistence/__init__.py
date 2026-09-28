"""Shared durable append-only storage for research-plane ledgers (debugging pass, 2026-09-25).

``AppendOnlyJournal`` is the common write path behind the sealed-OOS unsealing ledger
(``research.validation.sealed_oos``), the hypothesis trial ledger (``research.hypotheses.ledger``)
and the strategy lineage graph (``research.evolution.lineage``): the same durability contract as
``research.strategies.failure_registry.FailureRegistry`` (append/flush/fsync, refuse a file that
shrank), plus a SHA-256 hash chain so a restarted process can prove its history was not edited,
reordered or removed after the fact (CLAUDE.md H6).

This is research-plane, file-based persistence, not a database (CLAUDE.md H8/H12): one caller,
one file, one append at a time. It is not a substitute for the Control Plane ledger.

``research.persistence.gate`` (ADR-0073 admission lease review, 2026-09-28): the ``WriteGate`` a
loop state binds its durable stores to (every store write runs inside it, gate before the store's
own lock) and the read-only ``JournalSnapshot`` the stores hand out instead of their writable
journal.

``apps.worker.journal`` implements the same on-disk contract independently for the research
loop's durable audit (``apps/`` must not import ``research/``); a test keeps the two in step.
"""

from __future__ import annotations

from research.persistence.gate import (
    JournalSnapshot,
    WriteGate,
    detached_entry,
    gate_scope,
    journal_snapshot,
)
from research.persistence.journal import (
    GENESIS_HASH,
    AppendOnlyJournal,
    JournalCorrupted,
    JournalEntry,
)

__all__ = [
    "GENESIS_HASH",
    "AppendOnlyJournal",
    "JournalCorrupted",
    "JournalEntry",
    "JournalSnapshot",
    "WriteGate",
    "detached_entry",
    "gate_scope",
    "journal_snapshot",
]
