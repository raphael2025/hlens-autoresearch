"""Capacity probe tool: JSON summary shape and the memory-safety gate (G3-T)."""

from __future__ import annotations

import json

import pytest

from infrastructure.tools import capacity_probe as probe

STAGES = ("ingest_archive", "normalize", "pit_select_1h", "quality_report_day")
MEASUREMENT_KEYS = ("wall_seconds", "tracemalloc_peak_mb", "ru_maxrss_delta_mb")


def test_probe_runs_200_rows_and_prints_a_well_shaped_json_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = probe.main(["--rows", "200"])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == 200
    assert summary["symbol"] == "BTCUSDT"
    assert summary["data_type"] == "agg_trades"
    assert summary["day"] == "2025-06-01"
    assert set(summary["stages"]) == set(STAGES)
    for stage in STAGES:
        body = summary["stages"][stage]
        for key in MEASUREMENT_KEYS:
            assert key in body
            assert isinstance(body[key], int | float)
            assert body[key] >= 0
    assert summary["stages"]["ingest_archive"]["row_count"] == 200
    assert summary["stages"]["normalize"]["canonical_rows"] == 200
    assert summary["stages"]["pit_select_1h"]["keys_selected"] >= 1
    assert (
        summary["stages"]["pit_select_1h"]["keys_evaluated"]
        >= summary["stages"]["pit_select_1h"]["keys_selected"]
    )
    assert summary["stages"]["quality_report_day"]["event_count"] >= 0
    assert summary["stages"]["quality_report_day"]["reused"] is False
    assert isinstance(summary["notes"], list)
    assert summary["notes"]


def test_probe_refuses_more_than_50000_rows_without_override() -> None:
    with pytest.raises(SystemExit):
        probe.main(["--rows", "50001"])


def test_probe_refuses_fewer_than_one_row() -> None:
    with pytest.raises(SystemExit):
        probe.main(["--rows", "0"])
