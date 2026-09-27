"""Writer for Phase 2 state stability diagnostics (``research/states/diagnostics.py``).

``StateDiagnostics.to_payload()`` is already the deterministic, JSON-ready form and
``diagnostics_hash`` its content hash, so the report id is that hash. Before anything is written
the payload is read back through ``StateDiagnostics.from_payload(..., expected_hash=...)`` after a
real JSON encode / decode: a report whose payload is not exactly the canonical form of what it
encodes (e.g. per-state maps that do not match its ``state_space``) is refused (``ValueError``)
and nothing is written.

``apps/api``'s ``ReportStore`` serves it as the ``state_diagnostics`` kind (opaquely, same
``<root>/<kind>/<id>.json`` envelope rules as the other kinds). Descriptive only: the report holds
no threshold deciding whether a state is "good" (``research/states/diagnostics.py`` module docs).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.domain.base import canonical_json
from research.reports.envelope import WrittenReport, write_report_file
from research.states.diagnostics import StateDiagnostics

__all__ = ["KIND", "write_state_diagnostics"]

#: Directory name under the report root (``apps.api.store.ReportKind.STATE_DIAGNOSTICS``); equal
#: to ``research.states.diagnostics.PAYLOAD_KIND`` (the payload's own ``kind``).
KIND = "state_diagnostics"


def write_state_diagnostics(root: Path, diagnostics: StateDiagnostics) -> WrittenReport:
    """Write ``diagnostics`` at ``<root>/state_diagnostics/<diagnostics_hash>.json``."""
    payload = diagnostics.to_payload()
    if payload.get("kind") != KIND:
        raise ValueError(f"not a {KIND} payload: kind={payload.get('kind')!r}")
    report_id = diagnostics.diagnostics_hash
    wire = json.loads(canonical_json(payload))  # exactly what ReportStore will read back
    try:
        StateDiagnostics.from_payload(wire, expected_hash=report_id)
    except ValueError as exc:
        raise ValueError(f"the state diagnostics do not round-trip; not written: {exc}") from exc
    return write_report_file(root, KIND, report_id, payload)
