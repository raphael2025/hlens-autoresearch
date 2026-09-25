"""Strategy lineage graph (Phase 12): every descendant traces back to its ancestors."""

from __future__ import annotations

from collections.abc import Iterable

from core.domain.base import Ref
from core.domain.specs import StrategySpec

__all__ = ["LineageGraph"]


class LineageGraph:
    def __init__(self, specs: Iterable[StrategySpec]) -> None:
        self._specs = {spec.ref: spec for spec in specs}

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
