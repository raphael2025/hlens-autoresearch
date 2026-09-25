"""Deterministic, append-only report files shared by every report kind (ADR-0048 report writer).

``apps/api/store.py``'s ``ReportStore`` reads ``<root>/<kind>/<id>.json`` — one JSON object per
file — and builds its own envelope (``kind``, ``id``, ``created``, ``payload``, ``content_hash``)
purely from what it reads back: ``created`` is the file's mtime and ``content_hash`` is recomputed
from the parsed payload every time. The writer therefore never has to reproduce that envelope or
match the store's hash byte-for-byte; it only owns the other half of the contract:

- the file is valid JSON, written deterministically (sorted keys; no float/NaN slipping through;
  every value is already a JSON primitive — ``Decimal`` as ``str``, datetimes as UTC ISO-8601 —
  before it reaches this module, exactly as ``core.domain.base.canonical_json`` requires);
- **append-only**: an existing ``<kind>/<id>.json`` is never silently replaced with different
  content. Writing the same content again is a no-op; writing different content under the same id
  is refused (:class:`ReportConflict`).

Canonical JSON rule: this module reuses ``core.domain.base.canonical_json`` (sorted keys, compact
separators) rather than duplicating ``apps/api/store.py``'s private ``_canonical_json``. Two
reasons: (1) that helper is already the project's one documented canonical-JSON rule
(docs/architecture/02-domain.md §3), used to compute every ``Contract.content_hash()`` — reusing it
keeps "canonical JSON" a single definition instead of two slightly different ones (apps/api's
encoder additionally sets ``ensure_ascii=True``, a purely cosmetic difference that would otherwise
be a second, competing "canonical" form); (2) ``apps/api/store``'s encoder is a private,
underscore-prefixed module function, not part of its public surface — importing it would reach
into another Plane's internals for no benefit, since the store recomputes its own
``content_hash`` from the parsed payload on every read regardless of on-disk formatting. See
``docs/adr/0048-api-and-web-console.md`` (Implementation note, report writer) for the full
decision record.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.domain.base import canonical_json

__all__ = ["ReportConflict", "WrittenReport", "write_report_file"]

#: Matches apps/api/store.py's ``_SAFE_ID`` (plain filename stem, no path separators / dot-hidden).
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ReportConflict(ValueError):
    """Refused: ``<kind>/<id>.json`` already exists with different content (append-only)."""


@dataclass(frozen=True, slots=True)
class WrittenReport:
    """What one write produced."""

    kind: str
    id: str
    path: Path
    #: ``False`` when an identical file already existed (idempotent no-op), ``True`` when this
    #: call actually created the file.
    written: bool


def _validate_id(report_id: str) -> str:
    if not _SAFE_ID.fullmatch(report_id) or ".." in report_id:
        raise ValueError(f"invalid report id: {report_id!r}")
    return report_id


def write_report_file(
    root: Path, kind: str, report_id: str, payload: Mapping[str, Any]
) -> WrittenReport:
    """Write ``payload`` at ``<root>/<kind>/<report_id>.json`` (append-only; see module docs).

    ``payload`` must already be a plain JSON-safe mapping (the kind-specific writers in this
    package build it from the research-plane object they wrap: ``Decimal`` -> ``str``, datetimes
    -> UTC ISO-8601, enums -> their value). Writing is atomic (write to a sibling temp file, then
    ``os.replace``) so a crash mid-write never leaves a half-written or corrupt report file.
    """
    _validate_id(report_id)
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a JSON object (a mapping)")
    directory = root / kind
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report_id}.json"
    text = canonical_json(dict(payload))
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == json.loads(text):
            return WrittenReport(kind, report_id, path, written=False)
        raise ReportConflict(
            f"{kind}/{report_id} already exists with different content (append-only refusal)"
        )
    tmp = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return WrittenReport(kind, report_id, path, written=True)
