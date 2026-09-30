"""Test-only trusted runtime factory for the production Worker host subprocess tests."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from apps.worker.jobs import JobRunner
from infrastructure.event_bus.file import FileEventBus


@contextmanager
def build_runtime() -> Iterator[JobRunner]:
    bus_root = Path(os.environ["HLENS_TEST_WORKER_BUS"])
    results = Path(os.environ["HLENS_TEST_WORKER_RESULTS"])
    effects = Path(os.environ["HLENS_TEST_WORKER_EFFECTS"])
    ready = Path(os.environ["HLENS_TEST_WORKER_READY"])

    def handle(params: Mapping[str, Any]) -> dict[str, object]:
        with effects.open("a", encoding="utf-8") as stream:
            stream.write(str(params["value"]) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return {"accepted": True}

    with FileEventBus(bus_root) as bus:
        ready.write_text("ready\n", encoding="utf-8")
        yield JobRunner(
            bus,
            consumer="runtime-test",
            topic="worker.test",
            handlers={"test.effect": handle},
            results=results,
        )
