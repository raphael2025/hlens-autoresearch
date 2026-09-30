"""The committed console fixtures (``apps/web/fixtures/<kind>/<id>.json``) as test inputs.

Every fixture there is what a real ``research/reports`` writer produced (``apps/web/fixtures/
README.md``; pinned by ``tests/research/reports/test_console_fixture_writers.py``), so it is a
well-formed report of its kind named by its own identity. Tests under ``tests/apps`` (which must
not import ``research/``) read them to get valid payloads to serve or to tamper with.

Each kind has one latest committed fixture (contract 2.5.0). Kinds whose report changed with a
contract bump also keep **legacy readable** fixtures: the files committed before 2.1.0
(``LEGACY_2_0_0``, three kinds), before 2.2.0 (``LEGACY_2_1_0``, ADR-0055, six kinds), before
2.4.0 (``LEGACY_2_2_0``, ADR-0088, six kinds), and before 2.5.0 (``LEGACY_2_4_0``, ADR-0094,
six kinds), kept so the API and console prove they read earlier reports. A kind may also have named
**variant** fixtures (``VARIANTS``): further current reports of a distinct state the console must
show, e.g. an insufficient-evidence degradation check.
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

#: Named variant fixture ids per kind (``apps/web/fixtures/README.md``): current reports of a
#: distinct state, next to the kind's one current fixture.
VARIANTS: Final[dict[ReportKind, dict[str, str]]] = {
    ReportKind.DEGRADATION_CHECK: {
        # every ruled metric missing: ``"insufficient_evidence": true`` (never healthy)
        "insufficient_evidence": (
            "50f536888a5b49fec15e11737bd107101d909617449b16881de60d5139f37d9d"
        ),
    },
}


#: The legacy readable 2.1.0 fixture id of each kind whose report the 2.2.0 bump changed (ADR-0055).
LEGACY_2_1_0: Final[dict[ReportKind, str]] = {
    ReportKind.VALIDATION_REPORT: (
        "da3950c41dc7c6548ace7fdec79ce58b93e0646ed5dd92bf7a4e58879adf4032"
    ),
    ReportKind.STATE_STRATEGY_MATRIX: (
        "89f28e4a5d44e45a02ac3bf6d81716dd179b939cc8985edb94950aea5107b11a"
    ),
    ReportKind.ROUTER_PAPER_RUN: (
        "7f30d3d4c10238b5d5f9f4c9138b5518e633d5e7d3ba448a9aa6695a9179d8d2"
    ),
    ReportKind.GATE_CALIBRATION: (
        "c5147ea3fa460274b30f0c67a17df62788de1f213e4ba0efd108806823543c1e"
    ),
    ReportKind.ROUTER_STOP: "64c340616747be0377f0b48e4d4baeecbf1f72d41547bc7961fe24b2478ed4d9",
    ReportKind.PAPER_DEVIATION: (
        "45d3d4383bfd73fff69b46f2231029b008cd1a0805dc93521e8f96448ffc3c30"
    ),
}

#: The legacy readable 2.2.0 fixture ids retained when ADR-0088 advanced the current contract.
LEGACY_2_2_0: Final[dict[ReportKind, str]] = {
    ReportKind.VALIDATION_REPORT: (
        "b0896bbe070ce8cb929052108d67dafb91a1db3527791fc531cd0d80194e9cfa"
    ),
    ReportKind.STATE_STRATEGY_MATRIX: (
        "2e201246181bcfa53dd859cfff9a866ac54354c6db62678803d9685edab7ad93"
    ),
    ReportKind.ROUTER_PAPER_RUN: "6c1231639a4312f4a6c7cccba16d92b519f7ad4caa9d6b22e56c94ba74291c25",
    ReportKind.GATE_CALIBRATION: "8499223e39d1c260e76e2cb18b6af4c227914b140ffcf6089771accd3b16b535",
    ReportKind.ROUTER_STOP: "bff151b450dd125758410b9ed78e352030d36a27cc58a95f5d2ed8216b630382",
    ReportKind.PAPER_DEVIATION: "b9c4246c6492f47b8da5c289765009973ba3bff15b2fcfb7ddf3ca630602c442",
}

#: The legacy readable 2.4.0 fixture ids retained when ADR-0094 advanced the current contract.
LEGACY_2_4_0: Final[dict[ReportKind, str]] = {
    ReportKind.VALIDATION_REPORT: (
        "95a943f3e601530713c303525d0e0db45a4af4cca7b8321f65a2fbba8847de3d"
    ),
    ReportKind.STATE_STRATEGY_MATRIX: (
        "e0ba16aed30bdb8559fc7b5e4c58f64de6da1c39d0bb9e3e593a2fd79cae8cd6"
    ),
    ReportKind.ROUTER_PAPER_RUN: "3042183e2e74b2249ab7eebe751979bbd677a7bd6a687be4659e60c19d37f681",
    ReportKind.GATE_CALIBRATION: "67d00214a3a68d54ad9de14230b098da6e425fcbb384c34db54b475d2cb8b80d",
    ReportKind.ROUTER_STOP: "b50a6aeb40bc0414eb9082639a6d5945dbabada2c728cb4d356e0577383541fc",
    ReportKind.PAPER_DEVIATION: "d5681d611e79d3a262c2ce91798f8885272d91bfdd3221550ac0a9a3ecfc43d9",
}

# ADR-0081 requires both original descriptive 1.0.0 paper-deviation reports to remain readable.
# The generation registry pins one report per contract generation; these ids retain both
# original descriptive 1.0.0 reports alongside those generation pins.
LEGACY_ADR_0081: Final[dict[ReportKind, tuple[str, ...]]] = {
    ReportKind.PAPER_DEVIATION: (
        "183c62b9c2949b54793b1f164ddb4ba66325bb50d7d04173065cc230d771c160",
        "a168f4f764b698ec9c7f46035a2d62f4b857d8d543855b732dabb65ac9d456a1",
    ),
    ReportKind.RETRO_AUDIT: (
        "05545674ea02dcb48a07e70ed591999a38c02cf6785623d2aa784076d38249e2",
    ),
}

#: Every legacy generation, by the contract version whose code wrote it.
LEGACY: Final[dict[str, dict[ReportKind, str]]] = {
    "2.0.0": LEGACY_2_0_0,
    "2.1.0": LEGACY_2_1_0,
    "2.2.0": LEGACY_2_2_0,
    "2.4.0": LEGACY_2_4_0,
}


def legacy_ids(kind: ReportKind) -> set[str]:
    """The ids of every legacy readable fixture of ``kind`` (any generation)."""
    generation_ids = {ids[kind] for ids in LEGACY.values() if kind in ids}
    return generation_ids | set(LEGACY_ADR_0081.get(kind, ()))


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
    """The latest committed (current-version) fixture of ``kind`` (a fresh copy); not a
    legacy or variant one."""
    pinned = {*legacy_ids(kind), *VARIANTS.get(kind, {}).values()}
    (current,) = [item for item in fixtures(kind) if item.id not in pinned]
    return current


def legacy_fixture(kind: ReportKind, version: str = "2.0.0") -> Fixture:
    """The legacy readable fixture of ``kind`` written by the ``version`` code (``LEGACY``)."""
    return _load(FIXTURES_ROOT / kind.value / f"{LEGACY[version][kind]}.json")


def variant_fixture(kind: ReportKind, name: str) -> Fixture:
    """The variant fixture ``name`` of ``kind`` (``VARIANTS``)."""
    return _load(FIXTURES_ROOT / kind.value / f"{VARIANTS[kind][name]}.json")
