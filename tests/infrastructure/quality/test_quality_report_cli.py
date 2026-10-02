"""``python -m infrastructure.quality.report_cli`` (ADR-0101 §6): v3 Quality reports whose every
limit comes from the ``DatasetBuildProfile``; SQLite catalog, no DSN connected, no network."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
)
from infrastructure.dataset.factory import QUALITY_SCRATCH_DIRNAME
from infrastructure.dataset.profile import load_dataset_profile
from infrastructure.quality import report_cli
from infrastructure.quality.listing_report_v2 import ListingHistoryQualityReporterV2
from infrastructure.quality.report_v3 import QualityReporterV3
from infrastructure.tools import cli_support
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.dataset.entry_support import (
    listing_profile_document,
    make_profile,
    profile_document,
    settings_for,
    write_profile,
)
from tests.infrastructure.tools.upstream_cli_support import (
    assert_no_secret,
    patch_catalog,
    refuse_catalog,
    unreachable_catalog,
)

DAY = date(2023, 11, 14)


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path / "world") as opened:
        yield opened


def _argv(command: str | None, profile: Path, *extra: str) -> list[str]:
    head = [] if command is None else [command]
    return [
        *head,
        "--profile",
        str(profile),
        "--data-type",
        "agg_trades",
        "--symbol",
        "BTCUSDT",
        "--day",
        DAY.isoformat(),
        *extra,
    ]


def _run(
    capsys: pytest.CaptureFixture[str], w: World, argv: list[str]
) -> tuple[int, dict[str, Any] | None, str]:
    code = report_cli.main(argv, settings=settings_for(w))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


def test_without_a_subcommand_the_plan_is_printed_and_nothing_is_opened(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, profile = make_profile(tmp_path)
    refuse_catalog(monkeypatch)
    code, plan, err = _run(capsys, w, _argv(None, path, "--day", "2023-11-13"))
    assert code == 0, err
    assert plan is not None and plan["command"] == "plan" and plan["network"] is False
    assert plan["profile_hash"] == profile.profile_hash()
    assert plan["capacity_evidence"] == "none"
    assert plan["partitions"] == [
        {"symbol": "BTCUSDT", "day": "2023-11-13"},
        {"symbol": "BTCUSDT", "day": "2023-11-14"},
    ]


def test_report_then_verify_one_partition_with_profile_limits(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w.listed()
    w.trades()
    path, profile = make_profile(tmp_path, capacity_evidence="capacity-run-x")
    opened = patch_catalog(monkeypatch, w.h.adapter)
    seen: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> QualityReporterV3:
        seen.append(kwargs)
        return QualityReporterV3(*args, **kwargs)

    monkeypatch.setattr(report_cli, "QualityReporterV3", spy)
    code, reported, err = _run(capsys, w, _argv("report", path))
    assert code == 0, err
    assert reported is not None and reported["command"] == "report"
    assert reported["capacity_evidence"] == "capacity-run-x"
    (report,) = reported["reports"]
    assert report["symbol"] == "BTCUSDT" and report["day"] == DAY.isoformat()
    assert not report["reused"] and report["record_counts"]["events"] > 0
    assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) is not None

    code, verified, err = _run(capsys, w, _argv("verify", path))
    assert code == 0, err
    assert verified is not None and verified["reports"][0]["report_id"] == report["report_id"]
    assert verified["reports"][0]["reused"]
    assert len(opened) == 2

    quality, reporter = profile.quality, profile.quality.reporter
    expected = {
        "pit_params": profile.pit,
        "run_capacity": quality.run_capacity,
        "merge_fanout": quality.merge_fanout,
        "run_limits": quality.run_limits,
        "stream_limits": quality.stream_limits,
        "max_event_record_bytes": reporter.max_event_record_bytes,
        "max_revision_record_bytes": reporter.max_revision_record_bytes,
        "max_gap_record_bytes": reporter.max_gap_record_bytes,
        "max_input_record_bytes": reporter.max_input_record_bytes,
        "max_manifest_record_bytes": reporter.max_manifest_record_bytes,
        "max_identity_bytes": quality.max_identity_bytes,
        "retries": reporter.retries,
    }
    for kwargs in seen:
        assert {name: kwargs[name] for name in expected} == expected
        scratch_root = w.h.canonical_scratch_directory / QUALITY_SCRATCH_DIRNAME
        assert kwargs["scratch_storage"].warehouse_uri == (scratch_root / "warehouse").as_uri()


def test_another_profile_changes_the_limits_handed_to_the_reporter(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = profile_document()
    document["quality"]["reporter"]["retries"] = 3
    path = write_profile(tmp_path / "other.json", document)
    patch_catalog(monkeypatch, w.h.adapter)
    seen: list[int] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["retries"])
        raise RuntimeError("stop here")

    monkeypatch.setattr(report_cli, "QualityReporterV3", spy)
    code, _, err = _run(capsys, w, _argv("report", path))
    assert seen == [3] and code == cli_support.EXIT_FAILED
    assert err.strip() == "error: report failed (RuntimeError)"


def test_verify_of_a_partition_without_a_report_fails(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w.listed()
    w.trades()
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, w.h.adapter)
    code, out, err = _run(capsys, w, _argv("verify", path))
    assert code == cli_support.EXIT_FAILED and out is None
    assert "no committed v3 quality report" in err
    assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) is None


def test_an_invalid_profile_exits_3(
    w: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = profile_document()
    del document["quality"]
    path = write_profile(tmp_path / "bad.json", document)
    code, out, err = _run(capsys, w, _argv("report", path))
    assert code == cli_support.EXIT_PROFILE and out is None
    assert err.startswith("error: invalid profile:")


def test_an_unreachable_catalog_exits_4_with_the_dsn_redacted(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = make_profile(tmp_path)
    unreachable_catalog(monkeypatch)
    code, out, err = _run(capsys, w, _argv("report", path))
    assert code == cli_support.EXIT_ENVIRONMENT and out is None
    assert "<redacted>" in err
    assert_no_secret(err)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--symbol", "DOGEUSDT"), ("--data-type", "trades_raw"), ("--day", "2023-11-14T00:00")],
)
def test_bad_arguments_exit_2(flag: str, value: str, tmp_path: Path) -> None:
    argv = _argv("report", tmp_path / "p.json")
    argv[argv.index(flag) + 1] = value
    with pytest.raises(SystemExit) as exit_:
        report_cli.main(argv)
    assert exit_.value.code == cli_support.EXIT_USAGE


# ------------------------------------------------------------------------- listing (修订 1 §2)


def _listing_profile(tmp_path: Path) -> Path:
    return write_profile(tmp_path / "listing-profile.json", listing_profile_document())


@pytest.mark.parametrize("command", ["listing-plan", "listing-report", "listing-verify"])
def test_listing_commands_refuse_a_profile_without_the_listing_section(
    command: str,
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = make_profile(tmp_path)
    refuse_catalog(monkeypatch)
    code, out, err = _run(capsys, w, [command, "--profile", str(path)])
    assert code == cli_support.EXIT_PROFILE and out is None
    assert "listing_quality" in err


def test_listing_plan_opens_nothing(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _listing_profile(tmp_path)
    refuse_catalog(monkeypatch)
    code, plan, err = _run(capsys, w, ["listing-plan", "--profile", str(path)])
    assert code == 0, err
    assert plan is not None and plan["command"] == "listing-plan" and plan["network"] is False
    assert plan["tables"] == [CANONICAL_INSTRUMENT_LISTINGS.table, BINANCE_SPOT_EXCHANGE_INFO.table]


def test_listing_report_then_verify_with_profile_limits(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w.listed()
    path = _listing_profile(tmp_path)
    opened = patch_catalog(monkeypatch, w.h.adapter)
    seen: list[dict[str, Any]] = []

    def spy(*args: Any, **kwargs: Any) -> ListingHistoryQualityReporterV2:
        seen.append(kwargs)
        return ListingHistoryQualityReporterV2(*args, **kwargs)

    monkeypatch.setattr(report_cli, "ListingHistoryQualityReporterV2", spy)
    code, reported, err = _run(capsys, w, ["listing-report", "--profile", str(path)])
    assert code == 0, err
    assert reported is not None and reported["command"] == "listing-report"
    assert not reported["reused"]
    assert reported["snapshots"] == {
        CANONICAL_INSTRUMENT_LISTINGS.table: w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table),
        BINANCE_SPOT_EXCHANGE_INFO.table: w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table),
    }
    assert w.h.head(DATA_QUALITY_REPORT_MANIFESTS.table) is not None

    code, verified, err = _run(capsys, w, ["listing-verify", "--profile", str(path)])
    assert code == 0, err
    assert verified is not None and verified["report_id"] == reported["report_id"]
    assert verified["reused"] and verified["record_counts"] == reported["record_counts"]
    assert len(opened) == 2

    profile = load_dataset_profile(path)
    listing = profile.listing_quality
    assert listing is not None
    quality, reporter = profile.quality, profile.quality.reporter
    expected = {
        "market_data_base_url": ds.ORIGIN,
        "metadata_limits": listing.metadata_limits,
        "capacity": quality.run_capacity,
        "merge_fanout": quality.merge_fanout,
        "run_limits": quality.run_limits,
        "stream_limits": quality.stream_limits,
        "max_record_bytes": listing.max_record_bytes,
        "max_run_object_bytes": quality.max_run_object_bytes,
        "prefix_leaf_max_records": listing.prefix_leaf_max_records,
        "prefix_fanout": listing.prefix_fanout,
        "prefix_max_node_bytes": listing.prefix_max_node_bytes,
        "prefix_max_record_bytes": listing.prefix_max_record_bytes,
        "row_chunk_capacity": listing.row_chunk_capacity,
        "max_hash_chunk_bytes": listing.max_hash_chunk_bytes,
        "max_event_record_bytes": reporter.max_event_record_bytes,
        "max_revision_record_bytes": reporter.max_revision_record_bytes,
        "max_gap_record_bytes": reporter.max_gap_record_bytes,
        "max_manifest_record_bytes": reporter.max_manifest_record_bytes,
        "max_identity_bytes": quality.max_identity_bytes,
        "retries": reporter.retries,
    }
    assert len(seen) == 2
    for kwargs in seen:
        assert {name: kwargs[name] for name in expected} == expected
        scratch_root = w.h.canonical_scratch_directory / QUALITY_SCRATCH_DIRNAME
        assert kwargs["scratch_storage"].warehouse_uri == (scratch_root / "warehouse").as_uri()


def test_listing_verify_without_a_report_fails(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w.listed()
    path = _listing_profile(tmp_path)
    patch_catalog(monkeypatch, w.h.adapter)
    code, out, err = _run(capsys, w, ["listing-verify", "--profile", str(path)])
    assert code == cli_support.EXIT_FAILED and out is None
    assert "ListingHistoryQualityReportMissing" in err


def test_listing_report_without_listing_history_fails(
    w: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _listing_profile(tmp_path)
    patch_catalog(monkeypatch, w.h.adapter)
    code, out, err = _run(capsys, w, ["listing-report", "--profile", str(path)])
    assert code == cli_support.EXIT_FAILED and out is None
    assert "has no committed snapshot" in err
