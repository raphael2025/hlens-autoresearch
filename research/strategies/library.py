"""The Phase 5 research strategy library (ADR-0038): entries, sources, parameter spaces.

Every entry has a source (KnowledgeItem refs in the spec / policy lineage), a declared parameter
space and a hypothesis family (for the trial count). Registration is not validity: every entry is
``NOT_VALIDATED`` until it passes the validation pipeline (``validation.py``, ADR-0037 / 0041) with
frozen Profile numbers. Nothing here is promoted;
``strategies/`` and ``risk/`` stay empty (ADR-0005).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from core.contracts.knowledge import KnowledgeProvider, KnowledgeQuery
from core.contracts.strategy import StrategyProvider
from core.domain.base import Kind, Ref
from core.domain.research import KnowledgeItem
from core.domain.specs import RiskPolicy, StrategySpec
from research.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumProvider,
    xsmom_spec,
)
from research.strategies.pipeline import StrategyCandidate
from research.strategies.time_series_momentum import (
    TimeSeriesMomentumProvider,
    tsmom_spec,
    tsmom_vol_scaled_spec,
)
from research.strategies.volatility_target import VolatilityTargetRiskProvider, vol_target_policy

__all__ = [
    "LibraryEntry",
    "MissingKnowledgeSource",
    "library_entries",
    "resolve_knowledge",
]


class MissingKnowledgeSource(LookupError):
    """A lineage ref names a KnowledgeItem the knowledge base does not hold."""


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    spec: StrategySpec
    family: str
    hypothesis_family_id: str
    risk_policy: RiskPolicy | None = None
    #: Builds the entry's ``StrategyProvider`` from its spec (TSMOM unless the entry says so).
    provider: Callable[[Sequence[StrategySpec]], StrategyProvider] = TimeSeriesMomentumProvider

    @property
    def sources(self) -> tuple[Ref, ...]:
        """Every KnowledgeItem ref of the strategy and of its risk policy."""
        refs = list(self.spec.lineage)
        if self.risk_policy is not None:
            refs.extend(self.risk_policy.lineage)
        return tuple(ref for ref in refs if ref.kind is Kind.KNOWLEDGE)

    def candidate(self) -> StrategyCandidate:
        risk = (
            VolatilityTargetRiskProvider((self.risk_policy,))
            if self.risk_policy is not None
            else None
        )
        return StrategyCandidate(
            spec=self.spec,
            strategy=self.provider((self.spec,)),
            hypothesis_family_id=self.hypothesis_family_id,
            risk_policy=self.risk_policy,
            risk=risk,
        )


def library_entries() -> tuple[LibraryEntry, ...]:
    return (
        LibraryEntry(
            spec=tsmom_spec(),
            family="trend",
            hypothesis_family_id="tsmom_bars",
        ),
        LibraryEntry(
            spec=tsmom_vol_scaled_spec(),
            family="trend",
            hypothesis_family_id="tsmom_bars",
            risk_policy=vol_target_policy(),
        ),
        LibraryEntry(
            spec=xsmom_spec(),
            family="cross_sectional_momentum",
            hypothesis_family_id="xsmom_bars",
            provider=CrossSectionalMomentumProvider,
        ),
    )


def resolve_knowledge(
    refs: Iterable[Ref], provider: KnowledgeProvider
) -> tuple[KnowledgeItem, ...]:
    """Look every ref up in the knowledge base; a missing source fails closed."""
    found: list[KnowledgeItem] = []
    for ref in refs:
        if ref.kind is not Kind.KNOWLEDGE:
            raise ValueError(f"{ref} is not a knowledge ref")
        items = provider.search(KnowledgeQuery(name_prefix=ref.name, limit=1000)).items
        match = [item for item in items if item.ref.target_identity() == ref.target_identity()]
        if not match:
            raise MissingKnowledgeSource(f"{ref} is not in the knowledge base")
        found.append(match[0])
    return tuple(found)
