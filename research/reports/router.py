"""Writer for Phase 10 router paper runs (``research/router/paper.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.reports.envelope import WrittenReport, write_report_file
from research.router.paper import RouterPaperRun

__all__ = ["KIND", "write_router_paper_run"]

#: Directory name under the report root; matches ``apps.api.store.ReportKind.ROUTER_PAPER_RUN``.
KIND = "router_paper_run"


def _payload(run: RouterPaperRun) -> dict[str, Any]:
    return {
        "router": run.router,
        "router_spec_hash": run.router_spec_hash,
        "state_result_hash": run.state_result_hash,
        "strategy_result_hashes": dict(sorted(run.strategy_result_hashes.items())),
        "decisions": [
            {
                "at": decision.at.isoformat(),
                "state": decision.state,
                "weights": {key: str(value) for key, value in sorted(decision.weights.items())},
                "turnover": str(decision.turnover),
                "switching_cost": str(decision.switching_cost),
            }
            for decision in run.decisions
        ],
        "charges": [
            {
                "decision_time": charge.decision_time.isoformat(),
                "turnover": str(charge.turnover),
                "rate": str(charge.rate),
                "equity_base": str(charge.equity_base),
                "amount": str(charge.amount),
                "charged_at": None if charge.charged_at is None else charge.charged_at.isoformat(),
            }
            for charge in run.charges
        ],
        "total_switching_cost": str(run.total_switching_cost),
        "request_hash": run.result.request_hash,
        "gross_result_hash": run.gross.result_hash,
        "result_hash": run.result.result_hash,
        "initial_equity": str(run.result.initial_equity),
        "final_equity": str(run.result.final_equity),
        "pnl": str(run.result.pnl),
        "run_hash": run.run_hash,
    }


def write_router_paper_run(root: Path, run: RouterPaperRun) -> WrittenReport:
    """Write at ``<root>/router_paper_run/<run_hash>.json``.

    ``run.run_hash`` already binds the router spec, state result, every routed strategy's result
    hash, the routing decisions, the switching charges and both backtest results (module docstring,
    "``RouterPaperRun.run_hash`` binds everything") — the object's own result hash.
    """
    return write_report_file(root, KIND, run.run_hash, _payload(run))
