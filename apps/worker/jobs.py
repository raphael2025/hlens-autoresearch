"""Idempotent, retryable jobs over the event bus (apps/worker README; ADR-0044).

A job is identified by its content (``job_id`` = hash of name + params), so submitting it twice runs
it once; a handler failure is retried up to ``max_attempts`` and then recorded as failed (never
dropped). Jobs arrive as bus messages on a topic; the runner acknowledges a message only after the
job's outcome is recorded, so a crash between the two re-delivers it and idempotency absorbs it.

**Durable results** (ADR-0044 implementation note, durable jobs and bus wiring, 2026-09-26).
Without ``results`` the outcome table lives in memory (the original behaviour): after a restart the
bus acknowledgement is the only de-duplication, so a crash between recording an outcome and acking
its message re-runs the job once. With ``results=<path>`` every job writes, to a hash-chained
append-only journal (``apps.worker.journal``, the same on-disk contract as the audit):

- ``job_started`` (``job_id``, ``name``, ``params``) **before** the handler runs, and
- ``job_result`` (``job_id``, ``name``, ``succeeded``, ``attempts``, ``result``, ``error``) once
  the outcome is known, **before** the message is acknowledged.

Reopening replays and verifies the journal (hash chain, then every line: known type, exact fields,
``job_id`` = the content identity of ``name`` + ``params``, a result only after its start, at most
one result per job, no start after a result) and refuses anything else (``JobResultsCorrupted``,
never skipped or repaired). Then:

- a job **with** a recorded result is never run again: a re-delivered message is acknowledged from
  the stored outcome (crash between result and ack);
- a job that **started without a result** (the process died inside the handler, or before its
  result was written) is handled by ADR-0044's idempotency rule. The runner cannot know what a
  handler already did, so only a handler the caller declared idempotent (``idempotent=``) is run
  again when its message is re-delivered; any other interrupted job stops the runner:
  ``run_pending`` raises ``JobInterrupted`` without polling, until a human reviews it
  (``JobRunner.interrupted`` names the jobs; resolving one is a new results file). A
  non-idempotent job started twice in the journal is refused on opening.

Durable results must be JSON (``core.domain.base.canonical_json``); the stored and returned
``result`` is its JSON form, identical before and after a restart. If a durable write fails, the
runner stops (``JobInterrupted`` on the next ``run_pending``) and the message stays unacknowledged.
A message whose key is not its content identity is refused in durable mode (``ValueError``, not
acknowledged): its journal lines could not be verified on reopening.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

from apps.worker.journal import AppendOnlyJournal, JournalCorrupted, JournalEntry, JournalPath
from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.domain.base import canonical_json, content_hash

__all__ = [
    "JOB_RESULT",
    "JOB_STARTED",
    "JobInterrupted",
    "JobOutcome",
    "JobResultsCorrupted",
    "JobRunner",
    "JobSpec",
]

#: Journal line written before a job's handler runs.
JOB_STARTED: Final = "job_started"
#: Journal line written once a job's outcome is known, before its message is acknowledged.
JOB_RESULT: Final = "job_result"

_STARTED_FIELDS = frozenset({"job_id", "name", "params"})
_RESULT_FIELDS = frozenset({"job_id", "name", "succeeded", "attempts", "result", "error"})


def _job_id(name: str, params: Any) -> str:
    return content_hash({"name": name, "params": params})


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    params: Mapping[str, Any]

    @property
    def job_id(self) -> str:
        return _job_id(self.name, dict(self.params))

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


class JobResultsCorrupted(JournalCorrupted):
    """The results journal verifies as a chain but its lines are not a valid job history."""


class JobInterrupted(RuntimeError):
    """A job started without a recorded result and may not be re-run (see module docs)."""


class JobRunner:
    def __init__(
        self,
        bus: EventBusAdapter,
        *,
        consumer: str,
        topic: str,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]],
        max_attempts: int = 3,
        results: JournalPath | None = None,
        idempotent: Collection[str] = (),
    ) -> None:
        """``results``: a journal path for durable outcomes (``None``: in memory, the original
        behaviour). ``idempotent``: handler names that may safely run again after an
        interrupted start (no default idempotency: an undeclared handler halts for review)."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        unknown = sorted(set(idempotent) - set(handlers))
        if unknown:
            raise ValueError(f"idempotent names no handler: {unknown}")
        self._bus = bus
        self._consumer = consumer
        self._topic = topic
        self._handlers = dict(handlers)
        self._max_attempts = max_attempts
        self._idempotent = frozenset(idempotent)
        self._outcomes: dict[str, JobOutcome] = {}
        #: Jobs started without a recorded result (job_id -> name), from the durable journal.
        self._interrupted: dict[str, str] = {}
        #: Why the runner stopped in this process (a durable write failed).
        self._stopped: str | None = None
        self._journal = None if results is None else AppendOnlyJournal(results)
        if self._journal is not None:
            self._replay(self._journal.entries)

    @property
    def outcomes(self) -> Mapping[str, JobOutcome]:
        return dict(self._outcomes)

    @property
    def durable(self) -> bool:
        return self._journal is not None

    @property
    def interrupted(self) -> Mapping[str, str]:
        """Jobs that started without a recorded result (``job_id -> name``)."""
        return dict(self._interrupted)

    def submit(self, job: JobSpec) -> str:
        self._bus.publish(job.message(self._topic))
        return job.job_id

    def run_pending(self, limit: int = 100) -> list[JobOutcome]:
        """Run the pending jobs (at most ``limit`` messages) and return the ones run now; a
        message whose job already has an outcome (in memory, or recorded durably before a
        restart) is only acknowledged."""
        self._check_can_run()
        ran: list[JobOutcome] = []
        for message in self._bus.poll(self._consumer, self._topic, limit):
            job_id = message.key
            if job_id not in self._outcomes:  # idempotent: a duplicate delivery runs nothing
                self._outcomes[job_id] = self._execute(job_id, message)
                ran.append(self._outcomes[job_id])
            self._bus.ack(self._consumer, self._topic, message.message_id)
        return ran

    # -- internals ------------------------------------------------------------------------------

    def _check_can_run(self) -> None:
        if self._stopped is not None:
            raise JobInterrupted(f"the job runner stopped: {self._stopped}")
        halting = sorted(
            f"{name} ({job_id[:12]})"
            for job_id, name in self._interrupted.items()
            if name not in self._idempotent
        )
        if halting:
            raise JobInterrupted(
                f"jobs started without a recorded result and not declared idempotent: {halting}; "
                "what they already did is unknown, so the runner waits for a human review"
            )

    def _execute(self, job_id: str, message: BusMessage) -> JobOutcome:
        if self._journal is None:
            return self._run(job_id, message)
        payload = message.model_dump(mode="json")["payload"]
        name, params = str(payload["name"]), payload["params"]
        if job_id != _job_id(name, params):
            raise ValueError(f"message key {job_id[:12]} is not the content identity of its job")
        try:
            self._journal.append(JOB_STARTED, {"job_id": job_id, "name": name, "params": params})
        except Exception as exc:
            self._stopped = f"job {job_id[:12]} could not be journaled as started ({exc})"
            raise
        self._interrupted[job_id] = name
        outcome = self._run(job_id, message)
        try:
            result = json.loads(canonical_json(outcome.result))
            self._journal.append(
                JOB_RESULT,
                {
                    "job_id": job_id,
                    "name": outcome.name,
                    "succeeded": outcome.succeeded,
                    "attempts": outcome.attempts,
                    "result": result,
                    "error": outcome.error,
                },
            )
        except Exception as exc:
            self._stopped = f"the result of job {job_id[:12]} could not be journaled ({exc})"
            raise
        del self._interrupted[job_id]
        return JobOutcome(
            job_id, outcome.name, outcome.succeeded, outcome.attempts, result, outcome.error
        )

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

    def _replay(self, entries: tuple[JournalEntry, ...]) -> None:
        started: dict[str, str] = {}
        for entry in entries:
            where = f"results line {entry.seq}"
            raw = dict(entry.payload)
            if entry.type == JOB_STARTED:
                if set(raw) != _STARTED_FIELDS:
                    raise JobResultsCorrupted(f"{where} does not have exactly {_STARTED_FIELDS}")
                job_id, name = raw["job_id"], raw["name"]
                if not isinstance(name, str) or job_id != _job_id(name, raw["params"]):
                    raise JobResultsCorrupted(f"{where}: job_id is not the job's content identity")
                if job_id in self._outcomes:
                    raise JobResultsCorrupted(
                        f"{where}: job {job_id[:12]} started after its result"
                    )
                if job_id in started and name not in self._idempotent:
                    raise JobResultsCorrupted(
                        f"{where}: job {name!r} ({job_id[:12]}) is not declared idempotent, "
                        "yet it was started again without a result"
                    )
                started[job_id] = name
            elif entry.type == JOB_RESULT:
                self._admit_result(raw, started, where)
            else:
                raise JobResultsCorrupted(f"{where} has unknown type {entry.type!r}")
        self._interrupted = started

    def _admit_result(self, raw: dict[str, Any], started: dict[str, str], where: str) -> None:
        if set(raw) != _RESULT_FIELDS:
            raise JobResultsCorrupted(f"{where} does not have exactly {_RESULT_FIELDS}")
        job_id, name = raw["job_id"], raw["name"]
        if started.get(job_id) != name:
            raise JobResultsCorrupted(f"{where}: a result for a job that was not started")
        succeeded, attempts, error = raw["succeeded"], raw["attempts"], raw["error"]
        if (
            not isinstance(succeeded, bool)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < (1 if succeeded else 0)
            or not (error is None if succeeded else isinstance(error, str))
        ):
            raise JobResultsCorrupted(f"{where} is not a valid job outcome")
        del started[job_id]
        self._outcomes[job_id] = JobOutcome(job_id, name, succeeded, attempts, raw["result"], error)
