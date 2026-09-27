"""Phase 11 acceptance, cross-process: durable worker jobs and the worker loop's audit survive a
real process death (ADR-0044 durable jobs note; ADR-0049 durable audit note).

The existing restart tests of ``JobRunner(results=...)`` and ``ResearchLoop`` reopen the files in
the same interpreter and simulate a crash with an exception. Here each step is a separate
``python`` process (``worker_jobs_child``) and a crash is a ``SIGKILL`` at a deterministic point;
only the bus directory, the results journal, the audit and a side-effect file are shared. The
handler's side-effect file counts how often a job really ran, across processes.

Numbers (budgets, job params) are TEST ONLY. No production code is changed.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.worker import AppendOnlyJournal, read_job_results
from apps.worker.jobs import JOB_INTERRUPTED, JOB_RERUN, JOB_RESULT, JOB_STARTED, JOB_SUCCEEDED
from apps.worker.loop import ROUND_RECORDED, ROUND_STARTED
from tests.apps.worker_jobs_child import RESULT_PREFIX

REPO = Path(__file__).resolve().parents[2]
CHILD = "tests.apps.worker_jobs_child"
TIMEOUT = 120
KILLED = -signal.SIGKILL
PARAMS = {"x": 21}


def _command(args: dict[str, Any]) -> list[str]:
    return [sys.executable, "-m", CHILD, json.dumps(args)]


def _result(stdout: str) -> dict[str, Any] | None:
    lines = [line for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) <= 1, stdout
    return json.loads(lines[0].removeprefix(RESULT_PREFIX)) if lines else None


def _child(args: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    done = subprocess.run(
        _command(args), cwd=REPO, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )
    return done.returncode, _result(done.stdout)


def _ok(args: dict[str, Any]) -> dict[str, Any]:
    code, result = _child(args)
    assert code == 0 and result is not None, (code, result)
    assert result["ok"], result.get("traceback")
    return result


def _refused(args: dict[str, Any]) -> dict[str, Any]:
    code, result = _child(args)
    assert code == 0 and result is not None, (code, result)
    assert not result["ok"], "the process was expected to refuse"
    return result


def _killed(args: dict[str, Any]) -> None:
    code, result = _child(args)
    assert code == KILLED and result is None, (code, result)


class Paths:
    def __init__(self, root: Path) -> None:
        self.bus = root / "bus"
        self.results = root / "results.jsonl"
        self.side_effects = root / "side_effects.jsonl"

    def args(self, **extra: Any) -> dict[str, Any]:
        return {
            "mode": "jobs",
            "bus": str(self.bus),
            "results": str(self.results),
            "side_effects": str(self.side_effects),
            **extra,
        }

    def handler_runs(self) -> int:
        if not self.side_effects.exists():
            return 0
        return len(self.side_effects.read_text(encoding="utf-8").splitlines())

    def journal_types(self) -> list[str]:
        return [e.type for e in AppendOnlyJournal(self.results).entries]


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path)


# ------------------------------------------------------------------------ killed inside a job


def test_a_job_killed_mid_run_halts_the_next_process_and_is_never_rerun(paths: Paths) -> None:
    """SIGKILL inside the handler (``job_started`` on disk, no result). Another process sees the
    job as interrupted, refuses to run anything (``JobInterrupted``: what the handler already did
    is unknown), leaves the message unacknowledged and never runs the handler again."""
    _killed(paths.args(submit=[PARAMS], run=True, kill="in_handler"))
    assert paths.handler_runs() == 1
    assert paths.journal_types() == [JOB_STARTED]
    [record] = read_job_results(paths.results).jobs
    assert (record.status, record.starts, record.outcome) == (JOB_INTERRUPTED, 1, None)
    before = paths.results.read_bytes()

    seen = _ok(paths.args())  # open only: what the next process sees
    assert list(seen["interrupted"].values()) == ["work"]
    assert seen["pending"] == [record.job_id]  # the message was never acknowledged
    for _ in range(2):
        refusal = _refused(paths.args(run=True))
        assert refusal["error"] == "JobInterrupted"
        assert "waits for a human review" in refusal["message"]
    assert paths.handler_runs() == 1  # never re-run
    assert paths.results.read_bytes() == before  # nothing written by the refusing processes


def test_a_job_killed_mid_run_is_rerun_once_and_audited_only_when_declared_idempotent(
    paths: Paths,
) -> None:
    """The same crash, but the next process declares ``work`` idempotent: it re-runs the job once,
    writes a ``job_rerun`` line instead of a second start, and a third process runs nothing."""
    _killed(paths.args(submit=[PARAMS], run=True, kill="in_handler"))
    rerun = _ok(paths.args(run=True, idempotent=["work"]))
    [outcome] = rerun["ran"]
    assert (outcome["succeeded"], outcome["result"]) == (True, 42)
    assert rerun["reruns"] == {outcome["job_id"]: 1}
    assert rerun["pending"] == []
    assert paths.handler_runs() == 2  # the killed attempt and the audited re-run
    assert paths.journal_types() == [JOB_STARTED, JOB_RERUN, JOB_RESULT]

    third = _ok(paths.args(run=True, idempotent=["work"]))
    assert third["ran"] == [] and third["interrupted"] == {}
    assert third["outcomes"][outcome["job_id"]]["result"] == 42
    assert paths.handler_runs() == 2
    [record] = read_job_results(paths.results, idempotent=["work"]).jobs
    assert (record.status, record.starts, record.reruns) == (JOB_SUCCEEDED, 2, 1)


def test_a_job_killed_between_result_and_ack_is_settled_not_rerun(paths: Paths) -> None:
    """SIGKILL after ``job_result`` was fsync'd and before the bus ack: the next process
    acknowledges the re-delivered message from the stored outcome and never runs the handler."""
    _killed(paths.args(submit=[PARAMS], run=True, kill="before_ack"))
    assert paths.journal_types() == [JOB_STARTED, JOB_RESULT]
    before = paths.results.read_bytes()
    settled = _ok(paths.args(run=True))
    assert settled["ran"] == [] and settled["pending"] == []
    [stored] = settled["outcomes"].values()
    assert (stored["succeeded"], stored["result"], stored["attempts"]) == (True, 42, 1)
    assert paths.handler_runs() == 1
    assert paths.results.read_bytes() == before  # settling writes no journal line


# ----------------------------------------------------------------- contention and tampering


def test_a_second_runner_process_is_locked_out_of_the_bus(paths: Paths) -> None:
    """Process A holds the bus (a submitted job pending). Process B's runner on the same bus gets
    ``BusLocked`` before touching the results journal; after A exits, B runs the job once."""
    holder = subprocess.Popen(
        _command(paths.args(submit=[PARAMS], hold=True)),
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        refusal = _refused(paths.args(run=True))
        assert refusal["error"] == "BusLocked"
        assert not paths.results.exists() and paths.handler_runs() == 0
        out, _ = holder.communicate("go\n", timeout=TIMEOUT)
        assert holder.returncode == 0 and (_result(out) or {}).get("ok")
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=TIMEOUT)
    ran = _ok(paths.args(run=True))
    assert [o["result"] for o in ran["ran"]] == [42]
    assert paths.handler_runs() == 1


def test_a_results_journal_tampered_between_processes_is_refused(paths: Paths) -> None:
    """A result written by one process, then (1) edited in place, (2) re-chained without its
    ``job_started`` line: every later process refuses the journal and runs nothing."""
    _ok(paths.args(submit=[PARAMS], run=True))
    original = paths.results.read_text(encoding="utf-8")

    paths.results.write_text(original.replace('"result":42', '"result":43'), encoding="utf-8")
    assert paths.results.read_text(encoding="utf-8") != original
    refusal = _refused(paths.args(run=True))
    assert "JournalCorrupted" in refusal["mro"]

    paths.results.write_text(original, encoding="utf-8")
    kept = [e for e in AppendOnlyJournal(paths.results).entries if e.type != JOB_STARTED]
    paths.results.unlink()
    rechained = AppendOnlyJournal(paths.results)
    for entry in kept:
        rechained.append(entry.type, dict(entry.payload))
    refusal = _refused(paths.args(run=True))
    assert refusal["error"] == "JobResultsCorrupted"
    assert "not started" in refusal["message"]
    assert paths.handler_runs() == 1


# --------------------------------------------------------------- the worker loop (fake stages)


def test_an_exhausted_budget_stays_exhausted_in_the_next_process(tmp_path: Path) -> None:
    """Over budget across processes: process 1 runs until the TEST ONLY trial budget is spent
    (the loop halts ``BUDGET_EXHAUSTED``); process 2 restores the spent total and the halt from
    the audit, runs no round and writes nothing — a restart never resets a budget."""
    audit = tmp_path / "audit.jsonl"
    first = _ok({"mode": "loop", "audit": str(audit), "run": 10})
    assert first["statuses"] == ["COMPLETED", "COMPLETED", "COMPLETED", "BUDGET_EXHAUSTED"]
    assert (first["total_trials"], first["halted"]) == (3, "BUDGET_EXHAUSTED")
    before = audit.read_bytes()
    second = _ok({"mode": "loop", "audit": str(audit), "run": 10})
    assert (second["restored_trials"], second["halted"]) == (3, "BUDGET_EXHAUSTED")
    assert second["statuses"] == [] and second["record_hashes"] == first["record_hashes"]
    assert audit.read_bytes() == before


def test_a_worker_loop_killed_mid_round_stops_the_next_process(tmp_path: Path) -> None:
    """SIGKILL right after round 1's ``loop_round_started`` line: the next process refuses to run
    any round (``LoopHalted``: the interrupted round's spending is unknown) and writes nothing."""
    audit = tmp_path / "audit.jsonl"
    _killed({"mode": "loop", "audit": str(audit), "run": 3, "kill": "begin_round:1"})
    assert [e.type for e in AppendOnlyJournal(audit).entries] == [
        ROUND_STARTED,
        ROUND_RECORDED,
        ROUND_STARTED,
    ]
    before = audit.read_bytes()
    refusal = _refused({"mode": "loop", "audit": str(audit), "run": 1})
    assert refusal["error"] == "LoopHalted"
    assert "interrupted" in refusal["message"]
    assert audit.read_bytes() == before
