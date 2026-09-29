import json
import shutil
import signal
import subprocess
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


def test_interruption_kills_and_reaps_active_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                raise probe.ProbeInterrupted(signal.SIGINT)
            return "", ""

    process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(probe.ProbeInterrupted):
        probe._run_child(
            [],
            temp_dir=tmp_path,
            runtime="controlled",
            interval=0.001,
            timeout=1,
            rss_limit_kb=10_000,
        )
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
