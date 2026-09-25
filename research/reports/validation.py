"""Writer for Phase 4 validation reports (``research/validation/pipeline.py``'s ``build_report``).

Never imports ``research/validation`` internals beyond the public ``ValidationReport`` model it
already returns — this module only serializes an object handed to it.
"""

from __future__ import annotations

from pathlib import Path

from core.domain.research import ValidationReport
from research.reports.envelope import WrittenReport, write_report_file

__all__ = ["KIND", "write_validation_report"]

#: Directory name under the report root; matches ``apps.api.store.ReportKind.VALIDATION_REPORT``.
KIND = "validation_report"


def write_validation_report(root: Path, report: ValidationReport) -> WrittenReport:
    """Write ``report`` at ``<root>/validation_report/<content_hash>.json``.

    The id is ``report.content_hash()`` — the report's own content hash — not
    ``report.report_id``: the domain model's docstring is explicit that ``report_id`` is an
    externally assigned label, not a content identity (``core/domain/research.py``,
    ``ValidationReport``: "``report_id`` 是外部赋予的标识，**不是**结果内容身份"). Re-writing the
    exact same report is therefore always a no-op; nothing can collide two different report
    contents under the same id.
    """
    return write_report_file(root, KIND, report.content_hash(), report.model_dump(mode="json"))
