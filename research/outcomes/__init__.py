"""Outcome materialization (Phase 4, ADR-0037). Research code: never production (H5)."""

from research.outcomes.sources import bars_from_synthetic
from research.outcomes.store import (
    OutcomeTableConflict,
    OutcomeTableCorrupted,
    OutcomeTableStore,
    table_hash,
)
from research.outcomes.table import OutcomeTable, materialize

__all__ = [
    "OutcomeTable",
    "OutcomeTableConflict",
    "OutcomeTableCorrupted",
    "OutcomeTableStore",
    "bars_from_synthetic",
    "materialize",
    "table_hash",
]
