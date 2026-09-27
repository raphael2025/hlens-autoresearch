"""Read-only job endpoints over the worker's results journal (ADR-0048 read-only console; ADR-0044
durable results; 2026-09-26, CODE_COMPLETE / DEBUG_PENDING).

A real ``JobRunner`` writes a hash-chained results journal in ``tmp_path``; ``GET /jobs`` and
``GET /jobs/{job_id}`` read it through ``apps.worker.jobs.read_job_results`` (the runner's own
replay). A tampered or invalid journal is an explicit 500, never partial data.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.app import JOBS_NOT_CONFIGURED
from apps.worker import AppendOnlyJournal, JobResultsCorrupted, JobRunner, JobSpec
from apps.worker.jobs import (
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_RESULT,
    JOB_STARTED,
    JOB_SUCCEEDED,
    read_job_results,
)
from apps.worker.journal import GENESIS_HASH, JournalCorrupted
from infrastructure.event_bus import InMemoryEventBus


class Crash(BaseException):
    """A process death inside a handler (not an ``Exception``: no retry sees it)."""


def _boom(_: Mapping[str, Any]) -> None:
    raise ValueError("bad input")


def _runner(bus: InMemoryEventBus, path: Path, handlers: Mapping[str, Any], **kw: Any) -> JobRunner:
    return JobRunner(bus, consumer="w", topic="jobs", handlers=handlers, results=path, **kw)


def _two_jobs(path: Path) -> tuple[str, str]:
    """One succeeded job, then one that failed after two attempts."""
    bus = InMemoryEventBus()
    runner = _runner(
        bus, path, {"double": lambda p: {"doubled": int(p["x"]) * 2}, "boom": _boom}, max_attempts=2
    )
    ok = runner.submit(JobSpec("double", {"x": 21}))
    bad = runner.submit(JobSpec("boom", {}))
    runner.run_pending()
    return ok, bad


# --- apps.worker.jobs.read_job_results --------------------------------------------------


def test_the_reader_agrees_with_the_runner_and_never_writes(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    ok, bad = _two_jobs(results)
    before = results.read_bytes()
    history = read_job_results(results)
    assert results.read_bytes() == before
    assert [job.job_id for job in history.jobs] == [ok, bad]
    assert history.lines == 4 and history.head_hash == AppendOnlyJournal(results).head_hash
    first, second = history.jobs
    assert first.status == JOB_SUCCEEDED and first.params == {"x": 21}
    assert first.outcome is not None and first.outcome.result == {"doubled": 42}
    assert second.status == JOB_FAILED and second.outcome is not None
    assert second.outcome.attempts == 2 and second.outcome.error == "ValueError: bad input"
    reopened = _runner(InMemoryEventBus(), results, {"double": lambda p: 0, "boom": _boom})
    assert {job.job_id: job.outcome for job in history.jobs} == dict(reopened.outcomes)


def test_a_missing_journal_reads_as_empty_and_is_not_created(tmp_path: Path) -> None:
    history = read_job_results(tmp_path / "absent.jsonl")
    assert history.jobs == () and history.lines == 0 and history.head_hash == GENESIS_HASH
    assert not (tmp_path / "absent.jsonl").exists()


def test_the_reader_refuses_tampered_and_forged_journals(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    _two_jobs(results)
    lines = results.read_text(encoding="utf-8").splitlines(keepends=True)
    results.write_text(lines[0] + lines[1].replace('"doubled":42', '"doubled":43'), "utf-8")
    with pytest.raises(JournalCorrupted):
        read_job_results(results)
    forged = AppendOnlyJournal(tmp_path / "forged.jsonl")  # correctly chained, invalid history
    job = JobSpec("double", {"x": 1})
    forged.append(JOB_RESULT, {"job_id": job.job_id, "name": "double", "succeeded": True,
                               "attempts": 1, "result": 2, "error": None})  # fmt: skip
    with pytest.raises(JobResultsCorrupted):
        read_job_results(forged.path)
    with pytest.raises(TypeError, match="not one string"):
        read_job_results(results, idempotent="double")


# --- HTTP ------------------------------------------------------------------------------------


def test_job_endpoints_without_a_journal_are_a_503() -> None:
    client = TestClient(create_app())
    for url in ("/jobs", "/jobs/" + "0" * 64):
        response = client.get(url)
        assert response.status_code == 503
        assert response.json() == {"detail": JOBS_NOT_CONFIGURED}


def test_job_endpoints_serve_status_attempts_result_and_failure(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    ok, bad = _two_jobs(results)
    client = TestClient(create_app(jobs_results=results))
    listing = client.get("/jobs")
    assert listing.status_code == 200
    body = listing.json()
    assert body["lines"] == 4 and body["head_hash"] == AppendOnlyJournal(results).head_hash
    assert [job["job_id"] for job in body["jobs"]] == [ok, bad]
    first, second = body["jobs"]
    assert first == {
        "job_id": ok,
        "name": "double",
        "params": {"x": 21},
        "status": "succeeded",
        "attempts": 1,
        "result": {"doubled": 42},
        "error": None,
        "starts": 1,
        "reruns": 0,
        "first_seq": 1,
        "last_seq": 2,
    }
    assert second["status"] == "failed" and second["attempts"] == 2
    assert second["error"] == "ValueError: bad input" and second["result"] is None
    assert client.get(f"/jobs/{bad}").json() == second


def test_an_interrupted_job_is_reported_as_interrupted(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"

    def die(_: Mapping[str, Any]) -> None:
        raise Crash

    runner = _runner(InMemoryEventBus(), results, {"work": die})
    job_id = runner.submit(JobSpec("work", {"id": "a"}))
    with pytest.raises(Crash):
        runner.run_pending()
    job = TestClient(create_app(jobs_results=results)).get(f"/jobs/{job_id}").json()
    assert job["status"] == JOB_INTERRUPTED
    schemas = create_app().openapi()["components"]["schemas"]
    status = schemas["JobView"]["properties"]["status"]
    enum = schemas[status["$ref"].rsplit("/", 1)[-1]] if "$ref" in status else status
    assert set(enum["enum"]) == {JOB_SUCCEEDED, JOB_FAILED, JOB_INTERRUPTED}
    assert job["attempts"] is None and job["result"] is None and job["error"] is None
    assert job["starts"] == 1 and job["reruns"] == 0


def test_a_rerun_needs_the_same_idempotent_declaration_as_the_runner(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    crash = {"left": 1}

    def work(_: Mapping[str, Any]) -> str:
        if crash["left"]:
            crash["left"] -= 1
            raise Crash
        return "done"

    bus = InMemoryEventBus()
    job_id = _runner(bus, results, {"work": work}, idempotent=("work",)).submit(JobSpec("work", {}))
    with pytest.raises(Crash):
        _runner(bus, results, {"work": work}, idempotent=("work",)).run_pending()
    _runner(bus, results, {"work": work}, idempotent=("work",)).run_pending()

    declared = TestClient(create_app(jobs_results=results, jobs_idempotent=("work",)))
    job = declared.get(f"/jobs/{job_id}").json()
    assert job["status"] == "succeeded" and job["starts"] == 2 and job["reruns"] == 1
    undeclared = TestClient(create_app(jobs_results=results))  # refused, as by the runner
    response = undeclared.get("/jobs")
    assert response.status_code == 500 and "not declared idempotent" in response.json()["detail"]
    with pytest.raises(TypeError, match="not one string"):
        create_app(jobs_results=results, jobs_idempotent="work")


def test_a_tampered_journal_is_an_explicit_error_never_partial_data(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    ok, bad = _two_jobs(results)
    lines = results.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[3].count('"attempts":2') == 1
    results.write_text("".join(lines[:3]) + lines[3].replace('"attempts":2', '"attempts":1'))
    client = TestClient(create_app(jobs_results=results))
    for url in ("/jobs", f"/jobs/{ok}", f"/jobs/{bad}"):
        response = client.get(url)
        assert response.status_code == 500
        body = response.json()
        assert set(body) == {"detail"}
        assert body["detail"].startswith("job results journal failed verification")


def test_a_partial_trailing_line_is_refused_too(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    _two_jobs(results)
    with results.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 5')
    response = TestClient(create_app(jobs_results=results)).get("/jobs")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "partial trailing line" in detail and "results.jsonl" in detail
    assert str(tmp_path) not in detail  # the reason, never the server's filesystem path


def test_job_detail_rejects_bad_ids_and_reports_unknown_ones(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    _two_jobs(results)
    client = TestClient(create_app(jobs_results=results))
    for bad_id in ("abc", "0" * 63, "G" * 64, "0" * 64 + "0"):
        assert client.get(f"/jobs/{bad_id}").status_code == 400
    missing = client.get("/jobs/" + "f" * 64)
    assert missing.status_code == 404 and "not found" in missing.json()["detail"]


def test_a_configured_but_absent_journal_lists_no_jobs(tmp_path: Path) -> None:
    client = TestClient(create_app(jobs_results=tmp_path / "none.jsonl"))
    assert client.get("/jobs").json() == {"jobs": [], "head_hash": GENESIS_HASH, "lines": 0}
    assert client.get("/jobs/" + "0" * 64).status_code == 404
    assert not (tmp_path / "none.jsonl").exists()


def test_job_started_lines_alone_are_interrupted_jobs(tmp_path: Path) -> None:
    journal = AppendOnlyJournal(tmp_path / "r.jsonl")
    job = JobSpec("x", {"k": 1})
    journal.append(JOB_STARTED, {"job_id": job.job_id, "name": "x", "params": {"k": 1}})
    [record] = read_job_results(journal.path).jobs
    assert record.status == JOB_INTERRUPTED and record.outcome is None
