"""Strategy evolution (Phase 12, ADR-0045): lineage-preserving operators; research only (H5)."""

from research.evolution.lineage import LineageError, LineageGraph
from research.evolution.operators import (
    EvolutionError,
    Offspring,
    combine,
    mutate,
    require_new_version,
    retire,
)
from research.evolution.proposals import (
    PENDING_HUMAN_APPROVAL,
    ProposalLedger,
    ReplacementProposal,
    propose_replacement,
)

__all__ = [
    "PENDING_HUMAN_APPROVAL",
    "EvolutionError",
    "LineageError",
    "LineageGraph",
    "Offspring",
    "ProposalLedger",
    "ReplacementProposal",
    "combine",
    "mutate",
    "propose_replacement",
    "require_new_version",
    "retire",
]
