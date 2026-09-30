"""W2 production Worker restart over PostgreSQL-backed Iceberg and durable job state."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from apps.worker import AppendOnlyJournal, read_job_results
from apps.worker.jobs import JOB_RESULT, JOB_STARTED, JobRunner, JobSpec
from infrastructure.event.iceberg import EventTable
from infrastructure.event.table_definition import EVENT_EVENTS, PHASE3_REGISTRY
from infrastructure.event_bus.file import FileEventBus
from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    postgres_test_catalog_uri,
)

TIMEOUT = 120


def _wait_for(path: Path, process: subprocess.Popen[bytes], *, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not path.exists():
        stdout, stderr = (
            process.communicate(timeout=5) if process.poll() is not None else (b"", b"")
        )
        raise AssertionError(
            f"worker did not reach {path.name}; returncode={process.poll()} "
            f"stdout={stdout!r} stderr={stderr!r}"
        )


def test_production_worker_recovers_postgres_iceberg_job_after_crash_before_ack(
    tmp_path: Path,
) -> None:
    """Commit an Event run, kill after durable job result but before queue ack, then recover.

    The first process is killed at the exact result-before-ack boundary. Its file-backed bus still
    has the message pending, while the job journal and PostgreSQL + Iceberg write are durable. On
    restart, the production host must acknowledge from the stored result without rerunning the
    handler or appending a second Iceberg snapshot.
    """
    uri = postgres_test_catalog_uri()  # skips only when the explicit *_test DSN is absent
    warehouse = tmp_path / "warehouse"
    harness = PostgresCatalogHarness(
        tmp_path,
        uri=uri,
        registry=PHASE3_REGISTRY,
        catalog_name=f"w2_worker_{uuid.uuid4().hex[:12]}",
    )
    bus_root = tmp_path / "bus"
    results = tmp_path / "job-results.jsonl"
    invocations = tmp_path / "handler-invocations.txt"
    ready = tmp_path / "ready"
    ack_entered = tmp_path / "ack-entered"
    ack_completed = tmp_path / "ack-completed"
    command = [
        sys.executable,
        "-m",
        "apps.worker.serve",
        "--factory",
        "tests.apps.worker_postgres_factory:build_runtime",
    ]
    env = os.environ.copy()
    env.update(
        {
            "HLENS_TEST_WORKER_WAREHOUSE": str(warehouse),
            "HLENS_TEST_WORKER_CATALOG_NAME": harness.catalog_name,
            "HLENS_TEST_WORKER_BUS": str(bus_root),
            "HLENS_TEST_WORKER_RESULTS": str(results),
            "HLENS_TEST_WORKER_INVOCATIONS": str(invocations),
            "HLENS_TEST_WORKER_READY": str(ready),
            "HLENS_TEST_WORKER_ACK_ENTERED": str(ack_entered),
            "HLENS_TEST_WORKER_ACK_COMPLETED": str(ack_completed),
            "HLENS_TEST_WORKER_BLOCK_BEFORE_ACK": "1",
        }
    )

    first: subprocess.Popen[bytes] | None = None
    second: subprocess.Popen[bytes] | None = None
    try:
        # A real production JobRunner publishes the durable queue message before the host starts.
        with FileEventBus(bus_root) as bus:
            publisher = JobRunner(
                bus,
                consumer="publisher",
                topic="worker.w2.integration",
                handlers={},
            )
            job_id = publisher.submit(JobSpec("test.persist_event", {"case": "w2-restart"}))

        first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_for(ack_entered, first)
        # Entry into ack proves JobRunner has fsync'd job_result; it has not acked the bus yet.
        before_kill = read_job_results(results)
        [record] = before_kill.jobs
        assert record.job_id == job_id
        assert record.status == "succeeded" and record.outcome is not None
        assert [entry.type for entry in AppendOnlyJournal(results).entries] == [
            JOB_STARTED,
            JOB_RESULT,
        ]
        assert not ack_completed.exists()
        assert invocations.read_text(encoding="utf-8").splitlines() == [
            record.outcome.result["result_hash"]
        ]
        first.kill()
        first.communicate(timeout=TIMEOUT)
        assert first.returncode == -signal.SIGKILL
        durable_journal = results.read_bytes()

        # Recreate the same host runtime and storage. The queue redelivers the unacked message;
        # JobRunner must use its recovered result and the handler must not run a second time.
        ready.unlink()
        env["HLENS_TEST_WORKER_BLOCK_BEFORE_ACK"] = "0"
        second = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_for(ack_completed, second)
        second.send_signal(signal.SIGTERM)
        stdout, stderr = second.communicate(timeout=TIMEOUT)
        assert second.returncode == 0, (stdout, stderr)

        after_restart = read_job_results(results)
        [recovered_record] = after_restart.jobs
        assert recovered_record == record
        assert results.read_bytes() == durable_journal  # replay/ack appended no second result
        assert invocations.read_text(encoding="utf-8").splitlines() == [
            record.outcome.result["result_hash"]
        ]

        adapter = harness.open_adapter()
        try:
            info = adapter.load_table(EVENT_EVENTS.table)
            assert info is not None and info.current_snapshot is not None
            assert info.current_snapshot.snapshot_id == record.outcome.result["snapshot_id"]
            recovered_event = EventTable(adapter).read(record.outcome.result["result_hash"])
            assert recovered_event is not None
            assert recovered_event.result.result_hash == record.outcome.result["result_hash"]
            assert recovered_event.snapshot_id == record.outcome.result["snapshot_id"]
            assert len(recovered_event.rows) == record.outcome.result["row_count"]
        finally:
            adapter.close()
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=TIMEOUT)
        identifier = tuple(EVENT_EVENTS.table.split("."))
        harness.cleanup_owned_tables((identifier,))
