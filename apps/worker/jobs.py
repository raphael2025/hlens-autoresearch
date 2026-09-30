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
  (``JobRunner.interrupted`` names the jobs; resolving one is a new results file).

**``idempotent=`` -- read before using** (ADR-0044 implementation note, review fixes 3,
2026-09-26). Declaring a handler idempotent is a promise the runner cannot check: *running it
again after it died half-way leaves the world exactly as running it once would* (no double
publication, no double spend, no second append that is not de-duplicated downstream). A wrong
declaration silently repeats side effects, so:

- nothing is idempotent by default; the set names handlers explicitly (a name that is not a
  handler is refused with ``ValueError``, a bare string instead of a collection of names with
  ``TypeError``), and it is refused without ``results=`` (it only means something in durable
  mode: in memory there is no record of an interrupted start to act on);
- every re-run is **auditable** in the results journal: it is written as a ``job_rerun`` line
  (``job_id``, ``name``, ``params``, ``interrupted_starts`` = how many earlier starts of this job
  have no result) **instead of** a second ``job_started``; ``JobRunner.reruns`` counts them per
  job. Reopening refuses a ``job_started`` for a job that already started without a result (an
  unaudited re-run), a ``job_rerun`` of a handler not declared idempotent now, a ``job_rerun``
  whose count does not match the journal, and a ``job_rerun`` of a job that was never started or
  already has a result.

Durable results must be JSON (``core.domain.base.canonical_json``); the stored and returned
``result`` is its JSON form, identical before and after a restart. If a durable write fails, the
runner stops (``JobInterrupted`` on the next ``run_pending``) and the message stays unacknowledged.
A message whose key is not its content identity is refused in durable mode (``ValueError``, not
acknowledged): its journal lines could not be verified on reopening.

**Read-only view** (apps/api ``GET /jobs``, 2026-09-26; CODE_COMPLETE / DEBUG_PENDING).
``read_job_results(path, idempotent=...)`` replays a results journal with the *same* verification
the runner uses on reopening (one shared ``_Replay``) and returns every job's status, attempts,
result or failure reason, without a bus or handlers and without writing. A tampered or invalid
journal raises (``JournalCorrupted`` / ``JobResultsCorrupted``); it never yields partial data.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from apps.worker.journal import AppendOnlyJournal, JournalCorrupted, JournalEntry, JournalPath
from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.domain.base import canonical_json, content_hash

__all__ = [
    "JOB_FAILED",
    "JOB_INTERRUPTED",
    "JOB_RERUN",
    "JOB_RESULT",
    "JOB_STARTED",
    "JOB_SUCCEEDED",
    "JobHistory",
    "JobInterrupted",
    "JobOutcome",
    "JobRecord",
    "JobResultsCorrupted",
    "JobRunner",
    "JobSpec",
    "JobStatus",
    "read_job_results",
]

#: Journal line written before a job's handler runs.
JOB_STARTED: Final = "job_started"
#: Journal line written once a job's outcome is known, before its message is acknowledged.
JOB_RESULT: Final = "job_result"
#: Journal line written, instead of ``JOB_STARTED``, before an interrupted idempotent job runs
#: again (review fixes 3): the re-run is recorded, never implicit.
JOB_RERUN: Final = "job_rerun"

_STARTED_FIELDS = frozenset({"job_id", "name", "params"})
_RERUN_FIELDS = frozenset({"job_id", "name", "params", "interrupted_starts"})
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
        interrupted start (no default idempotency: an undeclared handler halts for review). Read
        the module docs, **``idempotent=`` -- read before using**: a wrong declaration repeats
        side effects; every re-run is written to the results journal (``job_rerun``)."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if isinstance(idempotent, str | bytes):
            raise TypeError("idempotent must be a collection of handler names, not one string")
        unknown = sorted(set(idempotent) - set(handlers))
        if unknown:
            raise ValueError(f"idempotent names no handler: {unknown}")
        if idempotent and results is None:
            raise ValueError(
                "idempotent= only applies to durable results (results=<path>): in memory there "
                "is no record of an interrupted start to re-run"
            )
        self._bus = bus
        self._consumer = consumer
        self._topic = topic
        self._handlers = dict(handlers)
        self._max_attempts = max_attempts
        self._idempotent = frozenset(idempotent)
        self._outcomes: dict[str, JobOutcome] = {}
        #: Jobs started without a recorded result (job_id -> name), from the durable journal.
        self._interrupted: dict[str, str] = {}
        #: How many starts without a result each interrupted job has (job_id -> count).
        self._starts: dict[str, int] = {}
        #: Recorded re-runs of interrupted idempotent jobs (job_id -> count), from the journal.
        self._reruns: dict[str, int] = {}
        #: Why the runner stopped in this process (a durable write failed).
        self._stopped: str | None = None
        self._last_poll_count = 0
        self._journal = None if results is None else AppendOnlyJournal(results)
        if self._journal is not None:
            replay = _Replay(self._idempotent)
            replay.run(self._journal.entries)
            self._outcomes = replay.outcomes
            self._interrupted = replay.interrupted
            self._starts = replay.starts
            self._reruns = replay.reruns

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

    @property
    def reruns(self) -> Mapping[str, int]:
        """How often each job was re-run after an interrupted start (the ``job_rerun`` lines of
        the results journal; module docs, **``idempotent=``**). Empty in memory."""
        return dict(self._reruns)

    @property
    def last_poll_count(self) -> int:
        """Messages delivered by the most recent :meth:`run_pending` call, including duplicates
        that were only acknowledged; ``0`` means that poll found the queue empty."""
        return self._last_poll_count

    def submit(self, job: JobSpec) -> str:
        self._bus.publish(job.message(self._topic))
        return job.job_id

    def run_pending(self, limit: int = 100) -> list[JobOutcome]:
        """Run the pending jobs (at most ``limit`` messages) and return the ones run now; a
        message whose job already has an outcome (in memory, or recorded durably before a
        restart) is only acknowledged."""
        self._check_can_run()
        ran: list[JobOutcome] = []
        self._last_poll_count = 0
        for message in self._bus.poll(self._consumer, self._topic, limit):
            self._last_poll_count += 1
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
        prior = self._starts.get(job_id, 0)
        start: dict[str, Any] = {"job_id": job_id, "name": name, "params": params}
        try:
            if prior:  # an interrupted job declared idempotent (``_check_can_run``): a re-run
                self._journal.append(JOB_RERUN, {**start, "interrupted_starts": prior})
            else:
                self._journal.append(JOB_STARTED, start)
        except Exception as exc:
            self._stopped = f"job {job_id[:12]} could not be journaled as started ({exc})"
            raise
        if prior:
            self._reruns[job_id] = self._reruns.get(job_id, 0) + 1
        self._interrupted[job_id] = name
        self._starts[job_id] = prior + 1
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
        del self._starts[job_id]
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


# -- the verified replay (shared by ``JobRunner`` and the read-only ``read_job_results``) --------


class _Replay:
    """Replays and verifies a results journal's lines as a job history (module docs): the one
    implementation both the runner and the read-only view use, so they can never disagree."""

    def __init__(self, idempotent: frozenset[str]) -> None:
        self.idempotent = idempotent
        self.outcomes: dict[str, JobOutcome] = {}
        self.interrupted: dict[str, str] = {}
        self.starts: dict[str, int] = {}
        self.reruns: dict[str, int] = {}
        #: For the read-only view: each job's params, line numbers and start lines (all kinds).
        self.params: dict[str, Any] = {}
        self.first_seq: dict[str, int] = {}
        self.last_seq: dict[str, int] = {}
        self.all_starts: dict[str, int] = {}

    def run(self, entries: tuple[JournalEntry, ...]) -> None:
        started: dict[str, str] = {}
        for entry in entries:
            where = f"results line {entry.seq}"
            raw = dict(entry.payload)
            if entry.type in (JOB_STARTED, JOB_RERUN):
                fields = _STARTED_FIELDS if entry.type == JOB_STARTED else _RERUN_FIELDS
                if set(raw) != fields:
                    raise JobResultsCorrupted(f"{where} does not have exactly {fields}")
                job_id, name = raw["job_id"], raw["name"]
                if not isinstance(name, str) or job_id != _job_id(name, raw["params"]):
                    raise JobResultsCorrupted(f"{where}: job_id is not the job's content identity")
                if job_id in self.outcomes:
                    raise JobResultsCorrupted(
                        f"{where}: job {job_id[:12]} started after its result"
                    )
                if entry.type == JOB_STARTED:
                    self._admit_start(job_id, name, started, where)
                else:
                    self._admit_rerun(job_id, name, raw["interrupted_starts"], started, where)
                self.params.setdefault(job_id, raw["params"])
                self.first_seq.setdefault(job_id, entry.seq)
                self.all_starts[job_id] = self.all_starts.get(job_id, 0) + 1
            elif entry.type == JOB_RESULT:
                self._admit_result(raw, started, where)
                self.starts.pop(raw["job_id"], None)
            else:
                raise JobResultsCorrupted(f"{where} has unknown type {entry.type!r}")
            self.last_seq[str(raw["job_id"])] = entry.seq
        self.interrupted = started

    def _admit_start(self, job_id: str, name: str, started: dict[str, str], where: str) -> None:
        if job_id in started:
            raise JobResultsCorrupted(
                f"{where}: job {name!r} ({job_id[:12]}) was started again without a result and "
                f"without a {JOB_RERUN!r} line (an unaudited re-run)"
            )
        started[job_id] = name
        self.starts[job_id] = 1

    def _admit_rerun(
        self, job_id: str, name: str, count: Any, started: dict[str, str], where: str
    ) -> None:
        if started.get(job_id) != name:
            raise JobResultsCorrupted(f"{where}: a re-run of a job that was not started")
        if name not in self.idempotent:
            raise JobResultsCorrupted(
                f"{where}: job {name!r} ({job_id[:12]}) is not declared idempotent, yet it was "
                "re-run without a result"
            )
        expected = self.starts.get(job_id)
        if not isinstance(count, int) or isinstance(count, bool) or count != expected:
            raise JobResultsCorrupted(
                f"{where}: interrupted_starts {count!r} does not match the journal "
                f"({expected} starts without a result)"
            )
        self.starts[job_id] = count + 1
        self.reruns[job_id] = self.reruns.get(job_id, 0) + 1

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
        self.outcomes[job_id] = JobOutcome(job_id, name, succeeded, attempts, raw["result"], error)


# -- read-only view (apps/api ``GET /jobs``; ADR-0048 read-only console) -------------------------

#: ``JobRecord.status`` values.
type JobStatus = Literal["succeeded", "failed", "interrupted"]
JOB_SUCCEEDED: Final = "succeeded"
JOB_FAILED: Final = "failed"
JOB_INTERRUPTED: Final = "interrupted"


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One job as the verified results journal records it (read-only view).

    ``status``: ``succeeded`` / ``failed`` (a ``job_result`` line) or ``interrupted`` (started,
    no result yet -- a running job looks the same). ``starts`` counts its ``job_started`` and
    ``job_rerun`` lines, ``reruns`` only the latter. ``outcome`` is ``None`` while interrupted.
    ``first_seq`` / ``last_seq``: the journal lines of its first start and its latest line.
    """

    job_id: str
    name: str
    params: Any
    status: JobStatus
    starts: int
    reruns: int
    first_seq: int
    last_seq: int
    outcome: JobOutcome | None


@dataclass(frozen=True, slots=True)
class JobHistory:
    """Every job of one results journal, in the order of their first start."""

    jobs: tuple[JobRecord, ...]
    #: The journal's hash-chain tip (``GENESIS_HASH`` when empty) and its number of lines.
    head_hash: str
    lines: int


def read_job_results(results: JournalPath, *, idempotent: Collection[str] = ()) -> JobHistory:
    """Read a durable results journal without a runner, bus or handlers; never writes.

    It is verified exactly as ``JobRunner(results=...)`` verifies it on reopening (the same
    replay): a broken chain raises ``JournalCorrupted``, an invalid job history
    ``JobResultsCorrupted`` -- never partial data. ``idempotent`` must name the handlers the
    runner declares idempotent, since a ``job_rerun`` of any other handler is refused. A missing
    file is an empty journal (like ``AppendOnlyJournal``).
    """
    if isinstance(idempotent, str | bytes):
        raise TypeError("idempotent must be a collection of handler names, not one string")
    journal = AppendOnlyJournal(results)
    replay = _Replay(frozenset(idempotent))
    replay.run(journal.entries)
    records: list[JobRecord] = []
    for job_id in sorted(replay.first_seq, key=replay.first_seq.__getitem__):
        outcome = replay.outcomes.get(job_id)
        status: JobStatus
        if outcome is None:
            status, name = JOB_INTERRUPTED, replay.interrupted[job_id]
        else:
            status, name = (JOB_SUCCEEDED if outcome.succeeded else JOB_FAILED), outcome.name
        records.append(
            JobRecord(
                job_id=job_id,
                name=name,
                params=replay.params[job_id],
                status=status,
                starts=replay.all_starts[job_id],
                reruns=replay.reruns.get(job_id, 0),
                first_seq=replay.first_seq[job_id],
                last_seq=replay.last_seq[job_id],
                outcome=outcome,
            )
        )
    return JobHistory(tuple(records), journal.head_hash, len(journal.entries))
