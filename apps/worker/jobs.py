"""Idempotent, retryable jobs over the event bus (apps/worker README; ADR-0044).

A job is identified by its content (``job_id`` = hash of name + params), so submitting it twice runs
it once; a handler failure is retried up to ``max_attempts`` and then recorded as failed (never
dropped). Jobs arrive as bus messages on a topic; the runner acknowledges a message only after the
job's outcome is recorded, so a crash between the two re-delivers it and idempotency absorbs it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.domain.base import content_hash

__all__ = ["JobOutcome", "JobRunner", "JobSpec"]


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    params: Mapping[str, Any]

    @property
    def job_id(self) -> str:
        return content_hash({"name": self.name, "params": dict(self.params)})

    def message(self, topic: str) -> BusMessage:
        return BusMessage.build(
            topic, self.job_id, {"name": self.name, "params": dict(self.params)}
        )


@dataclass(frozen=True, slots=True)
class JobOutcome:
    job_id: str
    name: str
    succeeded: bool
    attempts: int
    result: Any
    error: str | None


class JobRunner:
    def __init__(
        self,
        bus: EventBusAdapter,
        *,
        consumer: str,
        topic: str,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]],
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._bus = bus
        self._consumer = consumer
        self._topic = topic
        self._handlers = dict(handlers)
        self._max_attempts = max_attempts
        self._outcomes: dict[str, JobOutcome] = {}

    @property
    def outcomes(self) -> Mapping[str, JobOutcome]:
        return dict(self._outcomes)

    def submit(self, job: JobSpec) -> str:
        self._bus.publish(job.message(self._topic))
        return job.job_id

    def run_pending(self, limit: int = 100) -> list[JobOutcome]:
        ran: list[JobOutcome] = []
        for message in self._bus.poll(self._consumer, self._topic, limit):
            job_id = message.key
            if job_id not in self._outcomes:  # idempotent: a duplicate delivery runs nothing
                self._outcomes[job_id] = self._run(job_id, message)
                ran.append(self._outcomes[job_id])
            self._bus.ack(self._consumer, self._topic, message.message_id)
        return ran

    def _run(self, job_id: str, message: BusMessage) -> JobOutcome:
        name = str(message.payload["name"])
        params = message.payload["params"]
        handler = self._handlers.get(name)
        if handler is None:
            return JobOutcome(job_id, name, False, 0, None, f"no handler for job {name!r}")
        error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return JobOutcome(job_id, name, True, attempt, handler(params), None)
            except Exception as exc:  # noqa: BLE001 - recorded, retried, never dropped
                error = f"{type(exc).__name__}: {exc}"
        return JobOutcome(job_id, name, False, self._max_attempts, None, error)
