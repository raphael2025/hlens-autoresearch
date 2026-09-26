"""Report writers for ``router_stop`` (Phase 10), ``state_diagnostics`` (Phase 2) and
``event_statistics`` (Phase 3): each file round-trips through ``apps.api.store.ReportStore`` and
the ``/reports/...`` endpoints, rewriting is idempotent, a conflicting file is refused
(append-only), an inconsistent object is never written, and a corrupt file under the kind is
listed as invalid next to the good ones.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportStore
from core.domain.base import content_hash
from research.events.stats import EventStatsReport
from research.reports import (
    ReportConflict,
    WrittenReport,
    write_event_statistics,
    write_router_stop,
    write_state_diagnostics,
)
from research.reports.event_statistics import KIND as EVENT_KIND
from research.reports.router import STOP_KIND
from research.reports.state_diagnostics import KIND as DIAGNOSTICS_KIND
from research.states.diagnostics import PAYLOAD_KIND, StateDiagnostics
from tests.research.reports.test_console_fixture_writers import (
    event_statistics_report,
    router_stop,
    state_diagnostics,
)

#: kind -> (write the fixture object, the id the object names itself by)
CASES: dict[ReportKind, tuple[Callable[[Path], WrittenReport], Callable[[], str]]] = {
    ReportKind.ROUTER_STOP: (
        lambda root: write_router_stop(root, router_stop()),
        lambda: router_stop().stop_hash,
    ),
    ReportKind.STATE_DIAGNOSTICS: (
        lambda root: write_state_diagnostics(root, state_diagnostics()),
        lambda: state_diagnostics().diagnostics_hash,
    ),
    ReportKind.EVENT_STATISTICS: (
        lambda root: write_event_statistics(root, event_statistics_report()),
        lambda: event_statistics_report().report_hash,
    ),
}


def _load(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_writer_kinds_match_the_api_report_kinds_and_the_payload_kinds() -> None:
    assert STOP_KIND == ReportKind.ROUTER_STOP.value
    assert DIAGNOSTICS_KIND == ReportKind.STATE_DIAGNOSTICS.value == PAYLOAD_KIND
    assert EVENT_KIND == ReportKind.EVENT_STATISTICS.value
    assert event_statistics_report().to_payload()["kind"] == EVENT_KIND


@pytest.mark.parametrize("kind", list(CASES))
def test_written_report_round_trips_through_the_store_and_the_api(
    tmp_path: Path, kind: ReportKind
) -> None:
    write, expected_id = CASES[kind]
    written = write(tmp_path)
    assert written.written and written.kind == kind.value and written.id == expected_id()
    assert written.path == tmp_path / kind.value / f"{expected_id()}.json"
    on_disk = _load(written.path)

    store = ReportStore(tmp_path)
    listing = store.listing(kind)
    assert [env.id for env in listing.reports] == [written.id] and listing.invalid == []
    envelope = store.get(kind, written.id)
    assert envelope.kind is kind and envelope.payload == on_disk

    client = TestClient(create_app(reports_root=tmp_path))
    api_listing = client.get(f"/reports/{kind.value}").json()
    assert api_listing["kind"] == kind.value
    assert [item["id"] for item in api_listing["reports"]] == [written.id]
    assert api_listing["invalid"] == []
    detail = client.get(f"/reports/{kind.value}/{written.id}")
    assert detail.status_code == 200
    assert detail.json()["payload"] == on_disk
    assert detail.json()["content_hash"] == envelope.content_hash


@pytest.mark.parametrize("kind", list(CASES))
def test_rewrite_is_idempotent_and_a_conflicting_file_is_refused(
    tmp_path: Path, kind: ReportKind
) -> None:
    write, _ = CASES[kind]
    first = write(tmp_path)
    before = first.path.read_bytes()
    again = write(tmp_path)
    assert not again.written and again.path == first.path
    assert first.path.read_bytes() == before  # a no-op, not a rewrite

    first.path.write_text(json.dumps({**_load(first.path), "tampered": True}), encoding="utf-8")
    with pytest.raises(ReportConflict):
        write(tmp_path)
    assert _load(first.path)["tampered"] is True  # never silently overwritten


@pytest.mark.parametrize("kind", list(CASES))
def test_a_corrupt_file_under_the_kind_is_listed_as_invalid(
    tmp_path: Path, kind: ReportKind
) -> None:
    write, _ = CASES[kind]
    written = write(tmp_path)
    (tmp_path / kind.value / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / kind.value / "not-an-object.json").write_text("[]", encoding="utf-8")
    client = TestClient(create_app(reports_root=tmp_path))
    listing = client.get(f"/reports/{kind.value}").json()
    assert [item["id"] for item in listing["reports"]] == [written.id]
    assert listing["invalid"] == [
        {"id": "broken", "reason": "unreadable or not well-formed JSON"},
        {"id": "not-an-object", "reason": "JSON root must be an object"},
    ]
    assert client.get(f"/reports/{kind.value}/broken").status_code == 422


def test_state_diagnostics_payload_rebuilds_the_report(tmp_path: Path) -> None:
    report = state_diagnostics()
    payload = _load(write_state_diagnostics(tmp_path, report).path)
    assert StateDiagnostics.from_payload(payload, expected_hash=report.diagnostics_hash) == report
    assert payload["kind"] == "state_diagnostics" and payload["min_run"] == report.min_run


def test_state_diagnostics_that_do_not_round_trip_are_never_written(tmp_path: Path) -> None:
    broken = replace(state_diagnostics(), counts={"a": 3})  # a state of state_space is missing
    with pytest.raises(ValueError, match="do not round-trip"):
        write_state_diagnostics(tmp_path, broken)
    assert not (tmp_path / DIAGNOSTICS_KIND).exists()


def test_event_statistics_payload_binds_its_runs_and_its_hash(tmp_path: Path) -> None:
    report = event_statistics_report()
    payload = _load(write_event_statistics(tmp_path, report).path)
    assert payload["report_hash"] == report.report_hash
    body = {key: value for key, value in payload.items() if key != "report_hash"}
    assert content_hash(body) == report.report_hash
    assert payload["source_result_hashes"] == sorted(report.source_result_hashes)
    assert [item["kind"] for item in payload["statistics"]] == [
        "event_frequency", "co_occurrence", "lead_lag", "overlap_diagnostics",
    ]  # fmt: skip


def test_an_event_statistics_report_with_a_stale_hash_is_never_written(tmp_path: Path) -> None:
    report = event_statistics_report()
    stale = EventStatsReport(report.source_result_hashes, report.statistics)
    object.__setattr__(stale, "report_hash", "0" * 64)  # frozen: only a deliberate tamper
    with pytest.raises(ValueError, match="report_hash does not match"):
        write_event_statistics(tmp_path, stale)
    assert not (tmp_path / EVENT_KIND).exists()


def test_router_stop_payload_carries_the_reason_and_its_hash(tmp_path: Path) -> None:
    stop = router_stop()
    payload = _load(write_router_stop(tmp_path, stop).path)
    assert payload["reason"] == "all_routes_flat"
    assert payload["stop_hash"] == stop.stop_hash
    assert payload["lifecycle"] == dict(stop.lifecycle)
