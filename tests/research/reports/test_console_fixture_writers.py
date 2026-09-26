"""``apps/web/fixtures/`` for ``router_stop`` / ``state_diagnostics`` / ``event_statistics`` /
``paper_deviation`` / ``degradation_check`` are exactly what the real ``research/reports`` writers
produce for small, existing test objects.

The committed files are never hand-written: this module builds the objects from the existing
research test fixtures, writes them with the real writers into a temporary report root, and
requires the committed fixture directory to hold byte-identical files — and nothing else — for
each of these kinds. Regenerate after a payload change (delete the stale ``<kind>/`` file first;
writers are append-only)::

    uv run python -m tests.research.reports.test_console_fixture_writers

(``apps/web/fixtures/README.md``; ``tests/apps/test_console_fixtures.py`` separately checks that
every ``ReportKind`` has a fixture that loads through ``apps.api``.)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from core.domain.base import content_hash
from research.events.stats import EventStatsReport
from research.reports import (
    WrittenReport,
    write_event_statistics,
    write_paper_deviation,
    write_router_stop,
    write_state_diagnostics,
)
from research.router.deviation import PaperDeviation
from research.router.paper import RouterStop
from research.states.diagnostics import StateDiagnostics, diagnose
from tests.research.events.test_event_stats import _all_statistics
from tests.research.reports.test_degradation_writer import write_fixture as write_degradation
from tests.research.router.test_paper import LIFECYCLE
from tests.research.router.test_paper_deviation import deviation
from tests.research.router.test_router_completion import FLAT, _or_stop
from tests.research.states.test_state_diagnostics import SERIES, SPACE

FIXTURES_ROOT = Path(__file__).resolve().parents[3] / "apps" / "web" / "fixtures"


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


WRITERS: dict[str, Callable[[Path], WrittenReport]] = {
    "router_stop": lambda root: write_router_stop(root, router_stop()),
    "state_diagnostics": lambda root: write_state_diagnostics(root, state_diagnostics()),
    "event_statistics": lambda root: write_event_statistics(root, event_statistics_report()),
    "paper_deviation": lambda root: write_paper_deviation(root, paper_deviation()),
    # TEST ONLY thresholds / metrics (tests/research/reports/test_degradation_writer.py)
    "degradation_check": write_degradation,
}


def regenerate(root: Path = FIXTURES_ROOT) -> list[WrittenReport]:
    """Write every fixture of this module's kinds under ``root`` (append-only, idempotent)."""
    return [write(root) for write in WRITERS.values()]


@pytest.mark.parametrize("kind", list(WRITERS))
def test_the_committed_fixture_is_what_the_real_writer_produces(tmp_path: Path, kind: str) -> None:
    written = WRITERS[kind](tmp_path)
    assert written.kind == kind
    committed_dir = FIXTURES_ROOT / kind
    committed = sorted(path.name for path in committed_dir.glob("*.json"))
    assert committed == [written.path.name], "stale or missing fixture; regenerate (module docs)"
    assert (committed_dir / written.path.name).read_bytes() == written.path.read_bytes()


def test_regenerate_writes_every_kind_once_and_is_idempotent(tmp_path: Path) -> None:
    first = regenerate(tmp_path)
    assert [item.kind for item in first] == list(WRITERS) and all(item.written for item in first)
    assert not any(item.written for item in regenerate(tmp_path))


if __name__ == "__main__":  # pragma: no cover - manual regeneration
    for item in regenerate():
        print(("wrote " if item.written else "unchanged ") + str(item.path))
