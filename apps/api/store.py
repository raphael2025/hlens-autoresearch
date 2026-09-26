"""Read-only report store (ADR-0048; framework, FRAMEWORK_IMPLEMENTED / NOT_VALIDATED).

``apps/api`` never imports ``research/`` (01-system.md §3): the research plane writes its
artifacts — validation reports, research-loop round audit records, state x strategy matrices,
router paper runs and stops, gate calibration evidence, state diagnostics, event statistics — as
JSON files under a configured directory, and this module only reads them
back. The payload is served as ``payload`` inside a small envelope (``kind``, ``id``, ``created``,
``payload``, ``content_hash``); adding a new report kind never requires a contract change here.

Only the ``research_loop_round`` kind is checked against a contract (ADR-0050): its payload must be
a valid ``core.contracts.loop_audit.LoopRoundRecord`` that round-trips byte-identically, and the
file's ``id`` must be that record's ``record_hash`` (the writer names files by it). A file that
fails either check is malformed — never served as a report — so an edited or ill-formed audit
record is never served as one. Other kinds are still served opaquely.

Malformed files are **visible, not silent** (2026-09-26; CODE_COMPLETE / DEBUG_PENDING):
:meth:`ReportStore.listing` (behind ``GET /reports/{kind}``) returns the well-formed reports *and*
an ``invalid`` list naming every file it could not serve with the reason (no filesystem path), so
one bad file neither hides the others nor disappears unnoticed; ``get`` still refuses it (422).
:meth:`ReportStore.list` keeps returning only the well-formed envelopes.

Layout: ``<root>/<kind>/<id>.json``, one JSON object per file. ``id`` is the file's stem; ``kind``
is one of :class:`ReportKind`. ``created`` is the file's modification time (UTC) — the store does
not assume the payload carries its own timestamp field. ``content_hash`` is the SHA-256 of the
canonical (sorted-key, compact) JSON encoding of the payload, so two files with the same content
but different formatting hash the same.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.contracts.loop_audit import LoopRoundRecord

__all__ = [
    "InvalidReport",
    "InvalidReportId",
    "ReportEnvelope",
    "ReportListing",
    "ReportMalformed",
    "ReportKind",
    "ReportNotFound",
    "ReportStore",
]

#: ``id`` must be a plain filename stem: no path separators, no leading dot (hidden / relative).
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ReportKind(StrEnum):
    """The report kinds the console serves ((a)-(d), Phase 9 gate calibration evidence, and the
    Phase 10 router stop / Phase 2 state diagnostics / Phase 3 event statistics records)."""

    VALIDATION_REPORT = "validation_report"
    RESEARCH_LOOP_ROUND = "research_loop_round"
    STATE_STRATEGY_MATRIX = "state_strategy_matrix"
    ROUTER_PAPER_RUN = "router_paper_run"
    GATE_CALIBRATION = "gate_calibration"
    ROUTER_STOP = "router_stop"
    STATE_DIAGNOSTICS = "state_diagnostics"
    EVENT_STATISTICS = "event_statistics"


class InvalidReportId(ValueError):
    """Raised for an ``id`` that is not a safe filename stem (path traversal refusal)."""


class ReportNotFound(LookupError):
    """Raised when ``<root>/<kind>/<id>.json`` does not exist."""


class ReportMalformed(ValueError):
    """Raised when a report file is not a well-formed JSON object (or, for ``research_loop_round``,
    not a valid record named by its hash). ``reason`` is the message without the file's path."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.reason = reason


class ReportEnvelope(BaseModel):
    """The minimal envelope every report kind is served through."""

    model_config = ConfigDict(frozen=True)

    kind: ReportKind
    id: str
    created: datetime
    payload: dict[str, Any]
    content_hash: str


class InvalidReport(BaseModel):
    """A report file the store found but could not serve: its ``id`` (file stem) and why."""

    model_config = ConfigDict(frozen=True)

    id: str
    reason: str


class ReportListing(BaseModel):
    """``GET /reports/{kind}``: the well-formed reports (newest first) and every file skipped as
    malformed (by id), so a corrupt file is reported instead of silently dropped."""

    model_config = ConfigDict(frozen=True)

    kind: ReportKind
    reports: list[ReportEnvelope]
    invalid: list[InvalidReport]


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _check_loop_round(path: Path, payload: dict[str, Any]) -> None:
    """A ``research_loop_round`` file must hold a valid ``LoopRoundRecord`` named by its hash."""
    try:
        record = LoopRoundRecord.from_audit_payload(payload)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise ReportMalformed(path, f"not a valid LoopRoundRecord (ADR-0050): {exc}") from exc
    if record.record_hash != path.stem:
        raise ReportMalformed(path, "the file name is not the record's record_hash")


def _validate_id(report_id: str) -> str:
    if not _SAFE_ID.fullmatch(report_id) or ".." in report_id:
        raise InvalidReportId(f"invalid report id: {report_id!r}")
    return report_id


class ReportStore:
    """Reads report envelopes from ``<root>/<kind>/<id>.json``.

    ``root=None`` (the default via :func:`apps.api.app.create_app`) makes every ``list`` call
    return an empty list and every ``get`` call raise :class:`ReportNotFound`: a console with no
    configured report directory shows empty pages, not an error.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    def _kind_dir(self, kind: ReportKind) -> Path | None:
        if self._root is None:
            return None
        return self._root / kind.value

    def list(self, kind: ReportKind) -> list[ReportEnvelope]:
        """The well-formed reports only (see :meth:`listing` for the malformed ones)."""
        return self.listing(kind).reports

    def listing(self, kind: ReportKind) -> ReportListing:
        """Every well-formed report (newest first) plus every malformed file with its reason."""
        directory = self._kind_dir(kind)
        if directory is None or not directory.is_dir():
            return ReportListing(kind=kind, reports=[], invalid=[])
        envelopes: list[ReportEnvelope] = []
        invalid: list[InvalidReport] = []
        for path in sorted(directory.glob("*.json")):
            try:
                envelopes.append(self._read(kind, path))
            except ReportMalformed as exc:  # reported, and the rest of the listing still served
                invalid.append(InvalidReport(id=path.stem, reason=exc.reason))
        envelopes.sort(key=lambda env: (env.created, env.id), reverse=True)
        return ReportListing(kind=kind, reports=envelopes, invalid=invalid)

    def get(self, kind: ReportKind, report_id: str) -> ReportEnvelope:
        report_id = _validate_id(report_id)
        directory = self._kind_dir(kind)
        if directory is None:
            raise ReportNotFound(f"no report store configured (kind={kind.value})")
        path = (directory / f"{report_id}.json").resolve()
        if directory.resolve() not in path.parents or not path.is_file():
            raise ReportNotFound(f"{kind.value}/{report_id} not found")
        return self._read(kind, path)

    def _read(self, kind: ReportKind, path: Path) -> ReportEnvelope:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportMalformed(path, "unreadable or not well-formed JSON") from exc
        if not isinstance(payload, dict):
            raise ReportMalformed(path, "JSON root must be an object")
        if kind is ReportKind.RESEARCH_LOOP_ROUND:
            _check_loop_round(path, payload)
        created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return ReportEnvelope(
            kind=kind,
            id=path.stem,
            created=created,
            payload=payload,
            content_hash=_content_hash(payload),
        )
