"""The committed console fixtures (``apps/web/fixtures/<kind>/<id>.json``) as test inputs.

Every fixture there is what a real ``research/reports`` writer produced (``apps/web/fixtures/
README.md``; pinned by ``tests/research/reports/test_console_fixture_writers.py``), so it is a
well-formed report of its kind named by its own identity. Tests under ``tests/apps`` (which must
not import ``research/``) read them to get valid payloads to serve or to tamper with.

Each kind has one **current** fixture (contract 2.1.0). Three kinds also keep a **legacy
readable** 2.0.0 fixture (``LEGACY_2_0_0``): the files committed before 2.1.0, kept so the API and
the console keep proving they read a 2.0.0 report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from apps.api.store import ReportKind

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web" / "fixtures"

#: The legacy readable 2.0.0 fixture id of each kind that has one (``apps/web/fixtures/README.md``).
LEGACY_2_0_0: Final[dict[ReportKind, str]] = {
    ReportKind.VALIDATION_REPORT: (
        "a29b0ab7fb27edbc09cda37b01ad323c0c005f32694547ae45afd0e94333072b"
    ),
    ReportKind.STATE_STRATEGY_MATRIX: (
        "5940a5de3bde080ff156d564ce582d73d1db85623fea163b53ba925030fec21c"
    ),
    ReportKind.GATE_CALIBRATION: (
        "deaba5047218eeff080ed1e1ae588ba8e13a4f6ea3b6557fcab6ea3ffbe5fc07"
    ),
}


@dataclass(frozen=True, slots=True)
class Fixture:
    id: str
    payload: dict[str, Any]


def _load(path: Path) -> Fixture:
    return Fixture(path.stem, json.loads(path.read_text(encoding="utf-8")))


def fixtures(kind: ReportKind) -> list[Fixture]:
    """Every committed fixture of ``kind`` (current and legacy), sorted by id."""
    return [_load(path) for path in sorted((FIXTURES_ROOT / kind.value).glob("*.json"))]


def fixture(kind: ReportKind) -> Fixture:
    """The one current (2.1.0) committed fixture of ``kind`` (a fresh copy of its payload)."""
    legacy = LEGACY_2_0_0.get(kind)
    (current,) = [item for item in fixtures(kind) if item.id != legacy]
    return current


def legacy_fixture(kind: ReportKind) -> Fixture:
    """The legacy readable 2.0.0 fixture of ``kind`` (``LEGACY_2_0_0``)."""
    return _load(FIXTURES_ROOT / kind.value / f"{LEGACY_2_0_0[kind]}.json")
