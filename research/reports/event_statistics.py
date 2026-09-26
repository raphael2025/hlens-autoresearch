"""Writer for Phase 3 event statistics reports (``research/events/stats.py``).

``EventStatsReport.to_payload()`` is the deterministic, JSON-ready form (its body plus
``report_hash``, the content hash of that body), so the report id is ``report_hash``. Before
anything is written the writer recomputes the hash from the payload body: a report whose recorded
``report_hash`` does not match its own statistics / source run hashes is refused (``ValueError``)
and nothing is written.

``apps/api``'s ``ReportStore`` serves it as the ``event_statistics`` kind (opaquely, same
``<root>/<kind>/<id>.json`` envelope rules as the other kinds). Descriptive only: no validation
threshold, not a Validation Profile input (``research/events/stats.py`` module docs).
"""

from __future__ import annotations

from pathlib import Path

from core.domain.base import content_hash
from research.events.stats import EventStatsReport
from research.reports.envelope import WrittenReport, write_report_file

__all__ = ["KIND", "write_event_statistics"]

#: Directory name under the report root (``apps.api.store.ReportKind.EVENT_STATISTICS``); equal
#: to the payload's own ``kind``.
KIND = "event_statistics"


def write_event_statistics(root: Path, report: EventStatsReport) -> WrittenReport:
    """Write ``report`` at ``<root>/event_statistics/<report_hash>.json``."""
    payload = report.to_payload()
    if payload.get("kind") != KIND:
        raise ValueError(f"not an {KIND} payload: kind={payload.get('kind')!r}")
    body = {key: value for key, value in payload.items() if key != "report_hash"}
    if payload.get("report_hash") != report.report_hash or content_hash(body) != report.report_hash:
        raise ValueError("the event statistics report_hash does not match its payload; not written")
    return write_report_file(root, KIND, report.report_hash, payload)
