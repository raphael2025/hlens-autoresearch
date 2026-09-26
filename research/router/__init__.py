"""Dynamic strategy router (Phase 10, ADR-0043): research framework, paper only (H5).

- ``router``: ``StrategyRouter`` — weights from the state known at ``t``, switching turnover;
- ``paper``: ``paper_run`` — routing weights x P5 target positions -> combined targets -> a
  ``BacktestProvider`` run net of switching costs: the router's own ``BacktestResult`` (W1).
"""

from research.router.paper import (
    ROUTER_PAPER_BACKTEST,
    RouterPaperRun,
    RouterStop,
    SwitchingCharge,
    combine_targets,
    paper_run,
    paper_run_or_stop,
)
from research.router.router import (
    RouterError,
    RouterSpec,
    RouterStopped,
    RouterStopReason,
    RoutingDecision,
    StrategyRouter,
)

__all__ = [
    "ROUTER_PAPER_BACKTEST",
    "RouterError",
    "RouterPaperRun",
    "RouterSpec",
    "RouterStop",
    "RouterStopReason",
    "RouterStopped",
    "RoutingDecision",
    "StrategyRouter",
    "SwitchingCharge",
    "combine_targets",
    "paper_run",
    "paper_run_or_stop",
]
