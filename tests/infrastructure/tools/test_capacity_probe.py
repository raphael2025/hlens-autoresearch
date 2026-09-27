"""Capacity probe tool: JSON summary shape and the memory-safety gate (G3-T / G3-C)."""

from __future__ import annotations

import json

import pytest

from infrastructure.tools import capacity_probe as probe

STAGES = ("ingest_archive", "normalize", "pit_select_1h", "quality_report_day")
REST_STAGES = ("ingest_rest", "normalize_rest", "channel_reconcile")
DATASET_STAGES = ("universe_build", "dataset_build")
FEATURE_STAGES = ("ingest_klines_archive", "normalize_klines", "feature_bar_log_return")
MEASUREMENT_KEYS = ("wall_seconds", "tracemalloc_peak_mb", "ru_maxrss_delta_mb")


def _check_measurements(stages: dict[str, dict[str, object]], names: tuple[str, ...]) -> None:
    for stage in names:
        body = stages[stage]
        for key in MEASUREMENT_KEYS:
            assert key in body
            value = body[key]
            assert isinstance(value, int | float)
            assert value >= 0


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
    assert summary["flags"] == {"rest": False, "dataset": False, "feature": False}
    assert set(summary["stages"]) == set(STAGES)
    _check_measurements(summary["stages"], STAGES)
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


def test_probe_rest_stage_ingests_and_reconciles_a_rest_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = probe.main(["--rows", "200", "--rest"])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["flags"]["rest"] is True
    assert set(REST_STAGES) <= set(summary["stages"])
    assert not set(DATASET_STAGES) & set(summary["stages"])
    assert not set(FEATURE_STAGES) & set(summary["stages"])
    _check_measurements(summary["stages"], REST_STAGES)

    ingest_rest = summary["stages"]["ingest_rest"]
    assert ingest_rest["page_count"] == 1
    assert ingest_rest["element_count"] == 200
    assert ingest_rest["collection_outcome"] == "succeeded"
    assert summary["stages"]["normalize_rest"]["canonical_rows"] == 200

    reconcile = summary["stages"]["channel_reconcile"]
    # The REST tail describes the same trades as the tail of the archive (same ids / fields), so
    # reconciliation must find every one of them equal and record a genuine precedence edge.
    assert reconcile["edge_count"] == 200
    assert reconcile["new_edge_count"] == 200
    assert reconcile["finding_count"] == 0


def test_probe_dataset_stage_builds_a_research_dataset_and_manifest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = probe.main(["--rows", "200", "--dataset"])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["flags"]["dataset"] is True
    assert set(DATASET_STAGES) <= set(summary["stages"])
    assert not set(REST_STAGES) & set(summary["stages"])
    assert not set(FEATURE_STAGES) & set(summary["stages"])
    _check_measurements(summary["stages"], DATASET_STAGES)

    universe = summary["stages"]["universe_build"]
    # BTCUSDT + ETHUSDT are both observed TRADING in the mock exchangeInfo snapshot.
    assert universe["member_count"] == 2
    assert universe["exclusion_count"] == 0

    built = summary["stages"]["dataset_build"]
    assert built["row_count"] >= 1
    assert built["replayed"] is False
    assert isinstance(built["manifest_content_hash"], str)
    assert len(built["manifest_content_hash"]) == 64


def test_probe_feature_stage_runs_bar_log_return_over_a_klines_archive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = probe.main(["--rows", "200", "--feature"])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["flags"]["feature"] is True
    assert set(FEATURE_STAGES) <= set(summary["stages"])
    assert not set(REST_STAGES) & set(summary["stages"])
    assert not set(DATASET_STAGES) & set(summary["stages"])
    _check_measurements(summary["stages"], FEATURE_STAGES)

    assert summary["stages"]["ingest_klines_archive"]["row_count"] == 200
    assert summary["stages"]["normalize_klines"]["canonical_rows"] == 200

    feature = summary["stages"]["feature_bar_log_return"]
    assert feature["evaluation_count"] == 1
    # Two contiguous bars are visible at the single (far-future) evaluation time.
    assert feature["non_null_count"] == 1


def test_probe_all_optional_stages_combine_in_one_run(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = probe.main(["--rows", "200", "--rest", "--dataset", "--feature"])

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["flags"] == {"rest": True, "dataset": True, "feature": True}
    all_stages = STAGES + REST_STAGES + DATASET_STAGES + FEATURE_STAGES
    assert set(summary["stages"]) == set(all_stages)
    _check_measurements(summary["stages"], all_stages)
    # The dataset build sees both channels' Canonical revisions for the same trades.
    assert summary["stages"]["dataset_build"]["row_count"] >= 1
