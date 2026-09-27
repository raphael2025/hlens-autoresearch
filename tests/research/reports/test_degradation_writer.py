"""Phase 11 degradation checks (``apps/worker/degradation.py``) as the ``degradation_check`` report.

The thresholds and metric values here are TEST ONLY numbers (no Profile value): one breached
metric (higher is better), one within its allowed decline (lower is better) and one without a
recent value (missing, evidence insufficient).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.store import ReportKind, ReportStore
from apps.worker.degradation import DegradationCheck, DegradationMonitor
from core.domain.base import Kind, Ref, content_hash
from research.reports import ReportConflict, WrittenReport, write_degradation_check
from research.reports.degradation import KIND, degradation_check_payload

SUBJECT = Ref(kind=Kind.STRATEGY, name="trend_a", version="1.0.0")
#: TEST ONLY thresholds (not Profile values).
THRESHOLDS: dict[str, Decimal | float] = {
    "sharpe": Decimal("0.5"),
    "max_drawdown[<=]": Decimal("0.1"),
    "hit_rate": 0.05,
}
SOURCE = "tests/research/reports/test_degradation_writer.py (TEST ONLY thresholds)"
BASELINE: dict[str, Decimal | float | int] = {
    "sharpe": Decimal("1.4"),
    "max_drawdown": Decimal("0.12"),
    "hit_rate": 0.55,
}
RECENT: dict[str, Decimal | float | int] = {
    "sharpe": Decimal("0.7"),
    "max_drawdown": Decimal("0.15"),
}  # hit_rate missing
WINDOW = "paper 2026-01-01..2026-03-31 (TEST ONLY)"


def monitor() -> DegradationMonitor:
    return DegradationMonitor(THRESHOLDS, source=SOURCE)


def check() -> DegradationCheck:
    return monitor().check(SUBJECT, BASELINE, RECENT)


def write_fixture(root: Path) -> WrittenReport:
    """The console fixture (``apps/web/fixtures/degradation_check/``)."""
    return write_degradation_check(
        root, check(), monitor=monitor(), baseline=BASELINE, recent=RECENT, window=WINDOW
    )


def write_insufficient_evidence_fixture(root: Path) -> WrittenReport:
    """The second console fixture: the same monitor with no recent value at all (every metric
    missing -> ``insufficient_evidence``)."""
    empty = monitor().check(SUBJECT, BASELINE, {})
    return write_degradation_check(
        root, empty, monitor=monitor(), baseline=BASELINE, recent={}, window=WINDOW
    )


def _payload() -> dict[str, Any]:
    return degradation_check_payload(
        check(), monitor=monitor(), baseline=BASELINE, recent=RECENT, window=WINDOW
    )


def test_the_payload_records_every_rule_with_its_values_and_source() -> None:
    payload = _payload()
    assert payload["kind"] == KIND == ReportKind.DEGRADATION_CHECK.value
    assert payload["subject"] == "strategy:trend_a@1.0.0"
    assert payload["window"] == WINDOW
    assert payload["degraded"] is True
    metrics = {item["metric"]: item for item in payload["metrics"]}
    assert list(metrics) == ["hit_rate", "max_drawdown", "sharpe"]  # the monitor's rule order
    assert metrics["sharpe"] == {
        "metric": "sharpe",
        "direction": "higher_is_better",
        "baseline": "1.4",
        "recent": "0.7",
        "decline": "0.7",
        "max_decline": "0.5",
        "threshold_source": f"{SOURCE}[sharpe]",
        "breached": True,
        "missing": False,
    }
    drawdown = metrics["max_drawdown"]
    assert (drawdown["direction"], drawdown["decline"], drawdown["breached"]) == (
        "lower_is_better",
        "0.03",
        False,
    )
    hit = metrics["hit_rate"]
    assert (hit["baseline"], hit["recent"], hit["decline"], hit["missing"]) == (
        "0.55",
        None,
        None,
        True,
    )
    assert hit["max_decline"] == "0.05" and hit["breached"] is False
    assert payload["missing"] == ["hit_rate"]
    assert [breach["metric"] for breach in payload["breaches"]] == ["sharpe"]


def test_the_payload_is_deterministic_and_content_hashed() -> None:
    payload, again = _payload(), _payload()
    assert payload == again
    body = {key: value for key, value in payload.items() if key != "check_hash"}
    assert payload["check_hash"] == content_hash(body)
    other = degradation_check_payload(
        check(), monitor=monitor(), baseline=BASELINE, recent=RECENT, window="another window"
    )
    assert other["check_hash"] != payload["check_hash"]


def test_a_healthy_check_is_reported_too() -> None:
    recent: dict[str, Decimal | float | int] = {
        "sharpe": Decimal("1.3"),
        "max_drawdown": Decimal("0.12"),
        "hit_rate": 0.5,
    }
    healthy = monitor().check(SUBJECT, BASELINE, recent)
    payload = degradation_check_payload(
        healthy, monitor=monitor(), baseline=BASELINE, recent=recent, window=WINDOW
    )
    assert payload["degraded"] is False and payload["breaches"] == [] and payload["missing"] == []


def test_a_check_without_any_recent_value_is_reported_as_insufficient_evidence() -> None:
    empty = monitor().check(SUBJECT, BASELINE, {})
    payload = degradation_check_payload(
        empty, monitor=monitor(), baseline=BASELINE, recent={}, window=WINDOW
    )
    assert payload["insufficient_evidence"] is True and payload["degraded"] is False
    assert payload["missing"] == ["hit_rate", "max_drawdown", "sharpe"]
    assert all(item["missing"] and item["recent"] is None for item in payload["metrics"])
    body = {key: value for key, value in payload.items() if key != "check_hash"}
    assert payload["check_hash"] == content_hash(body)
    # the key is additive: only an insufficient-evidence check carries it
    assert "insufficient_evidence" not in _payload()


def test_an_insufficient_evidence_check_is_served_by_the_store(tmp_path: Path) -> None:
    empty = monitor().check(SUBJECT, BASELINE, {})
    written = write_degradation_check(
        tmp_path, empty, monitor=monitor(), baseline=BASELINE, recent={}, window=WINDOW
    )
    envelope = ReportStore(tmp_path).get(ReportKind.DEGRADATION_CHECK, written.id)
    assert envelope.payload["insufficient_evidence"] is True
    # the console fixture builder writes exactly this report
    assert not write_insufficient_evidence_fixture(tmp_path).written


def test_written_under_its_hash_and_served_by_the_api(tmp_path: Path) -> None:
    written = write_fixture(tmp_path)
    payload = json.loads(written.path.read_text(encoding="utf-8"))
    assert written.path == tmp_path / KIND / f"{payload['check_hash']}.json"
    assert written.id == payload["check_hash"]
    envelope = ReportStore(tmp_path).get(ReportKind.DEGRADATION_CHECK, written.id)
    assert envelope.payload == payload
    listing = TestClient(create_app(reports_root=tmp_path)).get("/reports/degradation_check")
    body = listing.json()
    assert [item["id"] for item in body["reports"]] == [written.id] and body["invalid"] == []
    assert not write_fixture(tmp_path).written  # idempotent
    written.path.write_text(json.dumps({**payload, "degraded": False}), encoding="utf-8")
    with pytest.raises(ReportConflict):
        write_fixture(tmp_path)
    [invalid] = ReportStore(tmp_path).listing(ReportKind.DEGRADATION_CHECK).invalid
    assert "check_hash does not match" in invalid.reason


def test_a_check_that_the_monitor_does_not_give_is_refused(tmp_path: Path) -> None:
    other_recent: dict[str, Decimal | float | int] = {
        **RECENT,
        "sharpe": Decimal("1.4"),
    }  # not breached with these metrics
    with pytest.raises(ValueError, match="not what this monitor gives"):
        write_degradation_check(
            tmp_path, check(), monitor=monitor(), baseline=BASELINE, recent=other_recent, window="w"
        )
    stricter = DegradationMonitor({"sharpe": Decimal("0.8")}, source=SOURCE)
    with pytest.raises(ValueError, match="not what this monitor gives"):
        write_degradation_check(
            tmp_path, check(), monitor=stricter, baseline=BASELINE, recent=RECENT, window="w"
        )
    assert not (tmp_path / KIND).exists()


@pytest.mark.parametrize("window", ["", "   "])
def test_the_window_must_be_named(tmp_path: Path, window: str) -> None:
    with pytest.raises(ValueError, match="window"):
        write_degradation_check(
            tmp_path, check(), monitor=monitor(), baseline=BASELINE, recent=RECENT, window=window
        )


def test_only_a_degradation_check_and_its_monitor_are_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DegradationCheck"):
        write_degradation_check(
            tmp_path,
            object(),  # type: ignore[arg-type]
            monitor=monitor(),
            baseline=BASELINE,
            recent=RECENT,
            window=WINDOW,
        )
