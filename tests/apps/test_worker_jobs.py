"""ADR-0044: the in-memory bus passes its suite; worker jobs are idempotent and retried; durable
results survive a restart (implementation note, durable jobs and bus wiring, 2026-09-26)."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apps.worker import (
    AppendOnlyJournal,
    JobInterrupted,
    JobOutcome,
    JobResultsCorrupted,
    JobRunner,
    JobSpec,
    JournalCorrupted,
)
from apps.worker.jobs import JOB_RESULT, JOB_STARTED
from core.contracts.event_bus import BusMessage
from infrastructure.event_bus import FileEventBus, InMemoryEventBus
from tests.contract_suites import event_bus as suite


@pytest.mark.parametrize("check", suite.BUS_CHECKS, ids=lambda c: c.__name__)
def test_the_memory_bus_passes_the_suite(check: suite.BusCheck) -> None:
    check(InMemoryEventBus())


def test_a_forged_message_id_is_refused() -> None:
    good = BusMessage.build("t", "k", {"a": 1})
    with pytest.raises(ValidationError, match="message_id"):
        BusMessage.model_validate(
            {"topic": "t", "key": "k", "payload": {"a": 2}, "message_id": good.message_id}
        )


def test_jobs_run_once_despite_duplicate_delivery_and_retry_on_failure() -> None:
    bus = InMemoryEventBus()
    calls: list[Mapping[str, Any]] = []
    flaky = {"left": 1}

    def work(params: Mapping[str, Any]) -> int:
        calls.append(params)
        if flaky["left"]:
            flaky["left"] -= 1
            raise RuntimeError("transient")
        return int(params["x"]) * 2

    runner = JobRunner(bus, consumer="w", topic="jobs", handlers={"double": work})
    job = JobSpec("double", {"x": 21})
    runner.submit(job)
    runner.submit(job)  # a duplicate submission
    [outcome] = runner.run_pending()
    assert outcome.succeeded and outcome.result == 42 and outcome.attempts == 2
    assert runner.run_pending() == [] and len(calls) == 2


def test_an_unknown_or_always_failing_job_is_recorded_not_dropped() -> None:
    bus = InMemoryEventBus()

    def boom(_: Mapping[str, Any]) -> None:
        raise ValueError("bad")

    runner = JobRunner(bus, consumer="w", topic="jobs", handlers={"boom": boom}, max_attempts=2)
    runner.submit(JobSpec("boom", {}))
    runner.submit(JobSpec("missing", {}))
    outcomes = {o.name: o for o in runner.run_pending()}
    assert not outcomes["boom"].succeeded and outcomes["boom"].attempts == 2
    assert outcomes["missing"].error == "no handler for job 'missing'"


# ------------------------------------ durable results (ADR-0044 note, durable jobs, 2026-09-26)


class Crash(BaseException):
    """A process death: not an ``Exception``, so neither the runner nor a handler retry sees it."""


def _die(*_: Any) -> None:
    raise Crash


def _durable(
    bus: Any,
    path: Path,
    handlers: Mapping[str, Any],
    *,
    idempotent: tuple[str, ...] = (),
) -> JobRunner:
    return JobRunner(
        bus, consumer="w", topic="jobs", handlers=handlers, results=path, idempotent=idempotent
    )


def test_a_crash_between_result_and_ack_does_not_rerun_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def work(params: Mapping[str, Any]) -> dict[str, Any]:
        calls.append(int(params["x"]))
        return {"doubled": int(params["x"]) * 2}

    results = tmp_path / "results.jsonl"
    with FileEventBus(tmp_path / "bus") as bus:
        runner = _durable(bus, results, {"double": work})
        job_id = runner.submit(JobSpec("double", {"x": 21}))
        monkeypatch.setattr(bus, "ack", _die)
        with pytest.raises(Crash):
            runner.run_pending()
        monkeypatch.undo()
    assert calls == [21]
    assert [e.type for e in AppendOnlyJournal(results).entries] == [JOB_STARTED, JOB_RESULT]

    with FileEventBus(tmp_path / "bus") as bus:  # a new process: only the files survive
        assert len(bus.poll("w", "jobs", 10)) == 1  # the message is re-delivered
        restarted = _durable(bus, results, {"double": work})
        assert restarted.interrupted == {}
        assert restarted.outcomes[job_id] == JobOutcome(
            job_id, "double", True, 1, {"doubled": 42}, None
        )
        assert restarted.run_pending() == []  # acknowledged from the stored result
        assert bus.poll("w", "jobs", 10) == ()
    assert calls == [21]  # never re-run
    assert len(AppendOnlyJournal(results).entries) == 2  # nothing written on the replay


def test_the_in_memory_runner_still_reruns_after_the_same_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented behaviour without ``results`` (unchanged): the ack is the only record."""
    calls: list[int] = []

    def work(params: Mapping[str, Any]) -> int:
        calls.append(1)
        return 1

    with FileEventBus(tmp_path / "bus") as bus:
        runner = JobRunner(bus, consumer="w", topic="jobs", handlers={"one": work})
        runner.submit(JobSpec("one", {}))
        monkeypatch.setattr(bus, "ack", _die)
        with pytest.raises(Crash):
            runner.run_pending()
        monkeypatch.undo()
    with FileEventBus(tmp_path / "bus") as bus:
        JobRunner(bus, consumer="w", topic="jobs", handlers={"one": work}).run_pending()
    assert calls == [1, 1]


@pytest.mark.parametrize("idempotent", [(), ("work",)], ids=["undeclared", "idempotent"])
def test_an_interrupted_job_halts_for_review_unless_declared_idempotent(
    tmp_path: Path, idempotent: tuple[str, ...]
) -> None:
    """ADR-0044 idempotency: what a handler did before dying is unknown, so only a declared
    idempotent job runs again; any other interrupted job stops the runner (nothing polled)."""
    calls: list[str] = []
    crash = {"left": 1}

    def work(params: Mapping[str, Any]) -> str:
        calls.append(str(params["id"]))
        if crash["left"]:
            crash["left"] -= 1
            raise Crash
        return "done"

    results = tmp_path / "results.jsonl"
    bus = InMemoryEventBus()
    runner = _durable(bus, results, {"work": work}, idempotent=idempotent)
    job_id = runner.submit(JobSpec("work", {"id": "a"}))
    with pytest.raises(Crash):
        runner.run_pending()
    restarted = _durable(bus, results, {"work": work}, idempotent=idempotent)
    assert restarted.interrupted == {job_id: "work"}
    if not idempotent:
        with pytest.raises(JobInterrupted, match="not declared idempotent"):
            restarted.run_pending()
        assert calls == ["a"] and len(bus.poll("w", "jobs", 10)) == 1  # not acknowledged
        with pytest.raises(JobInterrupted):  # still halted after another restart
            _durable(bus, results, {"work": work}).run_pending()
        return
    [outcome] = restarted.run_pending()
    assert outcome.succeeded and outcome.result == "done" and calls == ["a", "a"]
    assert restarted.interrupted == {} and bus.poll("w", "jobs", 10) == ()
    again = _durable(bus, results, {"work": work}, idempotent=idempotent)
    assert again.outcomes[job_id] == outcome and again.interrupted == {}
    types = [e.type for e in AppendOnlyJournal(results).entries]
    assert types == [JOB_STARTED, JOB_STARTED, JOB_RESULT]


def test_a_tampered_results_file_is_refused(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    bus = InMemoryEventBus()
    runner = _durable(bus, results, {"double": lambda p: int(p["x"]) * 2})
    runner.submit(JobSpec("double", {"x": 1}))
    runner.run_pending()
    lines = results.read_text(encoding="utf-8").splitlines(keepends=True)
    edited = lines[1].replace('"result":2', '"result":3')
    assert edited != lines[1]
    results.write_text(lines[0] + edited, encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        _durable(bus, results, {"double": lambda p: 0})


def test_a_rechained_forged_history_is_refused(tmp_path: Path) -> None:
    """Lines re-chained correctly (an attacker who knows the hash rules) that are not a valid
    job history: only the semantic replay can refuse them."""
    job = JobSpec("double", {"x": 1})
    started = {"job_id": job.job_id, "name": "double", "params": {"x": 1}}
    result = {
        "job_id": job.job_id,
        "name": "double",
        "succeeded": True,
        "attempts": 1,
        "result": 99,
        "error": None,
    }
    forgeries: list[list[tuple[str, dict[str, Any]]]] = [
        [(JOB_RESULT, result)],  # a result without a start
        [(JOB_STARTED, {**started, "params": {"x": 2}})],  # job_id is not the content
        [(JOB_STARTED, started), (JOB_RESULT, result), (JOB_STARTED, started)],  # start after
        [(JOB_STARTED, started), (JOB_RESULT, result), (JOB_RESULT, result)],  # second result
        [(JOB_STARTED, started), (JOB_STARTED, started)],  # non-idempotent job started twice
        [("job_forgotten", started)],  # unknown line type
        [(JOB_STARTED, {**started, "note": "x"})],  # extra field
        [(JOB_STARTED, started), (JOB_RESULT, {**result, "error": "e"})],  # success + error
        [(JOB_STARTED, started), (JOB_RESULT, {**result, "attempts": 0})],  # success, 0 tries
    ]
    for number, lines in enumerate(forgeries):
        journal = AppendOnlyJournal(tmp_path / f"forged-{number}.jsonl")
        for type_, payload in lines:
            journal.append(type_, payload)
        with pytest.raises(JobResultsCorrupted):
            _durable(InMemoryEventBus(), journal.path, {"double": lambda p: 0})


def test_durable_mode_refuses_non_json_results_and_misnamed_messages(tmp_path: Path) -> None:
    bus = InMemoryEventBus()
    runner = _durable(bus, tmp_path / "r.jsonl", {"obj": lambda p: object()})
    runner.submit(JobSpec("obj", {}))
    with pytest.raises(TypeError):
        runner.run_pending()
    assert len(bus.poll("w", "jobs", 10)) == 1  # not acknowledged
    with pytest.raises(JobInterrupted, match="could not be journaled"):
        runner.run_pending()

    other = InMemoryEventBus()
    other.publish(BusMessage.build("jobs", "0" * 64, {"name": "obj", "params": {}}))
    with pytest.raises(ValueError, match="content identity"):
        _durable(other, tmp_path / "r2.jsonl", {"obj": lambda p: 1}).run_pending()
    assert not (tmp_path / "r2.jsonl").exists()
    with pytest.raises(ValueError, match="idempotent names no handler"):
        _durable(other, tmp_path / "r3.jsonl", {"obj": lambda p: 1}, idempotent=("nope",))


def test_the_jobs_module_uses_only_apps_core_and_the_stdlib() -> None:
    source = Path(__file__).resolve().parents[2] / "apps" / "worker" / "jobs.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    roots = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert roots <= {"__future__", "apps", "core", "collections", "dataclasses", "json", "typing"}
