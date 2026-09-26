"""ADR-0048 (console): ``apps/web/fixtures/`` loads cleanly through ``apps.api``.

``apps/web/fixtures/`` is a real report root (``<kind>/<id>.json``) committed so `npm run dev`
has data for every console page without a live research-plane run (``apps/web/README.md`` "用
apps/web/fixtures/ 快速起一个有数据的后端"). This test is the guard that keeps it that way: every
``ReportKind`` must have at least one fixture file, and every fixture file must round-trip
through both ``ReportStore`` (direct) and the ``/reports/...`` HTTP endpoints
(``apps.api.create_app``) without being skipped as malformed. It does not import ``research/`` —
like ``apps/api`` itself, it only reads the JSON files back (apps/ must not import research/,
enforced by ``tests/test_architecture_boundaries.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportStore

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web" / "fixtures"


def test_fixtures_root_exists() -> None:
    assert FIXTURES_ROOT.is_dir(), FIXTURES_ROOT


@pytest.mark.parametrize("kind", list(ReportKind))
def test_every_report_kind_has_at_least_one_fixture(kind: ReportKind) -> None:
    store = ReportStore(FIXTURES_ROOT)
    envelopes = store.list(kind)
    assert envelopes, f"no fixture file under apps/web/fixtures/{kind.value}/"


@pytest.mark.parametrize("kind", list(ReportKind))
def test_every_fixture_round_trips_through_the_store_and_the_api(kind: ReportKind) -> None:
    store = ReportStore(FIXTURES_ROOT)
    client = TestClient(create_app(reports_root=FIXTURES_ROOT))

    listed = store.list(kind)
    api_listing = client.get(f"/reports/{kind.value}").json()
    assert api_listing["invalid"] == []  # every committed fixture is well-formed
    api_listed = api_listing["reports"]
    assert {env.id for env in listed} == {item["id"] for item in api_listed}

    for envelope in listed:
        assert envelope.kind is kind
        assert isinstance(envelope.payload, dict) and envelope.payload  # not empty
        assert isinstance(envelope.content_hash, str) and len(envelope.content_hash) == 64

        got = store.get(kind, envelope.id)
        assert got.payload == envelope.payload
        assert got.content_hash == envelope.content_hash

        detail = client.get(f"/reports/{kind.value}/{envelope.id}")
        assert detail.status_code == 200
        assert detail.json()["payload"] == envelope.payload


def test_validation_report_fixture_has_a_verdict() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.VALIDATION_REPORT)
    assert envelope.payload["verdict"] in {"PASS", "FAIL", "INCONCLUSIVE"}
    assert isinstance(envelope.payload["gates"], list) and envelope.payload["gates"]


def test_research_loop_round_fixture_has_a_status() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.RESEARCH_LOOP_ROUND)
    assert envelope.payload["loop_id"]
    assert envelope.payload["status"]


def test_state_strategy_matrix_fixture_has_cells() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.STATE_STRATEGY_MATRIX)
    assert envelope.payload["matrix_hash"]
    assert isinstance(envelope.payload["cells"], list) and envelope.payload["cells"]


def test_router_paper_run_fixture_has_equity_curves() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.ROUTER_PAPER_RUN)
    assert envelope.payload["run_hash"]
    assert envelope.payload["gross_equity_curve"]
    assert envelope.payload["net_equity_curve"]


def test_gate_calibration_fixture_carries_the_evidence_only_disclaimer() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.GATE_CALIBRATION)
    assert envelope.payload["disclaimer"] == "evidence only — not a Profile decision"
    candidates = envelope.payload["candidates"]
    assert isinstance(candidates, list) and len(candidates) >= 2
    for candidate in candidates:
        assert candidate["pipeline"]  # at least the noise arm
        assert candidate["gates"]


def test_router_stop_fixture_names_its_reason_and_hash() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.ROUTER_STOP)
    assert envelope.payload["reason"] in {"no_validated_candidate", "all_routes_flat"}
    assert envelope.payload["stop_hash"] == envelope.id
    assert isinstance(envelope.payload["lifecycle"], dict) and envelope.payload["lifecycle"]


def test_state_diagnostics_fixture_has_runs_and_transitions() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.STATE_DIAGNOSTICS)
    payload = envelope.payload
    assert payload["kind"] == "state_diagnostics"
    space = payload["state_space"]
    assert isinstance(space, list) and space
    assert set(payload["counts"]) == set(space) == set(payload["transitions"])
    assert isinstance(payload["runs"], list) and payload["runs"]


def test_event_statistics_fixture_binds_its_runs() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.EVENT_STATISTICS)
    payload = envelope.payload
    assert payload["report_hash"] == envelope.id
    assert payload["source_result_hashes"]
    assert {item["kind"] for item in payload["statistics"]} == {
        "event_frequency",
        "co_occurrence",
        "lead_lag",
        "overlap_diagnostics",
    }


def test_paper_deviation_fixture_compares_every_mark() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.PAPER_DEVIATION)
    payload = envelope.payload
    assert payload["kind"] == "paper_deviation"
    assert payload["deviation_hash"] == envelope.id
    assert payload["run_hash"] and payload["reference_result_hash"]
    assert isinstance(payload["marks"], list) and payload["marks"]
    assert payload["summary"]["marks"] == len(payload["marks"])
    # the router paper run it describes is the committed router_paper_run fixture
    (run,) = store.list(ReportKind.ROUTER_PAPER_RUN)
    assert payload["run_hash"] == run.id
    assert [mark["time"] for mark in payload["marks"]] == [
        point["time"] for point in run.payload["net_equity_curve"]
    ]


def test_degradation_check_fixture_names_its_rules_and_sources() -> None:
    store = ReportStore(FIXTURES_ROOT)
    (envelope,) = store.list(ReportKind.DEGRADATION_CHECK)
    payload = envelope.payload
    assert payload["kind"] == "degradation_check"
    assert payload["check_hash"] == envelope.id
    assert payload["degraded"] is True and payload["breaches"]
    metrics = payload["metrics"]
    assert isinstance(metrics, list) and metrics
    for metric in metrics:
        assert metric["threshold_source"]  # every threshold names where it came from
        assert metric["direction"] in {"higher_is_better", "lower_is_better"}
        assert (metric["recent"] is None) == metric["missing"]
    assert {item["metric"] for item in metrics if item["breached"]} == {
        breach["metric"] for breach in payload["breaches"]
    }
