"""Deterministic, append-only writers feeding apps/api's read-only research console (ADR-0048).

``apps/api`` never imports ``research/`` (01-system.md §3): the research plane writes its
artifacts as JSON files under a report root, and ``apps.api.store.ReportStore`` only reads them
back, opaquely. This package is the writing half — one function per report kind — that produces
files the store accepts. See ``research/reports/README.md`` for the envelope / id / conflict
rules shared by all of them.
"""

from __future__ import annotations

from research.reports.envelope import ReportConflict, WrittenReport, write_report_file
from research.reports.gate_calibration import write_gate_calibration_report
from research.reports.loop import write_research_loop_round, write_research_loop_rounds
from research.reports.matrix import write_state_strategy_matrix
from research.reports.router import write_router_paper_run, write_router_stop
from research.reports.validation import write_validation_report

__all__ = [
    "ReportConflict",
    "WrittenReport",
    "write_gate_calibration_report",
    "write_report_file",
    "write_research_loop_round",
    "write_research_loop_rounds",
    "write_router_paper_run",
    "write_router_stop",
    "write_state_strategy_matrix",
    "write_validation_report",
]
