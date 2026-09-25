"""Dynamic strategy router (Phase 10, ADR-0043): research framework, paper only (H5).

- ``router``: ``StrategyRouter`` — weights from the state known at ``t``, switching turnover;
- ``paper``: ``paper_run`` — routing weights x P5 target positions -> combined targets -> a
  ``BacktestProvider`` run net of switching costs: the router's own ``BacktestResult`` (W1).
"""

from research.router.paper import (
    ROUTER_PAPER_BACKTEST,
    RouterPaperRun,
    SwitchingCharge,
    combine_targets,
    paper_run,
)
from research.router.router import RouterError, RouterSpec, RoutingDecision, StrategyRouter

__all__ = [
    "ROUTER_PAPER_BACKTEST",
    "RouterError",
    "RouterPaperRun",
    "RouterSpec",
    "RoutingDecision",
    "StrategyRouter",
    "SwitchingCharge",
    "combine_targets",
    "paper_run",
]
