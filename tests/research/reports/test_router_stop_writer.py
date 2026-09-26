"""Phase 10 report writers: router stops get their own kind; supplied validation reports are
written with the paper run (and omitted, byte-identically, when not supplied)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.reports import ReportConflict, write_router_paper_run, write_router_stop
from research.reports.router import STOP_KIND
from research.router.paper import RouterStop
from tests.research.router.test_paper import LIFECYCLE, A, B
from tests.research.router.test_router_completion import FLAT, REPORT_A, REPORT_B, _or_stop, _paper


def _load(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_a_router_stop_is_written_under_its_own_kind(tmp_path: Path) -> None:
    stop = _or_stop(FLAT, LIFECYCLE)
    assert isinstance(stop, RouterStop)
    written = write_router_stop(tmp_path, stop)
    assert written.path == tmp_path / STOP_KIND / f"{stop.stop_hash}.json"
    payload = _load(written.path)
    assert payload["reason"] == "all_routes_flat" and payload["stop_hash"] == stop.stop_hash
    assert payload["validation_reports"] is None
    assert not write_router_stop(tmp_path, stop).written  # idempotent
    written.path.write_text(json.dumps({**payload, "reason": "x"}), encoding="utf-8")
    with pytest.raises(ReportConflict):
        write_router_stop(tmp_path, stop)


def test_validation_reports_are_written_only_when_supplied(tmp_path: Path) -> None:
    plain = _load(write_router_paper_run(tmp_path, _paper()).path)
    assert "validation_reports" not in plain
    bound = _paper(validation_reports={A: REPORT_A, B: REPORT_B})
    payload = _load(write_router_paper_run(tmp_path, bound).path)
    assert payload["validation_reports"] == {str(A): REPORT_A, str(B): REPORT_B}
    assert payload["run_hash"] == bound.run_hash != plain["run_hash"]
