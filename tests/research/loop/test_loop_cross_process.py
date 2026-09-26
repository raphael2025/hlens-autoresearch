"""Phase 11 acceptance, cross-process: a durable research loop survives a real process death and
fails closed when another process reopens it (ADR-0049 durable composition; roadmap P11 "restart,
job replay, over-budget, corrupt logs and human approvals fail closed").

Every other restart test of the loop reopens the state directory **in the same interpreter**. Here
every write and every reopen is a separate ``python`` process (``cross_process_child``): the only
thing shared is the directory on disk, and a crash is a ``SIGKILL`` at a deterministic point
(module docs of the child) — no ``finally``, no ``atexit``, the kernel drops the flock.

The scenario and every number are ``test_loop_durable``'s (TEST ONLY): round 0, a human approval
of ``h_llm_0``, rounds 1-2. What each test proves is in its docstring. No production code is
changed; the one real gap found is pinned as ``xfail(strict=True)`` with its evidence.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.worker import LoopAuditLog
from apps.worker.loop import ROUND_RECORDED, ROUND_STARTED
from research.loop.durable import AUDIT_FILE, MEMORY_FILE, REVIEWS_FILE
from research.loop.memory import REVIEW_APPROVED
from research.persistence import AppendOnlyJournal
from tests.research.loop.cross_process_child import RESULT_PREFIX
from tests.research.loop.test_loop_durable import _rechain

REPO = Path(__file__).resolve().parents[3]
CHILD = "tests.research.loop.cross_process_child"
TIMEOUT = 300  # seconds per child (a round takes a few seconds; generous for a loaded machine)
KILLED = -signal.SIGKILL


def _command(args: dict[str, Any]) -> list[str]:
    return [sys.executable, "-m", CHILD, json.dumps(args)]


def _result(stdout: str) -> dict[str, Any] | None:
    lines = [line for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) <= 1, stdout
    return json.loads(lines[0].removeprefix(RESULT_PREFIX)) if lines else None


def _child(args: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """Run one step in a fresh interpreter; returns its exit code and its RESULT (if any)."""
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
    assert not result["ok"], "the reopening process was expected to refuse the directory"
    return result


def _killed(args: dict[str, Any]) -> None:
    code, result = _child(args)
    assert code == KILLED, (code, result)
    assert result is None  # it died at the armed point, before reporting anything


def _snapshot(state_dir: Path) -> dict[str, bytes]:
    """Every file under the directory (the bus included), except the flock file."""
    return {
        str(path.relative_to(state_dir)): path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


def _copy(source: Path, tmp_path: Path, name: str = "state") -> Path:
    target = tmp_path / name
    shutil.copytree(source, target)
    return target


def _audit_types(state_dir: Path) -> list[str]:
    return [e.type for e in AppendOnlyJournal(state_dir / AUDIT_FILE).entries]


class _Holder:
    """A child that keeps the durable loop (and its bus lock) open until released."""

    def __init__(self, args: dict[str, Any]) -> None:
        self.process = subprocess.Popen(
            _command({**args, "hold": True}),
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        line = self.process.stdout.readline()  # EOF ("") if the child died before READY
        if line.strip() != "READY":
            self.process.kill()
            _, err = self.process.communicate(timeout=TIMEOUT)
            raise AssertionError(f"the holder did not open the directory: {line!r} {err[-2000:]}")

    def release(self) -> dict[str, Any]:
        out, err = self.process.communicate("go\n", timeout=TIMEOUT)
        assert self.process.returncode == 0, err[-2000:]
        result = _result(out)
        assert result is not None and result["ok"], result
        return result

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate(timeout=TIMEOUT)


# ------------------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """One process, never interrupted: round 0, the approval, rounds 1-2."""
    state_dir = tmp_path_factory.mktemp("reference") / "state"
    result = _ok({"state_dir": str(state_dir), "run": 1, "approve": True, "then_run": 2})
    outcome = result["outcome"]
    assert len(outcome["record_hashes"]) == 3
    assert result["round_bus"] == outcome["record_hashes"]
    return state_dir, outcome


@pytest.fixture(scope="module")
def round0(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Round 0 written by one process that then exited cleanly."""
    state_dir = tmp_path_factory.mktemp("round0") / "state"
    _ok({"state_dir": str(state_dir), "run": 1})
    return state_dir


@pytest.fixture(scope="module")
def approved(round0: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``round0`` reopened by a second process in which a human approved ``h_llm_0``."""
    state_dir = _copy(round0, tmp_path_factory.mktemp("approved"))
    _ok({"state_dir": str(state_dir), "consumed": 1, "approve": True})
    return state_dir


# ----------------------------------------------------------------------------------- restarts


def test_three_processes_end_exactly_like_one_uninterrupted_process(
    approved: Path, reference: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """Round 0 (process 1), the approval (process 2), rounds 1-2 (process 3): the audit, trial
    ledger, lifecycle, sealed-OOS ledger, failures, approvals, lineage and the bus's round records
    equal the single-process run's."""
    state_dir = _copy(approved, tmp_path)
    result = _ok({"state_dir": str(state_dir), "consumed": 1, "then_run": 2})
    assert result["outcome"] == reference[1]
    assert result["round_bus"] == reference[1]["record_hashes"]


def test_a_process_killed_after_round_started_is_refused_as_interrupted(
    approved: Path, tmp_path: Path
) -> None:
    """SIGKILL right after the audit's ``loop_round_started`` line of round 1: another process
    refuses the directory (what the round spent is unknown) and writes nothing."""
    state_dir = _copy(approved, tmp_path)
    _killed({"state_dir": str(state_dir), "consumed": 1, "then_run": 1, "kill": "begin_round:1"})
    assert _audit_types(state_dir) == [ROUND_STARTED, ROUND_RECORDED, ROUND_STARTED]
    before = _snapshot(state_dir)
    for _ in range(2):  # refused every time, never "repaired" by a refused open
        refusal = _refused({"state_dir": str(state_dir), "consumed": 2})
        assert refusal["error"] == "LoopStateInconsistent"
        assert "interrupted" in refusal["message"]
        assert _snapshot(state_dir) == before


def test_a_process_killed_between_checkpoint_and_audit_record_is_refused(
    approved: Path, tmp_path: Path
) -> None:
    """SIGKILL after round 1's memory checkpoint line and before the audit recorded the round:
    the checkpoint names a round the audit never recorded — refused on reopening."""
    state_dir = _copy(approved, tmp_path)
    _killed({"state_dir": str(state_dir), "consumed": 1, "then_run": 1, "kill": "checkpoint:1"})
    assert _audit_types(state_dir) == [ROUND_STARTED, ROUND_RECORDED, ROUND_STARTED]
    memory = [e.type for e in AppendOnlyJournal(state_dir / MEMORY_FILE).entries]
    assert memory.count("round_memory") == 2  # one checkpoint more than recorded rounds
    before = _snapshot(state_dir)
    refusal = _refused({"state_dir": str(state_dir), "consumed": 2})
    assert refusal["error"] == "LoopStateInconsistent"
    assert "interrupted" in refusal["message"]
    assert _snapshot(state_dir) == before


def test_a_process_killed_between_audit_and_bus_is_caught_up_by_the_next_process(
    approved: Path, reference: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """SIGKILL after the audit recorded round 1 and before the bus saw it (its job also never
    acknowledged): the next process publishes round 1 from the audit, settles its job, runs round
    2, and the whole run equals the uninterrupted one — the one crash window that is recoverable."""
    state_dir = _copy(approved, tmp_path)
    _killed({"state_dir": str(state_dir), "consumed": 1, "then_run": 1, "kill": "round_publish:1"})
    assert len(LoopAuditLog(state_dir / AUDIT_FILE).records) == 2
    result = _ok({"state_dir": str(state_dir), "consumed": 2, "then_run": 1})
    assert result["outcome"] == reference[1]
    assert result["round_bus"] == reference[1]["record_hashes"]


# ------------------------------------------------------------------------------ human approvals


def test_a_process_killed_between_approval_and_its_checkpoint_is_refused(
    round0: Path, tmp_path: Path
) -> None:
    """SIGKILL after the approval was journaled and before its between-rounds checkpoint: the
    approval is on disk but no checkpoint names it, so another process refuses it (it is never
    silently accepted as a human decision)."""
    state_dir = _copy(round0, tmp_path)
    _killed({"state_dir": str(state_dir), "consumed": 1, "approve": True, "kill": "between_rounds"})
    reviews = [e.type for e in AppendOnlyJournal(state_dir / REVIEWS_FILE).entries]
    assert reviews[-1] == REVIEW_APPROVED
    before = _snapshot(state_dir)
    refusal = _refused({"state_dir": str(state_dir), "consumed": 1})
    assert refusal["error"] == "LoopStateInconsistent"
    assert "no between-rounds checkpoint names" in refusal["message"]
    assert _snapshot(state_dir) == before


def test_an_approval_forged_by_a_third_process_is_refused(
    reference: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    """A process outside the loop appends a valid-chain approval (``mallory``) of a pending draft:
    the next process that opens the directory refuses it."""
    state_dir = _copy(reference[0], tmp_path)
    _ok({"state_dir": str(state_dir), "forge_approval": "h_llm_1@1.0.0"})
    before = _snapshot(state_dir)
    refusal = _refused({"state_dir": str(state_dir), "consumed": 3})
    assert refusal["error"] == "LoopStateInconsistent"
    assert "no between-rounds checkpoint names" in refusal["message"]
    assert _snapshot(state_dir) == before


def test_a_rechained_approval_is_refused_by_another_process(approved: Path, tmp_path: Path) -> None:
    """The approval's reviewer rewritten with a valid new hash chain (the file alone looks
    untouched): the checkpoints written by the first process disagree — refused."""
    state_dir = _copy(approved, tmp_path)

    def swap(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "reviewer": "mallory"} if kind == REVIEW_APPROVED else payload

    _rechain(state_dir / REVIEWS_FILE, swap)
    refusal = _refused({"state_dir": str(state_dir), "consumed": 1})
    assert refusal["error"] == "LoopStateInconsistent"
    assert "reviews does not match" in refusal["message"]


# ---------------------------------------------------------------------------- corrupt / budget


def test_an_audit_edited_in_place_is_refused_by_another_process(
    approved: Path, tmp_path: Path
) -> None:
    """Round 0's recorded line edited without re-chaining: the reopening process refuses the
    journal itself (``JournalCorrupted``), never skips or repairs it."""
    state_dir = _copy(approved, tmp_path)
    path = state_dir / AUDIT_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    raw = json.loads(lines[1])
    assert raw["type"] == ROUND_RECORDED
    raw["payload"]["record"]["seed"] += 1
    lines[1] = json.dumps(raw) + "\n"
    path.write_text("".join(lines), encoding="utf-8")
    before = _snapshot(state_dir)
    refusal = _refused({"state_dir": str(state_dir), "consumed": 1})
    assert "JournalCorrupted" in refusal["mro"]
    assert _snapshot(state_dir) == before


@pytest.mark.parametrize("budget", ["bigger", "bigger_compute"])
def test_reopening_with_a_bigger_budget_is_refused_by_another_process(
    approved: Path, tmp_path: Path, budget: str
) -> None:
    """Over budget across processes: the budget is bound to the directory; a process that brings
    a larger one is refused before anything is written (raising it is a human decision)."""
    state_dir = _copy(approved, tmp_path)
    before = _snapshot(state_dir)
    refusal = _refused({"state_dir": str(state_dir), "consumed": 1, "budget": budget})
    assert refusal["error"] == "LoopStateInconsistent"
    assert "the loop budget" in refusal["message"] and "NEW state_dir" in refusal["message"]
    assert _snapshot(state_dir) == before


# --------------------------------------------------------------------------------- contention


def test_a_second_process_is_locked_out_while_the_first_holds_the_directory(
    approved: Path, tmp_path: Path
) -> None:
    """Process A holds the loop open (its own ``FileEventBus`` flock); process B opening the same
    directory is refused and changes no byte (since 2026-09-26 by the directory's own
    ``state.lock``, taken before the bus lock: ``LoopStateLocked``). Once A exits, B's open
    succeeds."""
    state_dir = _copy(approved, tmp_path)
    holder = _Holder({"state_dir": str(state_dir), "consumed": 1})
    try:
        before = _snapshot(state_dir)
        refusal = _refused({"state_dir": str(state_dir), "consumed": 1})
        assert refusal["error"] == "LoopStateLocked"
        assert _snapshot(state_dir) == before
        holder.release()
    finally:
        holder.kill()
    assert len(_ok({"state_dir": str(state_dir), "consumed": 1})["outcome"]["record_hashes"]) == 1


def test_a_killed_holder_releases_the_directory(approved: Path, tmp_path: Path) -> None:
    """Process A is SIGKILLed while holding the loop open: the kernel drops its flock, so a new
    process opens the directory without any manual unlock, and the state is intact."""
    state_dir = _copy(approved, tmp_path)
    holder = _Holder({"state_dir": str(state_dir), "consumed": 1})
    before = _snapshot(state_dir)
    holder.process.send_signal(signal.SIGKILL)
    holder.process.communicate(timeout=TIMEOUT)
    assert holder.process.returncode == KILLED
    assert _snapshot(state_dir) == before
    assert len(_ok({"state_dir": str(state_dir), "consumed": 1})["outcome"]["record_hashes"]) == 1


def test_a_second_process_with_an_injected_bus_is_locked_out_too(
    approved: Path, tmp_path: Path
) -> None:
    """Fixed 2026-09-26: ``open_state`` takes its own ``state.lock`` flock (was a strict xfail)."""
    state_dir = _copy(approved, tmp_path)
    holder = _Holder({"state_dir": str(state_dir), "consumed": 1})
    try:
        code, result = _child({"state_dir": str(state_dir), "consumed": 1, "bus": "memory"})
        assert code == 0 and result is not None
        assert not result["ok"], "a second writer opened a state_dir another process holds"
    finally:
        holder.kill()
