"""Read-only report store (ADR-0048; framework, FRAMEWORK_IMPLEMENTED / NOT_VALIDATED).

``apps/api`` never imports ``research/`` (01-system.md §3): the research plane writes its
artifacts — validation reports, research-loop round audit records, state x strategy matrices,
router paper runs — as JSON files under a configured directory, and this module only reads them
back. Nothing here interprets the payload's internal shape; it is served opaquely as ``payload``
inside a small envelope (``kind``, ``id``, ``created``, ``payload``, ``content_hash``), so adding a
new report kind never requires a contract change here.

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

__all__ = [
    "InvalidReportId",
    "ReportEnvelope",
    "ReportKind",
    "ReportNotFound",
    "ReportStore",
]

#: ``id`` must be a plain filename stem: no path separators, no leading dot (hidden / relative).
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ReportKind(StrEnum):
    """The report kinds the console serves ((a)-(d) plus Phase 9 gate calibration evidence)."""

    VALIDATION_REPORT = "validation_report"
    RESEARCH_LOOP_ROUND = "research_loop_round"
    STATE_STRATEGY_MATRIX = "state_strategy_matrix"
    ROUTER_PAPER_RUN = "router_paper_run"
    GATE_CALIBRATION = "gate_calibration"


class InvalidReportId(ValueError):
    """Raised for an ``id`` that is not a safe filename stem (path traversal refusal)."""


class ReportNotFound(LookupError):
    """Raised when ``<root>/<kind>/<id>.json`` does not exist."""


class ReportMalformed(ValueError):
    """Raised when a report file is not a well-formed JSON object."""


class ReportEnvelope(BaseModel):
    """The minimal envelope every report kind is served through."""

    model_config = ConfigDict(frozen=True)

    kind: ReportKind
    id: str
    created: datetime
    payload: dict[str, Any]
    content_hash: str


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


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
        directory = self._kind_dir(kind)
        if directory is None or not directory.is_dir():
            return []
        envelopes: list[ReportEnvelope] = []
        for path in sorted(directory.glob("*.json")):
            try:
                envelopes.append(self._read(kind, path))
            except ReportMalformed:
                continue  # a corrupt file does not break the whole listing
        envelopes.sort(key=lambda env: (env.created, env.id), reverse=True)
        return envelopes

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
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportMalformed(f"{path}: not well-formed JSON") from exc
        if not isinstance(payload, dict):
            raise ReportMalformed(f"{path}: JSON root must be an object")
        created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return ReportEnvelope(
            kind=kind,
            id=path.stem,
            created=created,
            payload=payload,
            content_hash=_content_hash(payload),
        )
