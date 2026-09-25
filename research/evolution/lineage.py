"""Strategy lineage graph (Phase 12): every descendant traces back to its ancestors.

Durability (debugging pass, 2026-09-25, ADR-0045 implementation note): an optional ``path``
backs the graph with a hash-chained append-only file (``research.persistence.AppendOnlyJournal``),
so a parent/child relation recorded by one process is still there after a restart. Omit ``path``
and the graph is exactly the in-memory dict it always was, built once from ``specs``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from core.domain.base import Ref
from core.domain.specs import StrategySpec
from research.persistence import AppendOnlyJournal, JournalCorrupted

__all__ = ["LineageError", "LineageGraph"]


class LineageError(ValueError):
    """A spec would rewrite an already-recorded ancestor/descendant relation."""


class LineageGraph:
    def __init__(self, specs: Iterable[StrategySpec], *, path: Path | None = None) -> None:
        self._specs: dict[Ref, StrategySpec] = {}
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
        (``LineageError``): a lineage relation, once recorded, is never rewritten.
        """
        existing = self._specs.get(spec.ref)
        if existing is not None:
            if existing.content_hash() != spec.content_hash():
                raise LineageError(f"{spec.ref} is already recorded with other content")
            return
        if self._journal is not None:
            self._journal.append("add_spec", spec.model_dump(mode="json"))
        self._specs[spec.ref] = spec

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
