"""Writers for Phase 10 router paper runs and router stops (``research/router/paper.py``).

A ``RouterStop`` (the router refused to run: no validated candidate, or every route flat) is its
own report kind, ``router_stop``, never a ``router_paper_run`` with missing fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.contracts.strategy import EquityPoint
from research.reports.envelope import WrittenReport, write_report_file
from research.router.paper import RouterPaperRun, RouterStop

__all__ = ["KIND", "STOP_KIND", "write_router_paper_run", "write_router_stop"]

#: Directory name under the report root; matches ``apps.api.store.ReportKind.ROUTER_PAPER_RUN``.
KIND = "router_paper_run"
#: Directory name of router stops (the API / console exposure is a separate step).
STOP_KIND = "router_stop"


def _equity_curve(points: tuple[EquityPoint, ...]) -> list[dict[str, Any]]:
    return [
        {
            "time": point.time.isoformat(),
            "cash": str(point.cash),
            "equity": str(point.equity),
            "gross_exposure": str(point.gross_exposure),
        }
        for point in points
    ]


def _payload(run: RouterPaperRun) -> dict[str, Any]:
    payload = _run_payload(run)
    if run.validation_reports is not None:  # only when supplied: earlier payloads are unchanged
        payload["validation_reports"] = dict(sorted(run.validation_reports.items()))
    return payload


def _run_payload(run: RouterPaperRun) -> dict[str, Any]:
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
        # Display-only (not part of run_hash, which already binds gross_result_hash / result_hash):
        # the console's before- vs after-switching-cost equity chart needs the point series, not
        # just the endpoints.
        "gross_equity_curve": _equity_curve(run.gross.equity_curve),
        "net_equity_curve": _equity_curve(run.result.equity_curve),
        "run_hash": run.run_hash,
    }


def write_router_paper_run(root: Path, run: RouterPaperRun) -> WrittenReport:
    """Write at ``<root>/router_paper_run/<run_hash>.json``.

    ``run.run_hash`` already binds the router spec, state result, every routed strategy's result
    hash, the routing decisions, the switching charges and both backtest results (module docstring,
    "``RouterPaperRun.run_hash`` binds everything") — the object's own result hash. The payload's
    ``gross_equity_curve`` / ``net_equity_curve`` are display data for that already-bound pair of
    results (``gross_result_hash`` / ``result_hash``); they carry no additional identity.
    """
    return write_report_file(root, KIND, run.run_hash, _payload(run))


def _stop_payload(stop: RouterStop) -> dict[str, Any]:
    return {
        "router": stop.router,
        "router_spec_hash": stop.router_spec_hash,
        "reason": str(stop.reason),
        "detail": stop.detail,
        "lifecycle": dict(sorted(stop.lifecycle.items())),
        "state_result_hash": stop.state_result_hash,
        "strategy_result_hashes": dict(sorted(stop.strategy_result_hashes.items())),
        "validation_reports": None
        if stop.validation_reports is None
        else dict(sorted(stop.validation_reports.items())),
        "stop_hash": stop.stop_hash,
    }


def write_router_stop(root: Path, stop: RouterStop) -> WrittenReport:
    """Write at ``<root>/router_stop/<stop_hash>.json`` (``stop_hash`` binds every field)."""
    return write_report_file(root, STOP_KIND, stop.stop_hash, _stop_payload(stop))
