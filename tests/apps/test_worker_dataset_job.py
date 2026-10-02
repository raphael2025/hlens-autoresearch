"""ADR-0101 D2: the thin v3 Dataset worker job (fake port; no catalog, no research/ import)."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from apps.worker.dataset_job import (
    DATASET_BUILD_JOB,
    DatasetJobFailed,
    DatasetJobRejected,
    dataset_build_handler,
    dataset_build_job,
    dataset_job_handlers,
)
from apps.worker.jobs import JobRunner, read_job_results
from infrastructure.event_bus import InMemoryEventBus

REPO = Path(__file__).resolve().parents[2]
TOPIC = "worker.dataset"
SECRET = "postgresql://user:s3cr3t@db.invalid/hlens"
REQUEST: dict[str, Any] = {"window": "w1", "pit": {"name": "p"}}


class FakePort:
    """Selection id = a function of the request; ``build`` counts calls and echoes the id."""

    def __init__(self, *, fail: BaseException | None = None, other: bool = False) -> None:
        self.builds = 0
        self.fail = fail
        self.other = other

    def selection_id(self, request: Mapping[str, Any]) -> str:
        return f"hlens.dataset.pit-selection@3.0.0.{request['window']}"

    def build(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.builds += 1
        if self.fail is not None:
            raise self.fail
        selection = "hlens.dataset.pit-selection@3.0.0.other" if self.other else None
        return {
            "selection_id": selection or self.selection_id(request),
            "manifest_hash": "m" * 64,
            "replayed": self.builds > 1,
        }


def _runner(port: FakePort, bus: InMemoryEventBus, results: Path | None = None) -> JobRunner:
    return JobRunner(
        bus,
        consumer="dataset",
        topic=TOPIC,
        handlers=dataset_job_handlers(port),
        max_attempts=1,
        results=results,
    )


def test_the_job_is_keyed_by_the_selection_id() -> None:
    port = FakePort()
    job = dataset_build_job(port, REQUEST)
    assert job.name == DATASET_BUILD_JOB
    assert dict(job.params) == {
        "selection_id": "hlens.dataset.pit-selection@3.0.0.w1",
        "request": REQUEST,
    }
    assert dataset_build_job(port, dict(REQUEST)).job_id == job.job_id
    other = dataset_build_job(port, {**REQUEST, "window": "w2"})
    assert other.job_id != job.job_id
    assert port.builds == 0  # submitting never builds


def test_a_resubmitted_selection_runs_once(tmp_path: Path) -> None:
    port = FakePort()
    bus = InMemoryEventBus()
    runner = _runner(port, bus, tmp_path / "results.jsonl")
    job = dataset_build_job(port, REQUEST)
    first = runner.submit(job)
    assert runner.submit(dataset_build_job(port, dict(REQUEST))) == first
    outcomes = runner.run_pending()
    runner.submit(job)  # a later re-submission is absorbed by the stored outcome
    runner.run_pending()
    assert port.builds == 1
    assert [o.succeeded for o in outcomes] == [True]
    assert outcomes[0].result["selection_id"] == job.params["selection_id"]
    history = read_job_results(tmp_path / "results.jsonl")
    assert [record.status for record in history.jobs] == ["succeeded"]


def test_a_key_that_does_not_match_its_request_is_rejected() -> None:
    port = FakePort()
    handle = dataset_build_handler(port)
    with pytest.raises(DatasetJobRejected, match="does not match"):
        handle({"selection_id": "hlens.dataset.pit-selection@3.0.0.w9", "request": REQUEST})
    assert port.builds == 0


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"selection_id": "a.b"},
        {"selection_id": "a.b", "request": REQUEST, "extra": 1},
        {"selection_id": 7, "request": REQUEST},
        {"selection_id": "", "request": REQUEST},
        {"selection_id": "a b", "request": REQUEST},
        {"selection_id": "a.b", "request": ["not", "a", "mapping"]},
    ],
)
def test_malformed_params_are_rejected_before_any_build(params: dict[str, Any]) -> None:
    port = FakePort()
    with pytest.raises(DatasetJobRejected):
        dataset_build_handler(port)(params)
    assert port.builds == 0


def test_a_summary_of_another_selection_is_rejected() -> None:
    port = FakePort(other=True)
    job = dataset_build_job(port, REQUEST)
    with pytest.raises(DatasetJobRejected, match="not of the job's selection"):
        dataset_build_handler(port)(job.params)


def test_a_port_failure_is_journaled_by_type_only(tmp_path: Path) -> None:
    port = FakePort(fail=RuntimeError(f"could not reach {SECRET}"))
    bus = InMemoryEventBus()
    runner = _runner(port, bus, tmp_path / "results.jsonl")
    runner.submit(dataset_build_job(port, REQUEST))
    (outcome,) = runner.run_pending()
    assert not outcome.succeeded
    assert outcome.error == "DatasetJobFailed: dataset build failed (RuntimeError)"
    journal = (tmp_path / "results.jsonl").read_text(encoding="utf-8")
    assert "s3cr3t" not in journal and SECRET not in journal


def test_a_failing_selection_id_never_leaks_its_text() -> None:
    class Broken(FakePort):
        def selection_id(self, request: Mapping[str, Any]) -> str:
            raise ValueError(SECRET)

    with pytest.raises(DatasetJobFailed) as failed:
        dataset_build_job(Broken(), REQUEST)
    assert SECRET not in str(failed.value) and "ValueError" in str(failed.value)


def test_the_port_answer_must_be_a_selection_id() -> None:
    class Odd(FakePort):
        def selection_id(self, request: Mapping[str, Any]) -> str:
            return "not a selection id"

    with pytest.raises(DatasetJobRejected):
        dataset_build_job(Odd(), REQUEST)


class Crash(BaseException):
    """Stands for the process dying inside the build."""


def test_a_crash_inside_the_build_is_not_converted_into_a_failure(tmp_path: Path) -> None:
    """A death inside the port leaves the job started without a result (ADR-0044): the
    deployment decides by ``idempotent=`` whether the replaying v3 build may run again."""
    port = FakePort(fail=Crash())
    bus = InMemoryEventBus()
    results = tmp_path / "results.jsonl"
    job = dataset_build_job(port, REQUEST)
    runner = _runner(port, bus, results)
    runner.submit(job)
    with pytest.raises(Crash):
        runner.run_pending()
    port.fail = None
    restarted = JobRunner(
        bus,
        consumer="dataset",
        topic=TOPIC,
        handlers=dataset_job_handlers(port),
        results=results,
        idempotent=(DATASET_BUILD_JOB,),
    )
    assert restarted.interrupted == {job.job_id: DATASET_BUILD_JOB}
    (outcome,) = restarted.run_pending()
    assert outcome.succeeded and outcome.result["replayed"] is True
    assert port.builds == 2 and restarted.reruns == {job.job_id: 1}


# ------------------------------------------------------------------------- static guards


def _roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    roots |= {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return roots


def test_the_job_module_uses_only_apps_core_and_the_stdlib() -> None:
    roots = _roots(REPO / "apps" / "worker" / "dataset_job.py")
    assert roots <= {"__future__", "apps", "core", "collections", "re", "typing"}


@pytest.mark.parametrize("name", ["__init__.py", "serve.py", "jobs.py", "loop.py"])
def test_the_job_is_never_registered_automatically(name: str) -> None:
    source = (REPO / "apps" / "worker" / name).read_text(encoding="utf-8")
    assert "dataset_job" not in source and DATASET_BUILD_JOB not in source
