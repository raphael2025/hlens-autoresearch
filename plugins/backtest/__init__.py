"""BacktestProvider implementations (Phase 5; ADR-0038). Simulation only — no orders, no keys.

- ``bar``: ``BarBacktester`` — deterministic, ``Decimal``, bar-level; fills at the next bar's open
  with the request's fee + slippage cost model.
- ``execution``: ``ExecutionModel`` — opt-in execution realism for ``BarBacktester`` (bar-volume
  participation cap, square-root impact, per-bar funding); every parameter explicit, none by
  default, bound into the descriptor version and hence ``provider_hash``. With ``carry_over``
  (ADR-0054) the cap's remainder carries to later bars (``next_bar_open_participation``).
- ``risk_loop``: ``BarBacktester.run_with_risk`` support (ADR-0088 decision 3) — a ``RiskProvider``
  applied per decision time with ``PortfolioState.equity`` / ``peak_equity`` from the realized
  equity path (``RealizedEquityPath``, ``realized_portfolio_state``); nothing later than the
  decision time is read.
- ``reference``: ``ReferenceBacktester`` — a second, independent ``next_bar_open`` implementation
  (ADR-0106; the Phase 14 migration target). Not a production default; it refuses execution models,
  carry-over and the risk loop.
"""

from plugins.backtest.bar import (
    CARRY_OVER_VERSION,
    EXECUTION_VERSION,
    MONEY_QUANTUM,
    BarBacktester,
)
from plugins.backtest.execution import (
    IMPACT_MODEL,
    ExecutionModel,
    ExecutionReport,
    FillExecution,
    FundingCharge,
    UnfilledRemainder,
)
from plugins.backtest.reference import (
    REFERENCE_MONEY_QUANTUM,
    ReferenceBacktester,
    ReferenceScopeError,
)
from plugins.backtest.risk_loop import (
    RealizedEquityPath,
    RiskLoop,
    RiskLoopRun,
    realized_portfolio_state,
)

__all__ = [
    "CARRY_OVER_VERSION",
    "EXECUTION_VERSION",
    "IMPACT_MODEL",
    "MONEY_QUANTUM",
    "REFERENCE_MONEY_QUANTUM",
    "BarBacktester",
    "ExecutionModel",
    "ExecutionReport",
    "FillExecution",
    "FundingCharge",
    "RealizedEquityPath",
    "RiskLoop",
    "ReferenceBacktester",
    "ReferenceScopeError",
    "RiskLoopRun",
    "UnfilledRemainder",
    "realized_portfolio_state",
]
