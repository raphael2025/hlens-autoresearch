"""Strategy evolution (Phase 12, ADR-0045): lineage-preserving operators; research only (H5).

The replacement proposal job (``research.evolution.replacement_job``) is imported from its module
(it depends on ``research.router.evidence``; the package itself stays light for ``research.loop``).
"""

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
    ProposalAnchor,
    ProposalLedger,
    ProposalLedgerInconsistent,
    ProposalLedgerLocked,
    ReplacementProposal,
    propose_replacement,
)

__all__ = [
    "PENDING_HUMAN_APPROVAL",
    "EvolutionError",
    "LineageError",
    "LineageGraph",
    "Offspring",
    "ProposalAnchor",
    "ProposalLedger",
    "ProposalLedgerInconsistent",
    "ProposalLedgerLocked",
    "ReplacementProposal",
    "combine",
    "mutate",
    "propose_replacement",
    "require_new_version",
    "retire",
]
