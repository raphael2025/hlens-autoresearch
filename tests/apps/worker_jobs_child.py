"""Child-process side of ``test_worker_jobs_cross_process.py`` (Phase 11 cross-process durability).

TEST ONLY. Run as ``python -m tests.apps.worker_jobs_child '<json>'`` from the repository root.
Every step is a fresh interpreter; only files are shared. It prints one ``RESULT <json>`` line
(``{"ok": false, "error": ...}`` for a refusal, which is what the parent asserts on).

``mode: "jobs"`` — a durable ``JobRunner`` (``results=`` journal) over a ``FileEventBus``. The one
handler, ``work``, appends one line to a side-effect file (so the parent can count how often it
really ran) and returns ``params["x"] * 2``. Deterministic crash points (``kill``; a test-side
wrap in this process only, no production seam), each a ``SIGKILL``:

- ``in_handler``: inside the handler, after its side effect (``job_started`` is on disk, no result);
- ``before_ack``: after ``job_result`` was journaled, before the bus acknowledgement.

``mode: "loop"`` — the generic ``ResearchLoop`` with the fake stages of
``tests/apps/test_research_loop_durable.py`` over a durable ``LoopAuditLog`` and the TEST ONLY
budget of its ``test_cumulative_budget_totals_survive_a_restart`` (3 trials in total, one per
round). ``kill: "begin_round:<i>"`` dies right after round ``i``'s ``loop_round_started`` line.

``hold``: print ``READY`` after opening and block on stdin (keeps the bus lock held).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.worker import JobRunner, JobSpec, LoopAuditLog, LoopBudget
from infrastructure.event_bus import FileEventBus
from tests.apps.test_research_loop_durable import _durable_stages, _loop

RESULT_PREFIX = "RESULT "
CONSUMER = "cross_process_worker"
TOPIC = "jobs"
#: TEST ONLY (``test_cumulative_budget_totals_survive_a_restart``'s numbers).
BUDGET = LoopBudget(
    max_trials_per_round=5,
    max_trials_total=3,
    max_llm_cost_units=Decimal(0),
    max_compute_seconds=Decimal(1000),
)


def _die() -> None:
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _outcome(outcome: Any) -> dict[str, Any]:
    return {
        "job_id": outcome.job_id,
        "name": outcome.name,
        "succeeded": outcome.succeeded,
        "attempts": outcome.attempts,
        "result": outcome.result,
        "error": outcome.error,
    }


def _jobs(args: dict[str, Any]) -> dict[str, Any]:
    kill = args.get("kill")
    side_effects = Path(args["side_effects"])

    def work(params: Mapping[str, Any]) -> int:
        with side_effects.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(params)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if kill == "in_handler":
            _die()
        return int(params["x"]) * 2

    if kill == "before_ack":
        real_ack = FileEventBus.ack

        def die_before_ack(bus: FileEventBus, consumer: str, topic: str, message_id: str) -> None:
            if consumer == CONSUMER:
                _die()
            real_ack(bus, consumer, topic, message_id)

        FileEventBus.ack = die_before_ack  # type: ignore[method-assign,assignment]
    with FileEventBus(Path(args["bus"])) as bus:
        runner = JobRunner(
            bus,
            consumer=CONSUMER,
            topic=TOPIC,
            handlers={"work": work},
            max_attempts=1,
            results=Path(args["results"]),
            idempotent=tuple(args.get("idempotent", ())),
        )
        result: dict[str, Any] = {
            "ok": True,
            "interrupted": dict(runner.interrupted),
            "reruns": dict(runner.reruns),
        }
        for params in args.get("submit", ()):
            runner.submit(JobSpec("work", params))
        if args.get("hold"):
            print("READY", flush=True)
            sys.stdin.readline()
        if args.get("run"):
            result["ran"] = [_outcome(o) for o in runner.run_pending()]
            result["reruns"] = dict(runner.reruns)
        result["outcomes"] = {k: _outcome(v) for k, v in sorted(runner.outcomes.items())}
        result["pending"] = [m.key for m in bus.poll(CONSUMER, TOPIC, 100)]
    return result


def _worker_loop(args: dict[str, Any]) -> dict[str, Any]:
    kill = args.get("kill")
    if kill is not None:
        index = int(kill.removeprefix("begin_round:"))
        real_begin = LoopAuditLog.begin_round

        def begin_then_die(audit: LoopAuditLog, loop_id: str, round_index: int) -> None:
            real_begin(audit, loop_id, round_index)
            if round_index == index:
                _die()

        LoopAuditLog.begin_round = begin_then_die  # type: ignore[method-assign,assignment]
    loop, _ = _loop(_durable_stages(), LoopAuditLog(Path(args["audit"])), budget=BUDGET)
    result: dict[str, Any] = {
        "ok": True,
        "restored_trials": loop.total_usage.trials,
        "halted": None if loop.halted is None else loop.halted.value,
    }
    records = loop.run_unattended(int(args["run"]))
    result["statuses"] = [r.status.value for r in records]
    result["record_hashes"] = [r.record_hash for r in loop.audit.records]
    result["total_trials"] = loop.total_usage.trials
    result["halted"] = None if loop.halted is None else loop.halted.value
    return result


def main(argv: list[str]) -> int:
    args = json.loads(argv[1])
    try:
        result = _jobs(args) if args["mode"] == "jobs" else _worker_loop(args)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which asserts on it
        result = {
            "ok": False,
            "error": type(exc).__name__,
            "mro": [cls.__name__ for cls in type(exc).__mro__],
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    print(RESULT_PREFIX + json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
