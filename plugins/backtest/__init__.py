"""BacktestProvider implementations (Phase 5; ADR-0038). Simulation only — no orders, no keys.

- ``bar``: ``BarBacktester`` — deterministic, ``Decimal``, bar-level; fills at the next bar's open
  with the request's fee + slippage cost model.
"""

from plugins.backtest.bar import MONEY_QUANTUM, BarBacktester

__all__ = ["MONEY_QUANTUM", "BarBacktester"]
