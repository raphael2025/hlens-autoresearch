from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from apps.worker.jobs import JobRunner, JobSpec
from apps.worker.serve import load_factory, run
from infrastructure.event_bus.file import FileEventBus
from infrastructure.event_bus.memory import InMemoryEventBus


def test_factory_spec_requires_module_and_callable() -> None:
    with pytest.raises(ValueError, match="MODULE:CALLABLE"):
        load_factory("missing-separator")
    with pytest.raises(ValueError, match="MODULE:CALLABLE"):
        load_factory("tests.apps.worker_runtime_factory:build_runtime:extra")
    with pytest.raises(ModuleNotFoundError):
        load_factory("does_not_exist:build")


def test_signal_set_by_active_job_stops_after_that_job_finishes() -> None:
    bus = InMemoryEventBus()
    stop = threading.Event()

    def handle(_params: Mapping[str, Any]) -> str:
        stop.set()
        return "done"

    runner = JobRunner(
        bus,
        consumer="worker-host-test",
        topic="worker.test",
        handlers={"test.stop": handle},
    )
    runner.submit(JobSpec("test.stop", {}))
    run(runner, stop)
    assert len(runner.outcomes) == 1


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
def test_production_host_stops_and_restarts_without_repeating_completed_job(
    tmp_path: Path, shutdown_signal: signal.Signals
) -> None:
    bus_root = tmp_path / "bus"
    results = tmp_path / "results.jsonl"
    effects = tmp_path / "effects.txt"
    # Publish through the same durable bus before starting the production process.
    with FileEventBus(bus_root) as bus:
        JobRunner(
            bus,
            consumer="publisher",
            topic="worker.test",
            handlers={},
        ).submit(JobSpec("test.effect", {"value": "once"}))

    env = os.environ.copy()
    env.update(
        {
            "HLENS_TEST_WORKER_BUS": str(bus_root),
            "HLENS_TEST_WORKER_RESULTS": str(results),
            "HLENS_TEST_WORKER_EFFECTS": str(effects),
            "HLENS_TEST_WORKER_READY": str(tmp_path / "ready"),
        }
    )
    command = [
        sys.executable,
        "-m",
        "apps.worker.serve",
        "--factory",
        "tests.apps.worker_runtime_factory:build_runtime",
    ]

    first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while not effects.exists() and first.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert effects.exists(), "worker did not execute the submitted job"
    first.send_signal(shutdown_signal)
    stdout, stderr = first.communicate(timeout=10)
    assert first.returncode == 0, (stdout, stderr)
    assert effects.read_text(encoding="utf-8").splitlines() == ["once"]

    (tmp_path / "ready").unlink()
    second = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    ready = tmp_path / "ready"
    while not ready.exists() and second.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), second.communicate(timeout=10)
    second.send_signal(shutdown_signal)
    stdout, stderr = second.communicate(timeout=10)
    assert second.returncode == 0, (stdout, stderr)
    assert effects.read_text(encoding="utf-8").splitlines() == ["once"]
