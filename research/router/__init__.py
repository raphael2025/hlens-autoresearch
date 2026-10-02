"""Dynamic strategy router (Phase 10, ADR-0043): research framework, paper only (H5).

- ``router``: ``StrategyRouter`` — weights from the state known at ``t``, switching turnover;
- ``evidence``: eligibility evidence mode (P10-ELIG) — routed strategies checked against their
  actual ``ValidationReport`` (hash, subject, PASS, sealed-OOS G5);
- ``paper``: ``paper_run`` — routing weights x P5 target positions -> combined targets -> a
  ``BacktestProvider`` run net of switching costs: the router's own ``BacktestResult`` (W1).
"""

from research.router.deviation import (
    DeviationError,
    DeviationRefusal,
    PaperDeviation,
    RunBinding,
    paper_deviation,
    validate_scope_bound_payload,
)
from research.router.evidence import (
    EligibilityCheck,
    EligibilityEvidence,
    EligibilityRefusal,
    ReportResolver,
    ReportUnreadable,
    check_report,
    report_store_resolver,
)
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
    RouterEligibilityRefused,
    RouterError,
    RouterSpec,
    RouterStopped,
    RouterStopReason,
    RoutingDecision,
    StrategyRouter,
)

__all__ = [
    "ROUTER_PAPER_BACKTEST",
    "DeviationError",
    "EligibilityCheck",
    "EligibilityEvidence",
    "EligibilityRefusal",
    "DeviationRefusal",
    "PaperDeviation",
    "RunBinding",
    "ReportResolver",
    "ReportUnreadable",
    "RouterEligibilityRefused",
    "RouterError",
    "RouterPaperRun",
    "RouterSpec",
    "RouterStop",
    "RouterStopReason",
    "RouterStopped",
    "RoutingDecision",
    "StrategyRouter",
    "SwitchingCharge",
    "check_report",
    "combine_targets",
    "paper_deviation",
    "validate_scope_bound_payload",
    "paper_run",
    "paper_run_or_stop",
    "report_store_resolver",
]
