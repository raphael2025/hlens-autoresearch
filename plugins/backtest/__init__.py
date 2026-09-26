"""BacktestProvider implementations (Phase 5; ADR-0038). Simulation only — no orders, no keys.

- ``bar``: ``BarBacktester`` — deterministic, ``Decimal``, bar-level; fills at the next bar's open
  with the request's fee + slippage cost model.
- ``execution``: ``ExecutionModel`` — opt-in execution realism for ``BarBacktester`` (bar-volume
  participation cap, square-root impact, per-bar funding); every parameter explicit, none by
  default, bound into the descriptor version and hence ``provider_hash``.
"""

from plugins.backtest.bar import EXECUTION_VERSION, MONEY_QUANTUM, BarBacktester
from plugins.backtest.execution import (
    IMPACT_MODEL,
    ExecutionModel,
    ExecutionReport,
    FillExecution,
    FundingCharge,
    UnfilledRemainder,
)

__all__ = [
    "EXECUTION_VERSION",
    "IMPACT_MODEL",
    "MONEY_QUANTUM",
    "BarBacktester",
    "ExecutionModel",
    "ExecutionReport",
    "FillExecution",
    "FundingCharge",
    "UnfilledRemainder",
]
