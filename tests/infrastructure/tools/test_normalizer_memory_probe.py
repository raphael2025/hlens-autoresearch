import json
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest

from infrastructure.tools import normalizer_memory_probe as probe
from infrastructure.tools.normalizer_memory_probe import (
    DATASET_V3_DAY_END,
    DATASET_V3_DAY_START,
    DATASET_V3_RULE_KEYS,
    DATASET_V3_SOURCE_RUN_BYTES,
    DATASET_V3_SOURCE_RUN_FANOUT,
    DATASET_V3_SOURCE_RUN_RECORDS,
    _dataset_v3_source_params,
    _probe_identity,
    _validate_dataset_v3_config,
)


def test_dataset_v3_window_is_one_full_utc_day() -> None:
    assert DATASET_V3_DAY_START.tzinfo is UTC
    assert DATASET_V3_DAY_END.tzinfo is UTC
    assert DATASET_V3_DAY_END - DATASET_V3_DAY_START == timedelta(days=1)
    assert DATASET_V3_DAY_START.hour == 0
    assert DATASET_V3_DAY_END.hour == 0


def test_dataset_v3_requires_all_dq9_values_and_does_not_default() -> None:
    assert len(DATASET_V3_RULE_KEYS) == 4
    with pytest.raises(ValueError, match="missing="):
        _validate_dataset_v3_config({})
    with pytest.raises(ValueError, match="extra="):
        _validate_dataset_v3_config(
            {
                "chunk_rows": 1,
                "leaf_max_records": 1,
                "leaf_max_bytes": 64,
                "fanout": 2,
                "unapproved": 1,
            }
        )
    with pytest.raises(ValueError, match="positive integers"):
        _validate_dataset_v3_config(
            {"chunk_rows": True, "leaf_max_records": 1, "leaf_max_bytes": 64, "fanout": 2}
        )


def test_dataset_v3_explicit_rule_values_are_returned_unchanged() -> None:
    supplied = {"chunk_rows": 1, "leaf_max_records": 1, "leaf_max_bytes": 64, "fanout": 2}
    assert _validate_dataset_v3_config(supplied) == supplied


def test_dataset_v3_upstream_run_bounds_are_explicit_and_separately_reportable() -> None:
    pit, universe = _dataset_v3_source_params(microbatch=256)
    assert pit.row_batch_rows == 256
    assert pit.edge_batch_rows == 256
    assert pit.key_history_buffer == 256
    assert pit.limits.leaf_max_records == DATASET_V3_SOURCE_RUN_RECORDS
    assert pit.limits.leaf_max_bytes == DATASET_V3_SOURCE_RUN_BYTES
    assert pit.limits.fanout == DATASET_V3_SOURCE_RUN_FANOUT
    assert universe.capacity == DATASET_V3_SOURCE_RUN_RECORDS
    assert universe.merge_fanout == DATASET_V3_SOURCE_RUN_FANOUT


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("", "clean"),
        (" M infrastructure/tools/normalizer_memory_probe.py", "dirty"),
        (None, "unknown"),
    ],
)
def test_probe_identity_reports_sha_and_head_state(status: str | None, expected: str) -> None:
    identity = _probe_identity("abc123", status)
    assert identity["sha256"] == "abc123"
    assert identity["relative_to_head"] == expected
    assert identity["matches_head"] is (expected == "clean")


@pytest.mark.parametrize("missing", DATASET_V3_RULE_KEYS)
def test_dataset_v3_cli_rejects_each_missing_dq9_value(tmp_path: Path, missing: str) -> None:
    values = {
        "chunk_rows": 1,
        "leaf_max_records": 1,
        "leaf_max_bytes": 64,
        "fanout": 2,
    }
    args = ["--dataset-v3", "--rows", "1", "2", "3", "--base", str(tmp_path)]
    args.extend(
        f"--dataset-{key.replace('_', '-')}={value}"
        for key, value in values.items()
        if key != missing
    )
    with pytest.raises(SystemExit) as exc:
        probe.main(args)
    assert exc.value.code == 2


def test_dataset_v3_cli_parses_all_explicit_values_without_starting_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_probe(sizes: list[int], config: dict[str, int], **kwargs: Any) -> dict[str, Any]:
        captured.update(sizes=sizes, config=config, **kwargs)
        return {"status": "complete", "capacity_verdict": "PASS", "e1_cap1_evidence": False}

    monkeypatch.setattr(probe, "run_probe", fake_run_probe)
    monkeypatch.setattr(probe, "_filesystem", lambda _path: "ext4")
    exit_code = probe.main(
        [
            "--dataset-v3",
            "--dataset-chunk-rows=1",
            "--dataset-leaf-max-records=1",
            "--dataset-leaf-max-bytes=64",
            "--dataset-fanout=2",
            "--rows",
            "1",
            "2",
            "3",
            "--base",
            str(tmp_path),
        ]
    )
    assert exit_code == 3
    assert captured["dataset_v3_config"] == {
        "chunk_rows": 1,
        "leaf_max_records": 1,
        "leaf_max_bytes": 64,
        "fanout": 2,
    }


def test_dataset_v3_cli_rejects_dq9_values_without_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        probe.main(["--dataset-chunk-rows=1", "--rows", "1", "2", "3", "--base", str(tmp_path)])
    assert exc.value.code == 2


def _stub_probe_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe,
        "_code_line",
        lambda: {"matches_main": False, "probe_source": {"matches_head": False}},
    )
    monkeypatch.setattr(probe, "_environment", lambda _base, _runtime: {})
    monkeypatch.setattr(probe, "_filesystem", lambda _path: "ext4")


def test_interrupted_run_is_persisted_as_error_and_not_capacity_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_probe_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_run_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(probe.ProbeInterrupted(signal.SIGTERM)),
    )
    json_out = tmp_path / "interrupted.json"
    exit_code = probe.main(
        ["--rows", "1", "2", "3", "--base", str(tmp_path), "--json-out", str(json_out)]
    )
    assert exit_code == 128 + signal.SIGTERM
    assert json.loads(json_out.read_text())["status"] == "interrupted"
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "interrupted"
    assert output["capacity_verdict"] == "ERROR"
    assert output["e1_cap1_evidence"] is False


def test_dataset_child_failure_preserves_original_error_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_probe_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_run_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            probe.ProbeError("original setup child failure", {"stage": "setup"})
        ),
    )
    json_out = tmp_path / "dataset-child-error.json"
    exit_code = probe.main(
        [
            "--dataset-v3",
            "--dataset-chunk-rows=1",
            "--dataset-leaf-max-records=1",
            "--dataset-leaf-max-bytes=64",
            "--dataset-fanout=2",
            "--rows",
            "1",
            "2",
            "3",
            "--base",
            str(tmp_path),
            "--json-out",
            str(json_out),
        ]
    )
    assert exit_code == 4
    persisted = json.loads(json_out.read_text())
    output = json.loads(capsys.readouterr().out)
    for report in (persisted, output):
        assert report["status"] == "error"
        assert report["capacity_verdict"] == "ERROR"
        assert report["error"]["type"] == "ProbeError"
        assert report["error"]["message"] == "original setup child failure"
        assert report["error"]["stage"] == "setup"
        assert report["dataset_v3_measurement"]["status"] == "failed"


def test_initialization_interruption_is_persisted_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_probe_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_environment",
        lambda *_args: (_ for _ in ()).throw(probe.ProbeInterrupted(signal.SIGTERM)),
    )
    json_out = tmp_path / "initialization-interrupted.json"
    exit_code = probe.main(
        ["--rows", "1", "2", "3", "--base", str(tmp_path), "--json-out", str(json_out)]
    )
    assert exit_code == 128 + signal.SIGTERM
    persisted = json.loads(json_out.read_text())
    assert persisted["status"] == "interrupted"
    assert persisted["capacity_verdict"] == "ERROR"
    assert persisted["error"]["signal_name"] == "SIGTERM"
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "interrupted"


def test_interruption_kills_and_reaps_active_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampler_waiting = threading.Event()
    event_wait = threading.Event.wait

    def observe_sampler_wait(event: threading.Event, timeout: float | None = None) -> bool:
        if timeout == 2:
            sampler_waiting.set()
        return event_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", observe_sampler_wait)

    class FakeProcess:
        pid = 987654321

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_called = False
            self.communicate_calls = 0

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.kill_called = True
            self.returncode = -signal.SIGKILL

        def communicate(self) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                assert sampler_waiting.wait(1), "sampler did not enter its interruptible wait"
                raise probe.ProbeInterrupted(signal.SIGINT)
            return "", ""

    process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    started = time.monotonic()
    with pytest.raises(probe.ProbeInterrupted):
        probe._run_child(
            [],
            temp_dir=tmp_path,
            runtime="controlled",
            interval=2,
            timeout=1,
            rss_limit_kb=10_000,
        )
    assert time.monotonic() - started < 1
    assert process.kill_called
    assert process.communicate_calls == 2


def test_cleanup_failure_is_reported_without_overriding_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probe_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_run_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(probe.ProbeInterrupted(signal.SIGTERM)),
    )

    def fail_cleanup(_path: Path) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    report = probe.run_probe(
        [1, 2, 3],
        {"microbatch": 256, "d2_batch": 4096},
        base=tmp_path,
        repeats=1,
    )
    assert report["status"] == "interrupted"
    assert report["capacity_verdict"] == "ERROR"
    assert len(report["cleanup_warnings"]) == 1
    assert report["cleanup_warnings"][0]["type"] == "OSError"
    assert report["cleanup_warnings"][0]["message"] == "injected cleanup failure"


def test_keep_workdirs_skips_cleanup_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probe_environment(monkeypatch)
    monkeypatch.setattr(
        probe,
        "_run_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(probe.ProbeInterrupted(signal.SIGTERM)),
    )
    cleanup_called = False

    def cleanup(_path: Path) -> None:
        nonlocal cleanup_called
        cleanup_called = True

    monkeypatch.setattr(shutil, "rmtree", cleanup)
    report = probe.run_probe(
        [1, 2, 3],
        {"microbatch": 256, "d2_batch": 4096},
        base=tmp_path,
        repeats=1,
        keep_workdirs=True,
    )
    assert report["status"] == "interrupted"
    assert report["cleanup_warnings"] == []
    assert not cleanup_called


def test_v3_dataset_is_prepared_and_built_in_distinct_child_processes(tmp_path: Path) -> None:
    # Keep this child-process wiring check small; protocol-size capacity runs are separate.
    rows = 16
    config = {"microbatch": 8, "d2_batch": 16}
    workdir = tmp_path / "dataset-v3"
    workdir.mkdir()

    setup = probe._run_child(
        probe._child_args(
            "dataset_v3_setup", workdir, rows, config, None, staged_diagnostics=False
        ),
        temp_dir=workdir,
        runtime="default",
        interval=0.01,
        # Building the local SQLite/Iceberg listing and quality fixtures on ext4 can exceed
        # two minutes on slower CI / WSL disks; this is a fixture timeout, not a capacity gate.
        timeout=300,
        rss_limit_kb=2 * 1024 * 1024,
    )
    assert setup.returncode == 0, setup.stderr_tail
    assert "dataset_setup" in setup.by_event
    assert setup.by_event["dataset_setup"]["canonical_rows"] == rows

    build = probe._run_child(
        probe._child_args(
            "dataset_v3_build",
            workdir,
            rows,
            config,
            setup.by_event["dataset_setup"]["unit"],
            staged_diagnostics=False,
        ),
        temp_dir=workdir,
        runtime="default",
        interval=0.01,
        timeout=300,
        rss_limit_kb=2 * 1024 * 1024,
    )
    assert build.returncode == 0, build.stderr_tail
    end = build.by_event["end"]
    assert end["stage"] == "dataset_v3_build"
    assert end["hold_seconds"] == probe._HOLD_SECONDS
    assert end["facts"]["row_count"] > 0
    assert end["facts"]["row_count"] == rows
    assert end["facts"]["expected_row_count"] == rows
    assert end["facts"]["chunk_count"] > 0
    assert end["facts"]["diagnostic_only"] is True
    assert end["facts"]["dq9_parameters"] == probe._DATASET_V3_DIAGNOSTIC_RULE


def test_dq9_diagnostic_parameters_cannot_qualify_as_e1_evidence() -> None:
    assert "dataset_v3_build" in probe.STAGES
    assert probe._DATASET_V3_DQ9_ACCEPTED is False
    assert probe._DATASET_V3_DIAGNOSTIC_RULE == {
        "chunk_rows": probe.PROTOCOL_MICROBATCH,
        "leaf_max_records": 1_000,
        "leaf_max_bytes": 1 << 20,
        "fanout": 4,
    }
    assert probe._DATASET_V3_DIAGNOSTIC_PIT["row_batch_rows"] == probe.PROTOCOL_MICROBATCH
    assert probe._DATASET_V3_DIAGNOSTIC_PIT["edge_batch_rows"] == probe.PROTOCOL_MICROBATCH
    assert probe._DATASET_V3_DIAGNOSTIC_UNIVERSE["capacity"] == probe.PROTOCOL_MICROBATCH
    assert probe._DATASET_V3_DIAGNOSTIC_UNIVERSE["merge_fanout"] == 4


def test_prefill_units_before_2025_use_the_archive_millisecond_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0108 §9(e) prefill: a unit dated before 2025-01-01 is a millisecond archive (parser
    1.0.0 unit rule); microsecond ticks there were rejected at the 152nd unit of K = 1000."""
    from datetime import date

    monkeypatch.setattr(probe, "PROBE_DAY", date(2025, 1, 2))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    workdir = tmp_path / "prefill"
    workdir.mkdir()
    built = probe._prefill(workdir, 2, 256, 4096)  # 2025-01-01 (µs) and 2024-12-31 (ms)
    assert built["units"] == 2 and built["unit_rows"] == probe.PREFILL_UNIT_ROWS
    assert all(count >= 2 for count in built["snapshots"].values())
