"""Writer for explicit Phase 8 retro-audit reports (ADR-0041).

The caller supplies an already-computed ``RetroAuditReport``. This module only serializes and
persists that result; it does not discover lifecycle records, rerun validation, or perform any
lifecycle transition. The payload is content-addressed by ``report_hash`` and written through the
shared append-only report writer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from core.domain.base import content_hash
from research.reports.envelope import WrittenReport, write_report_file
from research.validation.retro_audit import RetroAuditReport

__all__ = ["KIND", "retro_audit_payload", "write_retro_audit_report"]

KIND: Final = "retro_audit"
SCHEMA_VERSION: Final = "1.0.0"
STATUS: Final = "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"


def retro_audit_payload(report: RetroAuditReport) -> dict[str, Any]:
    """Return a stable JSON-ready payload for one explicit audit result."""
    if not isinstance(report, RetroAuditReport):
        raise ValueError("a retro audit report needs an explicit RetroAuditReport")
    body = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        **report.to_dict(),
    }
    return {**body, "report_hash": content_hash(body)}


def write_retro_audit_report(root: Path, report: RetroAuditReport) -> WrittenReport:
    """Append one explicit audit at ``<root>/retro_audit/<report_hash>.json``."""
    payload = retro_audit_payload(report)
    return write_report_file(root, KIND, payload["report_hash"], payload)
