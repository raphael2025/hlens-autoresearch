"""Contract and identity checks of the report kinds (2026-09-26; CODE_COMPLETE / DEBUG_PENDING).

``apps/api/store.py`` recomputes each kind's content identity from the payload fields (it never
imports ``research/``) and refuses a file whose name, recorded hash and fields disagree; a
``validation_report`` must also be a valid ``ValidationReport``. Refused files are listed under
``invalid`` and their detail is a 422 ``ApiError`` whose ``detail`` never carries a server path.
The valid inputs are the committed fixtures, i.e. what the real ``research/reports`` writers
produced (``tests/apps/report_fixtures.py``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportMalformed, ReportStore
from core.domain.base import content_hash
from tests.apps.report_fixtures import fixture

Payload = dict[str, Any]

#: Every kind whose identity the store recomputes (the file must be named by it).
IDENTIFIED = (
    ReportKind.VALIDATION_REPORT,
    ReportKind.ROUTER_PAPER_RUN,
    ReportKind.ROUTER_STOP,
    ReportKind.STATE_DIAGNOSTICS,
    ReportKind.EVENT_STATISTICS,
    ReportKind.GATE_CALIBRATION,
    ReportKind.PAPER_DEVIATION,
    ReportKind.DEGRADATION_CHECK,
    ReportKind.RETRO_AUDIT,
)


def _write(root: Path, kind: ReportKind, report_id: str, payload: Payload) -> None:
    directory = root / kind.value
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{report_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _refused(root: Path, kind: ReportKind, report_id: str, reason: str) -> None:
    """``report_id`` is refused by the store and the API with ``reason`` (and no server path)."""
    store = ReportStore(root)
    with pytest.raises(ReportMalformed, match=reason):
        store.get(kind, report_id)
    listing = store.listing(kind)
    assert report_id not in {env.id for env in listing.reports}
    [invalid] = [item for item in listing.invalid if item.id == report_id]
    assert reason in invalid.reason and str(root) not in invalid.reason
    client = TestClient(create_app(reports_root=root))
    response = client.get(f"/reports/{kind.value}/{report_id}")
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail"} and isinstance(body["detail"], str)
    assert reason in body["detail"]
    assert str(root) not in response.text and "/" + kind.value + "/" not in response.text


@pytest.mark.parametrize("kind", IDENTIFIED)
def test_the_real_writers_files_are_served(tmp_path: Path, kind: ReportKind) -> None:
    good = fixture(kind)
    _write(tmp_path, kind, good.id, good.payload)
    assert ReportStore(tmp_path).get(kind, good.id).payload == good.payload
    listing = TestClient(create_app(reports_root=tmp_path)).get(f"/reports/{kind.value}").json()
    assert [item["id"] for item in listing["reports"]] == [good.id] and listing["invalid"] == []


@pytest.mark.parametrize("kind", IDENTIFIED)
def test_a_real_report_under_another_name_is_refused(tmp_path: Path, kind: ReportKind) -> None:
    good = fixture(kind)
    _write(tmp_path, kind, "renamed", good.payload)
    _refused(tmp_path, kind, "renamed", "the file name is not the report's")


# --- validation_report: a ValidationReport named by its content hash ----------------------


def _validation_edits() -> dict[str, Callable[[Payload], Payload]]:
    def verdict(payload: Payload) -> Payload:  # a verdict that is not derive_verdict(gates)
        return {**payload, "verdict": "FAIL" if payload["verdict"] == "PASS" else "PASS"}

    def extra(payload: Payload) -> Payload:
        return {**payload, "note": "added"}

    def missing(payload: Payload) -> Payload:
        return {key: value for key, value in payload.items() if key != "gates"}

    def not_a_report(payload: Payload) -> Payload:
        return {"verdict": "PASS"}

    return {"verdict": verdict, "extra": extra, "missing": missing, "not-a-report": not_a_report}


@pytest.mark.parametrize("case", sorted(_validation_edits()))
def test_an_invalid_validation_report_is_refused(tmp_path: Path, case: str) -> None:
    good = fixture(ReportKind.VALIDATION_REPORT)
    payload = _validation_edits()[case](good.payload)
    report_id = content_hash(payload)  # named by a hash, so only the contract can refuse it
    _write(tmp_path, ReportKind.VALIDATION_REPORT, report_id, payload)
    _refused(tmp_path, ReportKind.VALIDATION_REPORT, report_id, "not a valid ValidationReport")


def test_a_valid_but_edited_validation_report_under_its_old_name_is_refused(
    tmp_path: Path,
) -> None:
    good = fixture(ReportKind.VALIDATION_REPORT)
    edited = {**good.payload, "report_id": "another-label"}  # still valid, another content hash
    _write(tmp_path, ReportKind.VALIDATION_REPORT, good.id, edited)
    _refused(tmp_path, ReportKind.VALIDATION_REPORT, good.id, "report's content hash")


def test_a_non_canonical_validation_report_payload_is_refused(tmp_path: Path) -> None:
    good = fixture(ReportKind.VALIDATION_REPORT)
    # a datetime the contract parses but would write back differently: not the writer's bytes
    edited = {**good.payload, "created_at": "2026-01-01T00:00:00+00:00"}
    _write(tmp_path, ReportKind.VALIDATION_REPORT, good.id, edited)
    _refused(tmp_path, ReportKind.VALIDATION_REPORT, good.id, "does not round-trip")


# --- the hash-identified kinds: an edited field no longer matches the recorded hash --------


def _edit_router_run(payload: Payload) -> Payload:
    decisions = [dict(item) for item in payload["decisions"]]
    decisions[0]["turnover"] = "9"
    return {**payload, "decisions": decisions}


def _edit_charges(payload: Payload) -> Payload:
    charges = [dict(item) for item in payload["charges"]]
    charges[0]["amount"] = "0"
    return {**payload, "charges": charges}


HASH_EDITS: dict[str, tuple[ReportKind, str, Callable[[Payload], Payload]]] = {
    "run-decision": (ReportKind.ROUTER_PAPER_RUN, "run_hash", _edit_router_run),
    "run-charge": (ReportKind.ROUTER_PAPER_RUN, "run_hash", _edit_charges),
    "run-result": (
        ReportKind.ROUTER_PAPER_RUN,
        "run_hash",
        lambda p: {**p, "result_hash": "0" * 64},
    ),
    "run-reports-added": (
        ReportKind.ROUTER_PAPER_RUN,
        "run_hash",
        lambda p: {**p, "validation_reports": {"strategy:x@1.0.0": "0" * 64}},
    ),
    "stop-detail": (ReportKind.ROUTER_STOP, "stop_hash", lambda p: {**p, "detail": "edited"}),
    "stop-lifecycle": (ReportKind.ROUTER_STOP, "stop_hash", lambda p: {**p, "lifecycle": {}}),
    "stop-eligibility-added": (
        ReportKind.ROUTER_STOP,
        "stop_hash",
        lambda p: {**p, "eligibility": []},
    ),
    "event-statistics": (
        ReportKind.EVENT_STATISTICS,
        "report_hash",
        lambda p: {**p, "source_result_hashes": []},
    ),
    "gate-calibration": (
        ReportKind.GATE_CALIBRATION,
        "report_hash",
        lambda p: {**p, "disclaimer": "a Profile decision"},
    ),
    "paper-deviation": (
        ReportKind.PAPER_DEVIATION,
        "deviation_hash",
        lambda p: {**p, "summary": {**p["summary"], "tracking_error": "0"}},
    ),
    "degradation-check": (
        ReportKind.DEGRADATION_CHECK,
        "check_hash",
        lambda p: {**p, "degraded": not p["degraded"]},
    ),
    "retro-audit": (
        ReportKind.RETRO_AUDIT,
        "report_hash",
        lambda p: {**p, "rules": "edited"},
    ),
}


@pytest.mark.parametrize("case", sorted(HASH_EDITS))
def test_an_edited_field_breaks_the_recorded_hash(tmp_path: Path, case: str) -> None:
    kind, field, edit = HASH_EDITS[case]
    good = fixture(kind)
    _write(tmp_path, kind, good.id, edit(good.payload))
    _refused(tmp_path, kind, good.id, f"{field} does not match the payload it binds")


@pytest.mark.parametrize("kind", [ReportKind.EVENT_STATISTICS, ReportKind.GATE_CALIBRATION])
def test_an_edit_with_a_recomputed_hash_keeps_failing_on_the_name(
    tmp_path: Path, kind: ReportKind
) -> None:
    """Re-recording the hash of an edited body is not enough: the file keeps the writer's name."""
    good = fixture(kind)
    body = {key: value for key, value in good.payload.items() if key != "report_hash"}
    body["status"] = "VALIDATED"
    _write(tmp_path, kind, good.id, {**body, "report_hash": content_hash(body)})
    _refused(tmp_path, kind, good.id, "the file name is not the report's report_hash")


def test_an_edited_state_diagnostics_report_is_refused(tmp_path: Path) -> None:
    good = fixture(ReportKind.STATE_DIAGNOSTICS)
    edited = {**good.payload, "counts": {"a": 4, "b": 3}}
    _write(tmp_path, ReportKind.STATE_DIAGNOSTICS, good.id, edited)
    _refused(tmp_path, ReportKind.STATE_DIAGNOSTICS, good.id, "diagnostics_hash")


@pytest.mark.parametrize(
    ("kind", "field"),
    [(ReportKind.ROUTER_PAPER_RUN, "decisions"), (ReportKind.ROUTER_STOP, "reason")],
)
def test_a_router_record_without_a_bound_field_is_refused(
    tmp_path: Path, kind: ReportKind, field: str
) -> None:
    good = fixture(kind)
    payload = {key: value for key, value in good.payload.items() if key != field}
    _write(tmp_path, kind, good.id, payload)
    _refused(tmp_path, kind, good.id, "lacks a field bound by")


def test_a_router_decision_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    good = fixture(ReportKind.ROUTER_PAPER_RUN)
    _write(tmp_path, ReportKind.ROUTER_PAPER_RUN, good.id, {**good.payload, "decisions": [1]})
    _refused(tmp_path, ReportKind.ROUTER_PAPER_RUN, good.id, "lacks a field bound by run_hash")


def test_a_self_hashed_payload_that_is_not_canonical_json_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / ReportKind.EVENT_STATISTICS.value
    directory.mkdir(parents=True)
    (directory / "nan.json").write_text('{"x": NaN, "report_hash": "nan"}', encoding="utf-8")
    _refused(tmp_path, ReportKind.EVENT_STATISTICS, "nan", "not canonical JSON")


def test_a_consistent_self_hashed_report_is_served(tmp_path: Path) -> None:
    """The identity rule, not the fixture, is what is checked: a consistent payload is served."""
    body = {"kind": "event_statistics", "n": 1}
    report_hash = content_hash(body)
    _write(tmp_path, ReportKind.EVENT_STATISTICS, report_hash, {**body, "report_hash": report_hash})
    assert ReportStore(tmp_path).get(ReportKind.EVENT_STATISTICS, report_hash).payload["n"] == 1


# --- error bodies never carry a server path (2026-09-26) -----------------------------------


def test_the_malformed_detail_is_an_api_error_without_the_file_path(tmp_path: Path) -> None:
    directory = tmp_path / ReportKind.STATE_STRATEGY_MATRIX.value
    directory.mkdir(parents=True)
    (directory / "broken.json").write_text("{", encoding="utf-8")
    response = TestClient(create_app(reports_root=tmp_path)).get(
        "/reports/state_strategy_matrix/broken"
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": "state_strategy_matrix/broken is malformed: unreadable or not well-formed JSON"
    }
    assert str(tmp_path) not in response.text
