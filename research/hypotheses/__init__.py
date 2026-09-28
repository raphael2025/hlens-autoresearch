"""Hypothesis registration, combination operators, batches and generation (Phase 7, ADR-0040)."""

from research.hypotheses.batch import (
    BatchGrid,
    BatchOperator,
    BatchRefused,
    HypothesisBatch,
    ReviewedOperators,
    expand_batch,
    preregister_batch,
)
from research.hypotheses.dsl import (
    conditioning,
    ensemble,
    interaction,
    negation,
    temporal,
    transformation,
)
from research.hypotheses.generator import (
    HypothesisDraft,
    KnowledgeSearch,
    KnowledgeSource,
    LlmDraftRejected,
    from_knowledge,
    from_llm,
)
from research.hypotheses.ledger import (
    LedgerError,
    LedgerJournalSnapshot,
    LedgerLease,
    TrialEntry,
    TrialLedger,
)

__all__ = [
    "BatchGrid",
    "BatchOperator",
    "BatchRefused",
    "HypothesisBatch",
    "HypothesisDraft",
    "KnowledgeSearch",
    "KnowledgeSource",
    "LedgerError",
    "LedgerJournalSnapshot",
    "LedgerLease",
    "LlmDraftRejected",
    "ReviewedOperators",
    "TrialEntry",
    "TrialLedger",
    "conditioning",
    "ensemble",
    "expand_batch",
    "from_knowledge",
    "from_llm",
    "interaction",
    "negation",
    "preregister_batch",
    "temporal",
    "transformation",
]
