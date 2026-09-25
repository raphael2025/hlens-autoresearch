"""ADR-0048 (console): the report store reads research-plane JSON artifacts read-only.

Covers the ``ReportStore`` directly (tmp_path fixtures) and the ``/reports/...`` endpoints it
backs. Nothing here imports ``research/`` — the fixtures write plain JSON files, exactly as the
research plane is expected to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import (
    InvalidReportId,
    ReportKind,
    ReportNotFound,
    ReportStore,
)


def _write(root: Path, kind: ReportKind, report_id: str, payload: dict[str, object]) -> None:
    directory = root / kind.value
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{report_id}.json").write_text(json.dumps(payload), encoding="utf-8")


# --- ReportStore -------------------------------------------------------------------------


def test_store_with_no_root_is_always_empty() -> None:
    store = ReportStore(None)
    assert store.list(ReportKind.VALIDATION_REPORT) == []
    with pytest.raises(ReportNotFound):
        store.get(ReportKind.VALIDATION_REPORT, "anything")


def test_store_lists_and_gets_well_formed_reports(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.VALIDATION_REPORT, "r1", {"verdict": "PASS"})
    _write(tmp_path, ReportKind.VALIDATION_REPORT, "r2", {"verdict": "FAIL"})
    store = ReportStore(tmp_path)

    listed = store.list(ReportKind.VALIDATION_REPORT)
    assert {env.id for env in listed} == {"r1", "r2"}
    assert all(env.kind is ReportKind.VALIDATION_REPORT for env in listed)

    got = store.get(ReportKind.VALIDATION_REPORT, "r1")
    assert got.payload == {"verdict": "PASS"}
    assert got.content_hash == store.get(ReportKind.VALIDATION_REPORT, "r1").content_hash


def test_store_is_scoped_per_kind(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.VALIDATION_REPORT, "shared-id", {"kind": "validation"})
    _write(tmp_path, ReportKind.RESEARCH_LOOP_ROUND, "shared-id", {"kind": "round"})
    store = ReportStore(tmp_path)
    assert store.get(ReportKind.VALIDATION_REPORT, "shared-id").payload == {"kind": "validation"}
    assert store.get(ReportKind.RESEARCH_LOOP_ROUND, "shared-id").payload == {"kind": "round"}
    assert store.list(ReportKind.STATE_STRATEGY_MATRIX) == []


def test_store_skips_malformed_files_in_list_but_still_returns_the_rest(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.ROUTER_PAPER_RUN, "good", {"ok": True})
    directory = tmp_path / ReportKind.ROUTER_PAPER_RUN.value
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    (directory / "not-an-object.json").write_text("[1, 2, 3]", encoding="utf-8")
    store = ReportStore(tmp_path)
    listed = store.list(ReportKind.ROUTER_PAPER_RUN)
    assert {env.id for env in listed} == {"good"}


def test_store_get_on_malformed_file_raises(tmp_path: Path) -> None:
    directory = tmp_path / ReportKind.VALIDATION_REPORT.value
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    store = ReportStore(tmp_path)
    with pytest.raises(ValueError):
        store.get(ReportKind.VALIDATION_REPORT, "bad")


def test_store_get_missing_report_raises_not_found(tmp_path: Path) -> None:
    store = ReportStore(tmp_path)
    with pytest.raises(ReportNotFound):
        store.get(ReportKind.VALIDATION_REPORT, "nope")


@pytest.mark.parametrize(
    "bad_id",
    ["../secret", "..", "a/../../etc/passwd", "/etc/passwd", "a/b", ".hidden", ""],
)
def test_store_refuses_path_traversal_ids(tmp_path: Path, bad_id: str) -> None:
    store = ReportStore(tmp_path)
    with pytest.raises(InvalidReportId):
        store.get(ReportKind.VALIDATION_REPORT, bad_id)


def test_store_cannot_escape_root_even_via_symlink_like_traversal(tmp_path: Path) -> None:
    root = tmp_path / "reports"
    root.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"leaked": True}), encoding="utf-8")
    store = ReportStore(root)
    # a dotted id that resolves outside root is rejected by the id pattern already; this is a
    # belt-and-braces check that get() still refuses anything not physically under kind_dir.
    with pytest.raises((InvalidReportId, ReportNotFound)):
        store.get(ReportKind.VALIDATION_REPORT, "..%2Fsecret")


# --- HTTP endpoints ------------------------------------------------------------------------


def test_reports_endpoints_are_empty_with_no_configured_root() -> None:
    client = TestClient(create_app())
    assert client.get("/reports/validation_report").json() == []
    assert client.get("/reports/validation_report/r1").status_code == 404


def test_reports_endpoints_serve_the_configured_directory(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ReportKind.RESEARCH_LOOP_ROUND,
        "round-1",
        {"round": 1, "budget_used": "10", "failures": []},
    )
    client = TestClient(create_app(reports_root=tmp_path))

    listed = client.get("/reports/research_loop_round").json()
    assert len(listed) == 1
    assert listed[0]["id"] == "round-1"
    assert listed[0]["kind"] == "research_loop_round"
    assert "content_hash" in listed[0]

    detail = client.get("/reports/research_loop_round/round-1").json()
    assert detail["payload"] == {"round": 1, "budget_used": "10", "failures": []}


def test_reports_endpoint_rejects_unknown_kind(tmp_path: Path) -> None:
    client = TestClient(create_app(reports_root=tmp_path))
    assert client.get("/reports/not-a-kind").status_code == 422


def test_reports_endpoint_rejects_path_traversal_id(tmp_path: Path) -> None:
    client = TestClient(create_app(reports_root=tmp_path))
    response = client.get("/reports/validation_report/..%2F..%2Fetc%2Fpasswd")
    assert response.status_code in (400, 404)
