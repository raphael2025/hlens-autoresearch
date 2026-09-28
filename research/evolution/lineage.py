"""Strategy lineage graph (Phase 12): every descendant traces back to its ancestors.

Durability (debugging pass, 2026-09-25, ADR-0045 implementation note): an optional ``path``
backs the graph with a hash-chained append-only file (``research.persistence.AppendOnlyJournal``),
so a parent/child relation recorded by one process is still there after a restart. Omit ``path``
and the graph is exactly the in-memory dict it always was, built once from ``specs``.

Write gate (ADR-0073 admission lease review, 2026-09-28): a loop state directory binds a durable
graph to its admission gate (``bind_write_gate``; ``research.persistence.gate``), so every ``add``
runs inside the gate and is refused before writing while the state does not accept writes. The
backing journal is never handed out (its ``append`` would bypass the gate and the in-memory graph):
cross-file checks read ``durable``, ``journal_head()`` or ``journal_snapshot()``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from threading import RLock

from core.domain.base import Ref
from core.domain.specs import StrategySpec
from research.persistence import (
    AppendOnlyJournal,
    JournalCorrupted,
    JournalSnapshot,
    WriteGate,
    gate_scope,
    journal_snapshot,
)

__all__ = ["LineageError", "LineageGraph"]


class LineageError(ValueError):
    """A spec would rewrite an already-recorded ancestor/descendant relation."""


class LineageGraph:
    def __init__(self, specs: Iterable[StrategySpec], *, path: Path | None = None) -> None:
        self._specs: dict[Ref, StrategySpec] = {}
        #: Orders an ``add``'s check / append / insert against snapshots (taken after the gate).
        self._lock = RLock()
        self._gate: WriteGate | None = None
        self._journal = AppendOnlyJournal(path) if path is not None else None
        if self._journal is not None:
            for entry in self._journal.entries:
                if entry.type != "add_spec":
                    raise JournalCorrupted(f"{path}: unknown record type {entry.type!r}")
                self._insert(StrategySpec.model_validate(entry.payload), corrupted_path=path)
        for spec in specs:
            self.add(spec)

    def _insert(self, spec: StrategySpec, *, corrupted_path: Path | None) -> None:
        """Add an already-decided spec to memory (replay or a fresh ``add``)."""
        existing = self._specs.get(spec.ref)
        if existing is not None:
            if existing.content_hash() != spec.content_hash():
                if corrupted_path is not None:
                    raise JournalCorrupted(
                        f"{corrupted_path}: {spec.ref} recorded twice with other content"
                    )
                raise LineageError(f"{spec.ref} is already recorded with other content")
            return
        self._specs[spec.ref] = spec

    def add(self, spec: StrategySpec) -> None:
        """Record ``spec`` (and so its ``lineage`` links); a no-op if it is already recorded.

        Appends one journal line when a path-backed graph sees a ref for the first time; an
        identical re-add (same ref, same content) is silently absorbed, matching ``__init__``'s
        merge of its ``specs`` argument. A different spec under the same ref is refused
        (``LineageError``): a lineage relation, once recorded, is never rewritten. With a bound
        write gate the whole call runs inside it (module docs).
        """
        with gate_scope(self._gate, f"recording lineage spec {spec.ref}"), self._lock:
            existing = self._specs.get(spec.ref)
            if existing is not None:
                if existing.content_hash() != spec.content_hash():
                    raise LineageError(f"{spec.ref} is already recorded with other content")
                return
            if self._journal is not None:
                self._journal.append("add_spec", spec.model_dump(mode="json"))
            self._specs[spec.ref] = spec

    def bind_write_gate(self, gate: WriteGate) -> None:
        """Run every later ``add`` inside ``gate`` (module docs); once only."""
        with self._lock:
            if self._gate is not None:
                raise LineageError("this lineage graph is already bound to a write gate")
            self._gate = gate

    @property
    def durable(self) -> bool:
        """Whether a journal backs this graph."""
        return self._journal is not None

    def journal_head(self) -> tuple[int, str] | None:
        """``(entry count, chain head)`` of the backing journal; ``None``: in memory."""
        with self._lock:
            journal = self._journal
            return None if journal is None else (len(journal.entries), journal.head_hash)

    def journal_snapshot(self) -> JournalSnapshot | None:
        """Detached read-only copy of the backing journal's entries; ``None``: in memory."""
        with self._lock:
            return None if self._journal is None else journal_snapshot(self._journal)

    @property
    def specs(self) -> tuple[StrategySpec, ...]:
        """Every recorded spec in the order it was first recorded (journal order when durable)."""
        return tuple(self._specs.values())

    def parents(self, ref: Ref) -> tuple[Ref, ...]:
        spec = self._specs.get(ref)
        return () if spec is None else tuple(spec.lineage)

    def ancestors(self, ref: Ref) -> tuple[Ref, ...]:
        seen: list[Ref] = []
        stack = list(self.parents(ref))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.append(current)
            stack.extend(self.parents(current))
        return tuple(sorted(seen, key=str))

    def descendants(self, ref: Ref) -> tuple[Ref, ...]:
        return tuple(
            sorted((other for other in self._specs if ref in self.ancestors(other)), key=str)
        )

    def missing(self) -> tuple[Ref, ...]:
        """Lineage links to specs this graph does not hold (untraceable ancestry)."""
        return tuple(
            sorted(
                {parent for spec in self._specs.values() for parent in spec.lineage}
                - set(self._specs),
                key=str,
            )
        )
