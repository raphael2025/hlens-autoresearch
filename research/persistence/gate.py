"""Write gate and read-only journal views of the durable research stores (ADR-0073 admission
lease review, 2026-09-28).

A loop state directory (``research.loop.durable``) checkpoints the position of every durable
store it holds — the trial ledger, the sealed-OOS unsealing ledger, the lineage graph, the review
queue and the failure registry. A checkpoint reads those positions and then appends its line; an
admission lease requires them to stay put from its PREPARE to its admission checkpoint. Both only
hold if no store write can run in between, so a store bound to a state's gate
(``bind_write_gate``) runs every mutation entry inside ``gate.write_scope(what, token)``:

- the scope is entered **before** the store takes its own lock or touches its journal, so the lock
  order is always gate → store lock → journal lock, and nothing holding a store lock ever enters
  the gate;
- the gate refuses (raises) before anything is written when the state does not accept the write
  (an active admission lease that ``token`` does not identify, or an interrupted admission).

A store not bound to a gate behaves exactly as before. ``gate_scope`` is the null-safe entry.

``JournalSnapshot`` is the read-only view a store hands out instead of its writable
``AppendOnlyJournal`` (whose ``append`` would bypass the gate, the store's lock and its replayed
in-memory state): detached copies of the verified entries at one instant.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from research.persistence.journal import GENESIS_HASH, AppendOnlyJournal, JournalEntry

__all__ = ["JournalSnapshot", "WriteGate", "detached_entry", "gate_scope", "journal_snapshot"]


class WriteGate(Protocol):
    """The in-process write gate of a durable state a store is bound to (module docs)."""

    def write_scope(
        self, what: str, token: object | None = None
    ) -> AbstractContextManager[object]:
        """Held across one whole store write; raises before anything is written when refused.

        ``token``: the store-level credential of an admission lease (the TrialLedger's
        ``LedgerLease``); ``None`` for an ordinary write.
        """
        ...


@contextmanager
def gate_scope(gate: WriteGate | None, what: str, token: object | None = None) -> Iterator[None]:
    """``gate.write_scope(what, token)``, or nothing for an unbound store."""
    if gate is None:
        yield
        return
    with gate.write_scope(what, token):
        yield


@dataclass(frozen=True, slots=True)
class JournalSnapshot:
    """A store's verified journal at one instant: read-only and detached (``entries`` are copies,
    payloads included), so nothing done to it reaches the store, and it cannot append."""

    path: Path
    entries: tuple[JournalEntry, ...]

    @property
    def head_hash(self) -> str:
        """The chain's tip at the snapshot: ``GENESIS_HASH`` for an empty journal."""
        return self.entries[-1].hash if self.entries else GENESIS_HASH


def detached_entry(entry: JournalEntry) -> JournalEntry:
    """A copy of ``entry`` sharing no mutable payload with the journal's own record."""
    return JournalEntry(
        entry.seq, entry.type, copy.deepcopy(entry.payload), entry.prev_hash, entry.hash
    )


def journal_snapshot(journal: AppendOnlyJournal) -> JournalSnapshot:
    """A ``JournalSnapshot`` of ``journal`` (the caller holds whatever lock orders its appends)."""
    return JournalSnapshot(journal.path, tuple(detached_entry(entry) for entry in journal.entries))
