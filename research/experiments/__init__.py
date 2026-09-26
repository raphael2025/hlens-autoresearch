"""Experiments (research plane, H5). Phase 6: State x Strategy research (ADR-0039)."""

from research.experiments.state_strategy import (
    RETURN_QUANTUM,
    UNKNOWN_STATE_VALUE,
    CellSupport,
    ConditionalRegistration,
    StateCell,
    StateStrategyMatrix,
    backtest_returns,
    conditional_hypotheses,
    matrix_from_backtest,
    register_conditionals,
    register_matrix_conditionals,
    state_strategy_matrix,
)

__all__ = [
    "RETURN_QUANTUM",
    "UNKNOWN_STATE_VALUE",
    "CellSupport",
    "ConditionalRegistration",
    "StateCell",
    "StateStrategyMatrix",
    "backtest_returns",
    "conditional_hypotheses",
    "matrix_from_backtest",
    "register_conditionals",
    "register_matrix_conditionals",
    "state_strategy_matrix",
]
