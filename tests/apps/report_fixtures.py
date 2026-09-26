"""The committed console fixtures (``apps/web/fixtures/<kind>/<id>.json``) as test inputs.

Every fixture there is what a real ``research/reports`` writer produced (``apps/web/fixtures/
README.md``), so it is a well-formed report of its kind named by its own identity. Tests under
``tests/apps`` (which must not import ``research/``) read them to get valid payloads to serve or
to tamper with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.api.store import ReportKind

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web" / "fixtures"


@dataclass(frozen=True, slots=True)
class Fixture:
    id: str
    payload: dict[str, Any]


def fixture(kind: ReportKind) -> Fixture:
    """The one committed fixture of ``kind`` (a fresh copy of its payload)."""
    (path,) = sorted((FIXTURES_ROOT / kind.value).glob("*.json"))
    return Fixture(path.stem, json.loads(path.read_text(encoding="utf-8")))
