"""Hypothesis registration, combination operators and generation (Phase 7, ADR-0040)."""

from research.hypotheses.dsl import (
    conditioning,
    ensemble,
    interaction,
    negation,
    temporal,
    transformation,
)
from research.hypotheses.generator import HypothesisDraft, from_knowledge, from_llm
from research.hypotheses.ledger import LedgerError, TrialLedger

__all__ = [
    "HypothesisDraft",
    "LedgerError",
    "TrialLedger",
    "conditioning",
    "ensemble",
    "from_knowledge",
    "from_llm",
    "interaction",
    "negation",
    "temporal",
    "transformation",
]
