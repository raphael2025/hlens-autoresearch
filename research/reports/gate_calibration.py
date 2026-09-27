"""Writer for Phase 9 gate calibration reports (``research/synthetic_lab/gate_calibration.py``).

Evidence only — not a Profile decision (ADR-0042 implementation note, gate calibration harness).
The report object is taken structurally (``report_hash`` + ``to_payload()``) so this package does
not import ``research/synthetic_lab`` (which imports this writer).

``apps/api``'s ``ReportStore`` serves it as the ``gate_calibration`` kind (same
``<root>/<kind>/<id>.json`` envelope rules).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from research.reports.envelope import WrittenReport, write_report_file

__all__ = ["KIND", "CalibrationPayload", "write_gate_calibration_report"]

#: Directory name under the report root (``apps.api.store.ReportKind.GATE_CALIBRATION``).
KIND = "gate_calibration"


class CalibrationPayload(Protocol):
    @property
    def report_hash(self) -> str: ...

    def to_payload(self) -> Mapping[str, object]: ...


def write_gate_calibration_report(root: Path, report: CalibrationPayload) -> WrittenReport:
    """Write ``report`` at ``<root>/gate_calibration/<report_hash>.json`` (append-only)."""
    return write_report_file(root, KIND, report.report_hash, report.to_payload())
