"""ADR-0048 (console): the report store reads research-plane JSON artifacts read-only.

Covers the ``ReportStore`` directly (tmp_path fixtures) and the ``/reports/...`` endpoints it
backs. Nothing here imports ``research/`` — the fixtures write plain JSON files, exactly as the
research plane is expected to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import (
    InvalidReportId,
    ReportKind,
    ReportMalformed,
    ReportNotFound,
    ReportStore,
)
from core.domain.base import content_hash
from tests.apps.loop_records import completed_round
from tests.apps.report_fixtures import fixture


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
    # state_strategy_matrix is the one kind still served opaquely (the others are identity checked)
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "r1", {"verdict": "PASS"})
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "r2", {"verdict": "FAIL"})
    store = ReportStore(tmp_path)

    listed = store.list(ReportKind.STATE_STRATEGY_MATRIX)
    assert {env.id for env in listed} == {"r1", "r2"}
    assert all(env.kind is ReportKind.STATE_STRATEGY_MATRIX for env in listed)

    got = store.get(ReportKind.STATE_STRATEGY_MATRIX, "r1")
    assert got.payload == {"verdict": "PASS"}
    assert got.content_hash == store.get(ReportKind.STATE_STRATEGY_MATRIX, "r1").content_hash


def test_store_is_scoped_per_kind(tmp_path: Path) -> None:
    # a research_loop_round must be a valid LoopRoundRecord named by its record_hash (ADR-0050)
    round_payload = completed_round().payload()
    shared_id = content_hash(round_payload)
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, shared_id, {"kind": "matrix"})
    _write(tmp_path, ReportKind.RESEARCH_LOOP_ROUND, shared_id, round_payload)
    store = ReportStore(tmp_path)
    assert store.get(ReportKind.STATE_STRATEGY_MATRIX, shared_id).payload == {"kind": "matrix"}
    assert store.get(ReportKind.RESEARCH_LOOP_ROUND, shared_id).payload == round_payload
    assert store.list(ReportKind.ROUTER_PAPER_RUN) == []


def test_store_skips_malformed_files_in_list_but_still_returns_the_rest(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "good", {"ok": True})
    directory = tmp_path / ReportKind.STATE_STRATEGY_MATRIX.value
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    (directory / "not-an-object.json").write_text("[1, 2, 3]", encoding="utf-8")
    store = ReportStore(tmp_path)
    listed = store.list(ReportKind.STATE_STRATEGY_MATRIX)
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
    listing = client.get("/reports/validation_report").json()
    assert listing == {"kind": "validation_report", "reports": [], "invalid": []}
    assert client.get("/reports/validation_report/r1").status_code == 404


def test_reports_endpoints_serve_the_configured_directory(tmp_path: Path) -> None:
    round_payload = completed_round().payload()
    round_id = content_hash(round_payload)
    _write(tmp_path, ReportKind.RESEARCH_LOOP_ROUND, round_id, round_payload)
    client = TestClient(create_app(reports_root=tmp_path))

    listing = client.get("/reports/research_loop_round").json()
    assert listing["invalid"] == []
    listed = listing["reports"]
    assert len(listed) == 1
    assert listed[0]["id"] == round_id
    assert listed[0]["kind"] == "research_loop_round"
    assert "content_hash" in listed[0]

    detail = client.get(f"/reports/research_loop_round/{round_id}").json()
    assert detail["payload"] == round_payload


# --- research_loop_round: the LoopRoundRecord contract (ADR-0050) -------------------------


def _tampered_rounds() -> dict[str, dict[str, Any]]:
    """Payloads refused as research_loop_round files, keyed by what is wrong with them."""
    good = completed_round().payload()
    edited_usage = json.loads(json.dumps(good))
    edited_usage["round_usage"]["trials"] = 0  # an edit that breaks the stage accounting
    no_stages = {**good, "stages": []}
    shuffled = {**good, "stages": list(reversed(good["stages"]))}
    extra_key = {**good, "note": "added"}
    bad_status = {**good, "status": "PROMOTED"}
    envelope = {**good, "schema_version": "2.0.0"}  # the persisted bytes carry no envelope
    return {
        "edited-usage": edited_usage,
        "no-stages": no_stages,
        "shuffled-stages": shuffled,
        "extra-key": extra_key,
        "bad-status": bad_status,
        "envelope": envelope,
        "not-a-round": {"round": 1, "budget_used": "10", "failures": []},
    }


@pytest.mark.parametrize("case", sorted(_tampered_rounds()))
def test_an_ill_formed_or_tampered_loop_round_is_refused(tmp_path: Path, case: str) -> None:
    payload = _tampered_rounds()[case]
    # named by its own content hash, so only the contract (not the file name) can refuse it
    report_id = content_hash(payload)
    _write(tmp_path, ReportKind.RESEARCH_LOOP_ROUND, report_id, payload)
    store = ReportStore(tmp_path)
    with pytest.raises(ReportMalformed, match="LoopRoundRecord"):
        store.get(ReportKind.RESEARCH_LOOP_ROUND, report_id)
    assert store.list(ReportKind.RESEARCH_LOOP_ROUND) == []
    client = TestClient(create_app(reports_root=tmp_path))
    assert client.get(f"/reports/research_loop_round/{report_id}").status_code == 422
    listing = client.get("/reports/research_loop_round").json()
    assert listing["reports"] == []
    # skipped, but reported (2026-09-26): the id and the reason, never silently dropped
    [invalid] = listing["invalid"]
    assert invalid["id"] == report_id and "LoopRoundRecord" in invalid["reason"]


def test_a_valid_loop_round_under_another_name_is_refused(tmp_path: Path) -> None:
    """The writer names a round by its record_hash; a renamed or edited-then-rehashed file whose
    name no longer is its hash is not served as that round."""
    round_payload = completed_round().payload()
    _write(tmp_path, ReportKind.RESEARCH_LOOP_ROUND, "round-1", round_payload)
    store = ReportStore(tmp_path)
    with pytest.raises(ReportMalformed, match="record_hash"):
        store.get(ReportKind.RESEARCH_LOOP_ROUND, "round-1")
    assert store.list(ReportKind.RESEARCH_LOOP_ROUND) == []


def test_the_state_strategy_matrix_kind_is_still_served_opaquely(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "any-name", {"round": 1})
    got = ReportStore(tmp_path).get(ReportKind.STATE_STRATEGY_MATRIX, "any-name")
    assert got.payload == {"round": 1}


def test_reports_endpoint_rejects_unknown_kind(tmp_path: Path) -> None:
    client = TestClient(create_app(reports_root=tmp_path))
    assert client.get("/reports/not-a-kind").status_code == 422


def test_reports_endpoint_rejects_path_traversal_id(tmp_path: Path) -> None:
    client = TestClient(create_app(reports_root=tmp_path))
    response = client.get("/reports/validation_report/..%2F..%2Fetc%2Fpasswd")
    assert response.status_code in (400, 404)


# --- malformed files are reported, not silently skipped (2026-09-26) ----------------------


def test_listing_reports_every_malformed_file_with_its_reason(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "good", {"ok": True})
    directory = tmp_path / ReportKind.STATE_STRATEGY_MATRIX.value
    (directory / "bad.json").write_text("{not json", encoding="utf-8")
    (directory / "not-an-object.json").write_text("[1, 2, 3]", encoding="utf-8")
    (directory / "latin1.json").write_bytes(b'{"x": "\xff"}')  # not UTF-8
    listing = ReportStore(tmp_path).listing(ReportKind.STATE_STRATEGY_MATRIX)
    assert [env.id for env in listing.reports] == ["good"]
    reasons = {item.id: item.reason for item in listing.invalid}
    assert reasons == {
        "bad": "unreadable or not well-formed JSON",
        "latin1": "unreadable or not well-formed JSON",
        "not-an-object": "JSON root must be an object",
    }
    assert all(str(tmp_path) not in reason for reason in reasons.values())  # no server paths


def test_listing_of_a_clean_directory_has_no_invalid_entries(tmp_path: Path) -> None:
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "a", {"v": 1})
    _write(tmp_path, ReportKind.STATE_STRATEGY_MATRIX, "b", {"v": 2})
    listing = ReportStore(tmp_path).listing(ReportKind.STATE_STRATEGY_MATRIX)
    assert {env.id for env in listing.reports} == {"a", "b"} and listing.invalid == []
    assert ReportStore(None).listing(ReportKind.VALIDATION_REPORT).invalid == []


def test_the_list_endpoint_reports_malformed_files_alongside_good_ones(tmp_path: Path) -> None:
    good = fixture(ReportKind.GATE_CALIBRATION)
    _write(tmp_path, ReportKind.GATE_CALIBRATION, good.id, good.payload)
    (tmp_path / ReportKind.GATE_CALIBRATION.value / "broken.json").write_text("{", "utf-8")
    client = TestClient(create_app(reports_root=tmp_path))
    listing = client.get("/reports/gate_calibration").json()
    assert listing["kind"] == "gate_calibration"
    assert [env["id"] for env in listing["reports"]] == [good.id]
    assert listing["invalid"] == [{"id": "broken", "reason": "unreadable or not well-formed JSON"}]
    detail = client.get("/reports/gate_calibration/broken")
    assert detail.status_code == 422 and isinstance(detail.json()["detail"], str)
    assert str(tmp_path) not in detail.text  # the reason, never the server's file path


# --- router_stop / state_diagnostics / event_statistics kinds (2026-09-26) ------------------

NEW_KINDS = (ReportKind.ROUTER_STOP, ReportKind.STATE_DIAGNOSTICS, ReportKind.EVENT_STATISTICS)


def test_the_new_report_kinds_have_their_directory_names() -> None:
    assert [kind.value for kind in NEW_KINDS] == [
        "router_stop",
        "state_diagnostics",
        "event_statistics",
    ]


@pytest.mark.parametrize("kind", NEW_KINDS)
def test_the_new_kinds_are_served_and_scoped_per_kind(tmp_path: Path, kind: ReportKind) -> None:
    # a real writer's file (the committed fixture): these kinds are identity checked
    good = fixture(kind)
    _write(tmp_path, kind, good.id, good.payload)
    store = ReportStore(tmp_path)
    assert store.get(kind, good.id).payload == good.payload
    for other in ReportKind:
        if other is not kind:
            assert store.list(other) == []  # never leaks into another kind's listing
    client = TestClient(create_app(reports_root=tmp_path))
    assert client.get(f"/reports/{kind.value}/{good.id}").json()["payload"] == good.payload
    assert client.get(f"/reports/{kind.value}/missing").status_code == 404
    assert client.get(f"/reports/{kind.value}/..%2Fsecret").status_code in (400, 404)


@pytest.mark.parametrize("kind", NEW_KINDS)
def test_the_new_kinds_list_malformed_files_as_invalid(tmp_path: Path, kind: ReportKind) -> None:
    good = fixture(kind)
    _write(tmp_path, kind, good.id, good.payload)
    (tmp_path / kind.value / "broken.json").write_text("{", "utf-8")
    listing = TestClient(create_app(reports_root=tmp_path)).get(f"/reports/{kind.value}").json()
    assert listing["kind"] == kind.value
    assert [env["id"] for env in listing["reports"]] == [good.id]
    assert listing["invalid"] == [{"id": "broken", "reason": "unreadable or not well-formed JSON"}]


def test_the_new_kinds_are_empty_without_a_report_root() -> None:
    client = TestClient(create_app())
    for kind in NEW_KINDS:
        assert client.get(f"/reports/{kind.value}").json() == {
            "kind": kind.value,
            "reports": [],
            "invalid": [],
        }
