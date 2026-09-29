"""Every file under ``apps/web/fixtures/`` is exactly what the real ``research/reports`` writers
produce for small, existing test objects — for every console report kind.

The committed files are never hand-written: this module builds the objects from the existing
research test fixtures, writes them with the real writers into a temporary report root, and
requires each committed ``<kind>/`` directory to hold byte-identical files — and nothing else.

Contract-bound reports retain the 2.0.0, 2.1.0, 2.2.0, and 2.4.0 generations (ADR-0052 §4,
ADR-0055, ADR-0088, ADR-0094):

- ``WRITERS`` — the current fixture of every kind; contract-bound reports use 2.5.0, while
  ``retro_audit`` uses its report schema version.
  The ``validation_report`` one carries an exact gate (``value_exact`` / ``threshold_exact``,
  TEST ONLY values) next to the float-only gate, so the console's exact display is exercised.
- ``LEGACY_WRITERS["2.0.0"]`` — the **legacy readable** 2.0.0 fixtures of ``validation_report``,
  ``state_strategy_matrix`` and ``gate_calibration``: the same builders, run by a fresh
  interpreter that imports them (so their modules' constants are built too) inside
  ``contract_schema_version_scope("2.0.0")`` (:func:`regenerate_legacy`), which reproduces the
  files committed before 2.1.0 byte for byte.
- ``LEGACY_WRITERS["2.1.0"]`` — the **legacy readable** 2.1.0 fixtures of the six kinds whose
  report the 2.2.0 bump changed (``validation_report``, ``state_strategy_matrix``,
  ``router_paper_run``, ``gate_calibration``, ``router_stop``, ``paper_deviation``): the current
  builders in a fresh interpreter inside ``contract_schema_version_scope("2.1.0")``, which
  reproduces the files committed before 2.2.0 byte for byte.
- ``LEGACY_WRITERS["2.2.0"]`` — the **legacy readable** 2.2.0 fixtures of those same six kinds,
  produced inside ``contract_schema_version_scope("2.2.0")`` and retained as a prior generation.
- ``LEGACY_WRITERS["2.4.0"]`` — the **legacy readable** 2.4.0 fixtures of those same six kinds,
  produced inside ``contract_schema_version_scope("2.4.0")`` and retained after ADR-0094.
- ``VARIANT_WRITERS`` — named further current fixtures of a kind, for a state the console must
  show distinctly (the ``degradation_check`` with every metric missing:
  ``insufficient_evidence``); their ids are pinned in ``tests/apps/report_fixtures.py``
  (``VARIANTS``).

The legacy files stay so the API and the console keep proving they read reports written by the
earlier code; their ids are pinned in ``tests/apps/report_fixtures.py`` (``LEGACY`` and the
ADR-0081 immutable descriptive fixtures). Kinds without a legacy file of a generation serialize
nothing versioned: the earlier code wrote the same bytes.

Regenerate after a payload change (delete the stale ``<kind>/`` file first; writers are
append-only)::

    uv run python -m tests.research.reports.test_console_fixture_writers

(``apps/web/fixtures/README.md``; ``tests/apps/test_console_fixtures.py`` separately checks that
every ``ReportKind`` has a fixture that loads through ``apps.api``.)
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from apps.api.store import ReportKind
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    content_hash,
    scoped_contract_schema_version,
)
from core.domain.research import GateResult, ValidationReport, Verdict
from research.events.stats import EventStatsReport
from research.reports import (
    WrittenReport,
    write_event_statistics,
    write_gate_calibration_report,
    write_paper_deviation,
    write_research_loop_round,
    write_retro_audit_report,
    write_router_paper_run,
    write_router_stop,
    write_state_diagnostics,
    write_state_strategy_matrix,
    write_validation_report,
)
from research.router.deviation import PaperDeviation
from research.router.paper import RouterStop
from research.states.diagnostics import StateDiagnostics, diagnose
from research.synthetic_lab.gate_calibration import run_gate_calibration
from research.validation.retro_audit import RetroAuditReport, retro_audit
from tests.apps.report_fixtures import LEGACY, VARIANTS, legacy_ids
from tests.research.events.test_event_stats import _all_statistics
from tests.research.reports.test_degradation_writer import write_fixture as write_degradation
from tests.research.reports.test_degradation_writer import (
    write_insufficient_evidence_fixture as write_degradation_insufficient_evidence,
)
from tests.research.reports.test_writers import (
    _loop_record,
    _matrix_with_backtest,
    _validation_report,
)
from tests.research.router.test_paper import LIFECYCLE
from tests.research.router.test_paper import _run as router_paper_run
from tests.research.router.test_paper_deviation import deviation
from tests.research.router.test_router_completion import FLAT, _or_stop
from tests.research.states.test_state_diagnostics import SERIES, SPACE
from tests.research.synthetic_lab.test_gate_calibration import _toy_setup

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_ROOT = REPO_ROOT / "apps" / "web" / "fixtures"
_MODULE = "tests.research.reports.test_console_fixture_writers"


Writer = Callable[[Path], WrittenReport]


def router_stop() -> RouterStop:
    """Every route of the spec is flat -> the router stops (``all_routes_flat``)."""
    stop = _or_stop(FLAT, LIFECYCLE)
    assert isinstance(stop, RouterStop)
    return stop


def state_diagnostics() -> StateDiagnostics:
    """The hand-checked two-state series of ``tests/research/states/test_state_diagnostics.py``."""
    return diagnose(SERIES, SPACE, min_run=2)


def event_statistics_report() -> EventStatsReport:
    """All four statistics of ``tests/research/events/test_event_stats.py`` over two runs."""
    runs = (content_hash({"run": 1}), content_hash({"run": 2}))
    return EventStatsReport(runs, _all_statistics())  # type: ignore[arg-type]


def paper_deviation() -> PaperDeviation:
    """``tests/research/router/test_paper.py``'s run vs strategy A alone (the reference)."""
    return deviation()


def retro_audit_report() -> RetroAuditReport:
    """An empty, fixed-time audit; it has no subjects and performs no lifecycle transitions."""
    return retro_audit(
        (),
        audited_at=datetime(2026, 9, 27, tzinfo=UTC),
        rules="fixture-only; no lifecycle transition",
    )


#: TEST ONLY exact gate values (ADR-0052 §1): the exact value has more digits than its float
#: (``0.03``), so a console that shows the float instead of ``value_exact`` is visibly wrong.
EXACT_VALUE = "0.0300000000000000001"
EXACT_THRESHOLD = "0.05"


def exact_gate() -> GateResult:
    """A gate with an exact value and threshold (2.1.0 content; the floats derive from them)."""
    return GateResult(
        gate_id="G3.adjusted_p_value",
        metric="p[at_most]",
        value=float(Decimal(EXACT_VALUE)),
        threshold=float(Decimal(EXACT_THRESHOLD)),
        threshold_source="significance.multiple_testing_threshold_exact",
        verdict=Verdict.PASS,
        value_exact=Decimal(EXACT_VALUE),
        threshold_exact=Decimal(EXACT_THRESHOLD),
    )


def validation_report() -> ValidationReport:
    """``tests/research/reports/test_writers.py``'s report plus :func:`exact_gate` (both PASS)."""
    base = _validation_report()
    return ValidationReport.model_validate(
        {**base.model_dump(), "gates": [*base.gates, exact_gate()]}
    )


WRITERS: dict[str, Writer] = {
    "validation_report": lambda root: write_validation_report(root, validation_report()),
    "research_loop_round": lambda root: write_research_loop_round(root, _loop_record()),
    "state_strategy_matrix": lambda root: write_state_strategy_matrix(
        root, _matrix_with_backtest()
    ),
    "router_paper_run": lambda root: write_router_paper_run(root, router_paper_run()),
    # the toy, Profile-reading detector (TEST ONLY), not the full G0 -> G4 pipeline
    "gate_calibration": lambda root: write_gate_calibration_report(
        root, run_gate_calibration(_toy_setup())
    ),
    "router_stop": lambda root: write_router_stop(root, router_stop()),
    "state_diagnostics": lambda root: write_state_diagnostics(root, state_diagnostics()),
    "event_statistics": lambda root: write_event_statistics(root, event_statistics_report()),
    "paper_deviation": lambda root: write_paper_deviation(root, paper_deviation()),
    # TEST ONLY thresholds / metrics (tests/research/reports/test_degradation_writer.py)
    "degradation_check": write_degradation,
    "retro_audit": lambda root: write_retro_audit_report(root, retro_audit_report()),
}


#: Named variant fixtures: further current reports of a kind in a distinct state.
VARIANT_WRITERS: dict[str, dict[str, Writer]] = {
    # the same TEST ONLY monitor with no recent value at all: every metric missing
    "degradation_check": {"insufficient_evidence": write_degradation_insufficient_evidence},
}


def _variant_writers() -> list[tuple[str, str, Writer]]:
    return [
        (kind, name, write)
        for kind, named in VARIANT_WRITERS.items()
        for name, write in named.items()
    ]


#: The legacy readable fixtures, by the contract version whose code wrote them. 2.0.0: the
#: float-only validation report (no exact gate: 2.0.0 has no ``value_exact``), and the matrix /
#: calibration builders unchanged. 2.1.0: the current builders of every kind the 2.2.0 bump changed.
#: 2.2.0: those same builders before the 2.4.0 composition extension. 2.4.0: those builders
#: before ADR-0094's envelope bump.
LEGACY_WRITERS: dict[str, dict[str, Writer]] = {
    "2.0.0": {
        "validation_report": lambda root: write_validation_report(root, _validation_report()),
        "state_strategy_matrix": WRITERS["state_strategy_matrix"],
        "gate_calibration": WRITERS["gate_calibration"],
    },
    "2.1.0": {
        kind: WRITERS[kind]
        for kind in (
            "validation_report",
            "state_strategy_matrix",
            "router_paper_run",
            "gate_calibration",
            "router_stop",
            "paper_deviation",
        )
    },
    "2.2.0": {
        kind: WRITERS[kind]
        for kind in (
            "validation_report",
            "state_strategy_matrix",
            "router_paper_run",
            "gate_calibration",
            "router_stop",
            "paper_deviation",
        )
    },
    "2.4.0": {
        kind: WRITERS[kind]
        for kind in (
            "validation_report",
            "state_strategy_matrix",
            "router_paper_run",
            "gate_calibration",
            "router_stop",
            "paper_deviation",
        )
    },
}
LEGACY_VERSIONS = tuple(LEGACY_WRITERS)


def write_legacy(root: Path) -> list[WrittenReport]:
    """The ``LEGACY_WRITERS`` files of the scope's version. Only meaningful in the process
    :func:`regenerate_legacy` starts: every test fixture module's constants (Refs, Profiles,
    markets) must have been *built* at that version too, so the imports themselves have to run
    inside the scope."""
    version = scoped_contract_schema_version()
    if version not in LEGACY_WRITERS:
        raise RuntimeError(f"write_legacy needs contract_schema_version_scope of {LEGACY_VERSIONS}")
    return [write(root) for write in LEGACY_WRITERS[version].values()]


_LEGACY_PROGRAM = """\
import importlib, json, sys
from pathlib import Path
from core.domain.base import contract_schema_version_scope
version, module, root = sys.argv[1:]
with contract_schema_version_scope(version):
    written = importlib.import_module(module).write_legacy(Path(root))
print(json.dumps([[item.kind, item.id, str(item.path), item.written] for item in written]))
"""


def regenerate_legacy(version: str, root: Path = FIXTURES_ROOT) -> list[WrittenReport]:
    """Write the legacy ``version`` fixtures under ``root`` from a fresh interpreter that imports
    this module (and so every builder's module) inside
    ``contract_schema_version_scope(version)``."""
    result = subprocess.run(
        [sys.executable, "-c", _LEGACY_PROGRAM, version, _MODULE, str(root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("[")]
    return [WrittenReport(k, i, Path(p), w) for k, i, p, w in json.loads(lines[-1])]


def regenerate(root: Path = FIXTURES_ROOT) -> list[WrittenReport]:
    """Write every fixture of every kind under ``root`` (append-only, idempotent)."""
    written = [write(root) for write in WRITERS.values()]
    written += [write(root) for _, _, write in _variant_writers()]
    for version in LEGACY_VERSIONS:
        written += regenerate_legacy(version, root)
    return written


def test_every_report_kind_has_a_generated_fixture() -> None:
    assert set(WRITERS) == {kind.value for kind in ReportKind}
    assert set(LEGACY) == set(LEGACY_WRITERS) == {"2.0.0", "2.1.0", "2.2.0", "2.4.0"}
    for version, ids in LEGACY.items():
        assert {kind.value for kind in ids} == set(LEGACY_WRITERS[version])
    assert {kind.value: set(named) for kind, named in VARIANTS.items()} == {
        kind: set(named) for kind, named in VARIANT_WRITERS.items()
    }


@pytest.mark.parametrize("kind", list(WRITERS))
def test_the_committed_fixture_is_what_the_real_writer_produces(tmp_path: Path, kind: str) -> None:
    written = WRITERS[kind](tmp_path)
    assert written.kind == kind
    variants = [write(tmp_path) for write in VARIANT_WRITERS.get(kind, {}).values()]
    assert all(variant.kind == kind and variant.written for variant in variants)
    committed_dir = FIXTURES_ROOT / kind
    committed = sorted(path.name for path in committed_dir.glob("*.json"))
    legacy = sorted(legacy_ids(ReportKind(kind)))
    expected = sorted(
        [
            written.path.name,
            *(variant.path.name for variant in variants),
            *(f"{item}.json" for item in legacy),
        ]
    )
    assert committed == expected, "stale or missing fixture; regenerate (module docs)"
    for item in [written, *variants]:
        assert (committed_dir / item.path.name).read_bytes() == item.path.read_bytes()


@pytest.mark.parametrize(("kind", "name", "write"), _variant_writers())
def test_each_variant_fixture_is_pinned_by_id(
    tmp_path: Path, kind: str, name: str, write: Writer
) -> None:
    written = write(tmp_path)
    assert written.id == VARIANTS[ReportKind(kind)][name]
    assert written.id != WRITERS[kind](tmp_path).id  # a separate file next to the current one


def test_the_insufficient_evidence_degradation_fixture_is_flagged_and_never_healthy(
    tmp_path: Path,
) -> None:
    written = VARIANT_WRITERS["degradation_check"]["insufficient_evidence"](tmp_path)
    payload = json.loads(written.path.read_text(encoding="utf-8"))
    assert payload["insufficient_evidence"] is True and payload["degraded"] is False
    assert payload["breaches"] == [] and payload["metrics"]
    assert payload["missing"] == sorted(item["metric"] for item in payload["metrics"])
    assert all(item["missing"] and item["recent"] is None for item in payload["metrics"])


@pytest.fixture(scope="module")
def legacy(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, WrittenReport]]:
    """The legacy fixtures of every generation, written once by :func:`regenerate_legacy` (one
    subprocess per version)."""
    return {
        version: {
            item.kind: item
            for item in regenerate_legacy(version, tmp_path_factory.mktemp(f"legacy-{version}"))
        }
        for version in LEGACY_VERSIONS
    }


def test_the_legacy_writers_refuse_to_run_outside_a_legacy_scope(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="contract_schema_version_scope"):
        write_legacy(tmp_path)


@pytest.mark.parametrize(
    ("version", "kind"),
    [(version, kind) for version, writers in LEGACY_WRITERS.items() for kind in writers],
)
def test_the_legacy_fixture_is_what_the_real_writer_produced_at_its_version(
    tmp_path: Path, legacy: dict[str, dict[str, WrittenReport]], version: str, kind: str
) -> None:
    written = legacy[version][kind]
    assert written.written and written.id == LEGACY[version][ReportKind(kind)]
    assert written.id != WRITERS[kind](tmp_path).id  # a separate, older file
    committed = FIXTURES_ROOT / kind / written.path.name
    assert committed.read_bytes() == written.path.read_bytes()


@pytest.mark.parametrize("kind", ["validation_report", "gate_calibration"])
def test_each_generation_carries_its_own_envelopes(
    tmp_path: Path, legacy: dict[str, dict[str, WrittenReport]], kind: str
) -> None:
    current = WRITERS[kind](tmp_path).path.read_text(encoding="utf-8")
    assert CONTRACT_SCHEMA_VERSION == "2.5.0"
    texts = {
        "2.5.0": current,
        **{version: legacy[version][kind].path.read_text("utf-8") for version in LEGACY},
    }
    for version, text in texts.items():
        others = [other for other in texts if other != version]
        assert f'"schema_version":"{version}"' in text, (kind, version)
        assert not any(f'"schema_version":"{other}"' in text for other in others), (kind, version)


def test_the_current_validation_report_carries_an_exact_gate(
    tmp_path: Path, legacy: dict[str, dict[str, WrittenReport]]
) -> None:
    written = WRITERS["validation_report"](tmp_path)
    payload = json.loads(written.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2.5.0" and payload["verdict"] == "PASS"
    old_exact = json.loads(legacy["2.1.0"]["validation_report"].path.read_text("utf-8"))
    for report in (payload, old_exact):
        float_gate, exact = report["gates"]
        assert "value_exact" not in float_gate and "threshold_exact" not in float_gate
        assert (exact["value_exact"], exact["threshold_exact"]) == (EXACT_VALUE, EXACT_THRESHOLD)
        assert exact["value"] == 0.03  # the derived float loses the exact value's last digit
    old = json.loads(legacy["2.0.0"]["validation_report"].path.read_text(encoding="utf-8"))
    assert all("value_exact" not in gate for gate in old["gates"])


def test_regenerate_writes_every_kind_once_and_is_idempotent(tmp_path: Path) -> None:
    first = regenerate(tmp_path)
    assert [item.kind for item in first] == [
        *WRITERS,
        *(kind for kind, _, _ in _variant_writers()),
        *(kind for writers in LEGACY_WRITERS.values() for kind in writers),
    ]
    assert all(item.written for item in first)
    assert not any(item.written for item in regenerate(tmp_path))


if __name__ == "__main__":  # pragma: no cover - manual regeneration
    for item in regenerate():
        print(("wrote " if item.written else "unchanged ") + str(item.path))
