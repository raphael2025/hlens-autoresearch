"""Read-only report store (ADR-0048; framework, FRAMEWORK_IMPLEMENTED / NOT_VALIDATED).

``apps/api`` never imports ``research/`` (01-system.md §3): the research plane writes its
artifacts — validation reports, research-loop round audit records, state x strategy matrices,
router paper runs and stops, gate calibration evidence, state diagnostics, event statistics — as
JSON files under a configured directory, and this module only reads them
back. The payload is served as ``payload`` inside a small envelope (``kind``, ``id``, ``created``,
``payload``, ``content_hash``); adding a new report kind never requires a contract change here.

The ``research_loop_round`` kind is checked against a contract (ADR-0050): its payload must be
a valid ``core.contracts.loop_audit.LoopRoundRecord`` that round-trips byte-identically, and the
file's ``id`` must be that record's ``record_hash`` (the writer names files by it). A file that
fails either check is malformed — never served as a report — so an edited or ill-formed audit
record is never served as one.

Contract and identity checks of the other kinds (2026-09-26; CODE_COMPLETE / DEBUG_PENDING). Every
``research/reports`` writer names a file by the report's own content identity; the store
recomputes that identity from the payload fields (it never imports ``research/``; the rules below
restate each writer's hash with ``core.domain.base.content_hash``) and refuses a file whose name,
recorded hash and fields disagree:

| kind | contract / identity (file id must equal it) |
|---|---|
| ``validation_report`` | a ``core.domain.research.ValidationReport`` that round-trips; its hash |
| ``router_paper_run`` | ``run_hash`` = hash of the fields it binds (``paper.py`` ``_run_hash``) |
| ``router_stop`` | ``stop_hash`` = hash of ``{"kind": "router_stop", <every other field>}`` |
| ``state_diagnostics`` | id = hash of the whole payload (``diagnostics_hash``) |
| ``event_statistics`` / ``gate_calibration`` | ``report_hash`` = hash of the payload without it |

Honest boundary: display-only fields a hash does not bind (the router run's equity curves,
endpoints and per-decision ``switching_cost``) are not verified; ``state_strategy_matrix`` is still
served opaquely (its ``matrix_hash`` is not recomputable from the payload alone).

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
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from core.contracts.loop_audit import LoopRoundRecord
from core.domain.base import canonical_json, content_hash
from core.domain.research import ValidationReport

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
    """Raised when a report file is not a well-formed JSON object, or fails its kind's contract /
    identity check (module docs). ``reason`` is the message without the file's path."""

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


def _check_validation_report(path: Path, payload: dict[str, Any]) -> None:
    """A ``validation_report`` file holds a valid ``ValidationReport`` named by its content hash
    (``research/reports/validation.py`` writes ``report.model_dump(mode="json")`` under
    ``report.content_hash()``)."""
    try:
        report = ValidationReport.model_validate(payload)
        same = canonical_json(report.model_dump(mode="json")) == canonical_json(payload)
    except (TypeError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise ReportMalformed(path, f"not a valid ValidationReport: {exc}") from exc
    if not same:
        raise ReportMalformed(path, "the ValidationReport payload does not round-trip")
    if report.content_hash() != path.stem:
        raise ReportMalformed(path, "the file name is not the report's content hash")


def _hash_of(path: Path, body: Any, what: str) -> str:
    try:
        return content_hash(body)
    except (TypeError, ValueError) as exc:  # e.g. a NaN literal: not canonical JSON
        raise ReportMalformed(path, f"the fields bound by {what} are not canonical JSON") from exc


def _require_identity(path: Path, payload: dict[str, Any], field: str, expected: str) -> None:
    """The recorded ``field`` must be ``expected`` (recomputed) and the file must be named by it."""
    if payload.get(field) != expected:
        raise ReportMalformed(path, f"{field} does not match the payload it binds")
    if path.stem != expected:
        raise ReportMalformed(path, f"the file name is not the report's {field}")


def _self_hashed(field: str) -> Callable[[Path, dict[str, Any]], None]:
    """``field`` is the content hash of the payload without it (and the file is named by it)."""

    def check(path: Path, payload: dict[str, Any]) -> None:
        body = {key: value for key, value in payload.items() if key != field}
        _require_identity(path, payload, field, _hash_of(path, body, field))

    return check


def _check_state_diagnostics(path: Path, payload: dict[str, Any]) -> None:
    """``diagnostics_hash`` is the content hash of the whole payload; the file is named by it."""
    if path.stem != _hash_of(path, payload, "diagnostics_hash"):
        raise ReportMalformed(path, "the file name is not the report's diagnostics_hash")


#: The routing decision fields ``run_hash`` binds (``switching_cost`` is display-only).
_DECISION_FIELDS: Final = ("at", "state", "weights", "turnover")
_RUN_FIELDS: Final = (
    "router",
    "router_spec_hash",
    "state_result_hash",
    "strategy_result_hashes",
    "request_hash",
    "gross_result_hash",
    "charges",
    "result_hash",
)
_STOP_FIELDS: Final = (
    "router",
    "router_spec_hash",
    "reason",
    "detail",
    "lifecycle",
    "state_result_hash",
    "strategy_result_hashes",
    "validation_reports",
)
#: Keys only present when supplied / in evidence mode (absent: not hashed, as the writer does).
_OPTIONAL_RUN_FIELDS: Final = ("validation_reports", "eligibility")


def _check_router_paper_run(path: Path, payload: dict[str, Any]) -> None:
    """``run_hash`` restated from ``research/router/paper.py`` ``_run_hash`` over the payload."""
    try:
        body: dict[str, Any] = {key: payload[key] for key in _RUN_FIELDS}
        body["decisions"] = [
            {key: decision[key] for key in _DECISION_FIELDS} for decision in payload["decisions"]
        ]
    except (KeyError, TypeError) as exc:
        raise ReportMalformed(path, f"lacks a field bound by run_hash: {exc}") from exc
    for key in _OPTIONAL_RUN_FIELDS:
        if key in payload:
            body[key] = payload[key]
    _require_identity(path, payload, "run_hash", _hash_of(path, body, "run_hash"))


def _check_router_stop(path: Path, payload: dict[str, Any]) -> None:
    """``stop_hash`` restated from ``research/router/paper.py`` ``_stop_record``."""
    try:
        body: dict[str, Any] = {"kind": "router_stop"} | {key: payload[key] for key in _STOP_FIELDS}
    except KeyError as exc:
        raise ReportMalformed(path, f"lacks a field bound by stop_hash: {exc}") from exc
    if "eligibility" in payload:  # evidence mode only
        body["eligibility"] = payload["eligibility"]
    _require_identity(path, payload, "stop_hash", _hash_of(path, body, "stop_hash"))


def _validate_id(report_id: str) -> str:
    if not _SAFE_ID.fullmatch(report_id) or ".." in report_id:
        raise InvalidReportId(f"invalid report id: {report_id!r}")
    return report_id


#: The contract / identity check of every kind that has one (module docs).
_CHECKS: Final[dict[ReportKind, Callable[[Path, dict[str, Any]], None]]] = {
    ReportKind.VALIDATION_REPORT: _check_validation_report,
    ReportKind.RESEARCH_LOOP_ROUND: _check_loop_round,
    ReportKind.ROUTER_PAPER_RUN: _check_router_paper_run,
    ReportKind.ROUTER_STOP: _check_router_stop,
    ReportKind.STATE_DIAGNOSTICS: _check_state_diagnostics,
    ReportKind.EVENT_STATISTICS: _self_hashed("report_hash"),
    ReportKind.GATE_CALIBRATION: _self_hashed("report_hash"),
}


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
        check = _CHECKS.get(kind)
        if check is not None:
            check(path, payload)
        created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return ReportEnvelope(
            kind=kind,
            id=path.stem,
            created=created,
            payload=payload,
            content_hash=_content_hash(payload),
        )
