"""ADR-0044: the in-memory bus passes its suite; worker jobs are idempotent and retried."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from apps.worker import JobRunner, JobSpec
from core.contracts.event_bus import BusMessage
from infrastructure.event_bus import InMemoryEventBus
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
