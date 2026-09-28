"""Deterministic, append-only writers feeding apps/api's read-only research console (ADR-0048).

``apps/api`` never imports ``research/`` (01-system.md §3): the research plane writes its
artifacts as JSON files under a report root, and ``apps.api.store.ReportStore`` only reads them
back and validates each registered payload DTO. This package is the writing half — one function per
report kind — that produces files the store accepts. See ``research/reports/README.md`` for the
envelope / id / conflict rules shared by all of them.
"""

from __future__ import annotations

from research.reports.degradation import write_degradation_check, write_degradation_operation
from research.reports.deviation import write_paper_deviation
from research.reports.envelope import ReportConflict, WrittenReport, write_report_file
from research.reports.event_statistics import write_event_statistics
from research.reports.gate_calibration import write_gate_calibration_report
from research.reports.loop import write_research_loop_round, write_research_loop_rounds
from research.reports.matrix import write_state_strategy_matrix
from research.reports.retro_audit import write_retro_audit_report
from research.reports.router import write_router_paper_run, write_router_stop
from research.reports.state_diagnostics import write_state_diagnostics
from research.reports.validation import write_validation_report

__all__ = [
    "ReportConflict",
    "WrittenReport",
    "write_degradation_check",
    "write_degradation_operation",
    "write_event_statistics",
    "write_gate_calibration_report",
    "write_paper_deviation",
    "write_report_file",
    "write_research_loop_round",
    "write_research_loop_rounds",
    "write_router_paper_run",
    "write_router_stop",
    "write_retro_audit_report",
    "write_state_diagnostics",
    "write_state_strategy_matrix",
    "write_validation_report",
]
