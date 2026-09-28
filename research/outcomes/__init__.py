"""Outcome materialization (Phase 4, ADR-0037). Research code: never production (H5)."""

from research.outcomes.sources import bars_from_synthetic, outcome_events_from_event_result
from research.outcomes.store import (
    OutcomeTableConflict,
    OutcomeTableCorrupted,
    OutcomeTableStore,
    table_hash,
)
from research.outcomes.table import OutcomeTable, materialize
from research.outcomes.volatility import VolatilityWiringError, select_volatility_for_entry_times

__all__ = [
    "OutcomeTable",
    "OutcomeTableConflict",
    "OutcomeTableCorrupted",
    "OutcomeTableStore",
    "VolatilityWiringError",
    "bars_from_synthetic",
    "materialize",
    "outcome_events_from_event_result",
    "select_volatility_for_entry_times",
    "table_hash",
]
