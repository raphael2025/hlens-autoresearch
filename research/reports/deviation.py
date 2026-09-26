"""Writer for Phase 10 paper deviation reports (``research/router/deviation.py``).

``PaperDeviation.to_payload()`` is the deterministic, JSON-ready form (its body plus
``deviation_hash``, the content hash of that body), so the report id is ``deviation_hash``. Before
anything is written the writer recomputes the hash from the payload body: a report whose recorded
``deviation_hash`` does not match its own fields is refused (``ValueError``) and nothing is written.

``apps/api``'s ``ReportStore`` serves it as the ``paper_deviation`` kind (same
``<root>/<kind>/<id>.json`` envelope rules; the store recomputes ``deviation_hash`` too).
Descriptive only: no threshold, no verdict (``research/router/deviation.py`` module docs).
"""

from __future__ import annotations

from pathlib import Path

from core.domain.base import content_hash
from research.reports.envelope import WrittenReport, write_report_file
from research.router.deviation import PAYLOAD_KIND, PaperDeviation

__all__ = ["KIND", "write_paper_deviation"]

#: Directory name under the report root (``apps.api.store.ReportKind.PAPER_DEVIATION``); equal to
#: the payload's own ``kind``.
KIND = PAYLOAD_KIND


def write_paper_deviation(root: Path, deviation: PaperDeviation) -> WrittenReport:
    """Write ``deviation`` at ``<root>/paper_deviation/<deviation_hash>.json``."""
    if not isinstance(deviation, PaperDeviation):
        raise ValueError("write_paper_deviation needs a PaperDeviation")
    payload = deviation.to_payload()
    body = {key: value for key, value in payload.items() if key != "deviation_hash"}
    if payload.get("deviation_hash") != deviation.deviation_hash or (
        content_hash(body) != deviation.deviation_hash
    ):
        raise ValueError("the paper deviation hash does not match its payload; not written")
    return write_report_file(root, KIND, deviation.deviation_hash, payload)
