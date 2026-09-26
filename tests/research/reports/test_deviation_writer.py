"""Phase 10 paper deviation reports: written under ``paper_deviation/<deviation_hash>.json`` and
served by ``apps/api`` (which recomputes the hash)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportStore
from research.reports import ReportConflict, write_paper_deviation
from research.reports.deviation import KIND
from tests.research.router.test_paper_deviation import deviation


def test_a_paper_deviation_is_written_under_its_hash_and_served(tmp_path: Path) -> None:
    report = deviation()
    written = write_paper_deviation(tmp_path, report)
    assert KIND == ReportKind.PAPER_DEVIATION.value == "paper_deviation"
    assert written.path == tmp_path / KIND / f"{report.deviation_hash}.json"
    payload = json.loads(written.path.read_text(encoding="utf-8"))
    assert payload == report.to_payload()
    envelope = ReportStore(tmp_path).get(ReportKind.PAPER_DEVIATION, written.id)
    assert envelope.payload == payload
    listing = TestClient(create_app(reports_root=tmp_path)).get("/reports/paper_deviation").json()
    assert [item["id"] for item in listing["reports"]] == [written.id] and listing["invalid"] == []


def test_rewriting_is_a_no_op_and_a_changed_file_is_a_conflict(tmp_path: Path) -> None:
    report = deviation()
    written = write_paper_deviation(tmp_path, report)
    assert not write_paper_deviation(tmp_path, report).written
    payload = json.loads(written.path.read_text(encoding="utf-8"))
    written.path.write_text(json.dumps({**payload, "router": "x"}), encoding="utf-8")
    with pytest.raises(ReportConflict):
        write_paper_deviation(tmp_path, report)
    # and the store refuses the edited file: its deviation_hash no longer binds its fields
    [invalid] = ReportStore(tmp_path).listing(ReportKind.PAPER_DEVIATION).invalid
    assert "deviation_hash does not match" in invalid.reason


def test_a_report_whose_fields_no_longer_match_its_hash_is_not_written(tmp_path: Path) -> None:
    report = deviation()
    object.__setattr__(report, "router", "tampered@1.0.0")  # bypasses frozen, keeps the hash
    with pytest.raises(ValueError, match="does not match"):
        write_paper_deviation(tmp_path, report)
    assert not (tmp_path / KIND).exists()


def test_only_a_paper_deviation_is_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PaperDeviation"):
        write_paper_deviation(tmp_path, object())  # type: ignore[arg-type]
