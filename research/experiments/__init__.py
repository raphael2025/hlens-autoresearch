"""Experiments (research plane, H5). Phase 6: State x Strategy research (ADR-0039)."""

from research.experiments.state_strategy import (
    RETURN_QUANTUM,
    StateCell,
    StateStrategyMatrix,
    backtest_returns,
    matrix_from_backtest,
    register_conditionals,
    state_strategy_matrix,
)

__all__ = [
    "RETURN_QUANTUM",
    "StateCell",
    "StateStrategyMatrix",
    "backtest_returns",
    "matrix_from_backtest",
    "register_conditionals",
    "state_strategy_matrix",
]
