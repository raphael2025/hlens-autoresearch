"""Phase 11 acceptance, cross-process: ``FileEventBus`` single-writer lock, crash windows and
tamper refusals across real processes (ADR-0044 implementation note, file-backed bus).

The bus's own tests open, close and reopen it inside one interpreter. Here every writer and every
reader is a separate ``python -c`` process (``CHILD`` below; small, no loop, no market), a crash is
a ``SIGKILL`` at a deterministic point (a wrap installed in the child only; no production seam),
and the only shared state is the bus directory (and the anchor file) on disk.

What is proven: a second process gets ``BusLocked`` while the first holds the directory, and gets
the lock once the first exits or is killed (the kernel drops the flock); a process killed inside
an ``ack`` or between a publish and its anchor line loses nothing on reopening (at least once);
a log or consumer state tampered with between processes is refused (``BusCorrupted``); and two
processes that bypass the bus and append to one journal without its lock are *detected* on the
next reopen (duplicate ``seq``) — the lock, not the journal, is what prevents it.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.contracts.event_bus import BusMessage
from infrastructure.event_bus import BusLocked, FileEventBus
from infrastructure.event_bus.journal import AppendOnlyJournal, JournalCorrupted

REPO = Path(__file__).resolve().parents[3]
TIMEOUT = 60
KILLED = -signal.SIGKILL
RESULT_PREFIX = "RESULT "
TOPIC = "orders"
CONSUMER = "auditor"

CHILD = r"""
import json, os, signal, sys
from pathlib import Path
from core.contracts.event_bus import BusMessage
from infrastructure.event_bus import FileEventBus
import infrastructure.event_bus.file as bus_file
from infrastructure.event_bus.journal import AppendOnlyJournal

def die():
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)

args = json.loads(sys.argv[1])
kill = args.get("kill")
if kill == "ack_before_replace":  # consumer state .tmp written and fsync'd, not yet renamed
    def replace_then_die(src, dst):
        if "consumers" in str(dst):
            die()
        raise AssertionError("unexpected rename")
    bus_file.os.replace = replace_then_die
elif kill == "publish_before_anchor":  # the log line is fsync'd, its anchor line is not
    def anchor_then_die(self, topic, log):
        die()
    FileEventBus._publish_anchor = anchor_then_die
result = {"ok": True}
try:
    if args["mode"] == "journal":  # bypasses the bus (and its lock) on purpose
        journal = AppendOnlyJournal(Path(args["path"]))
        if args.get("hold"):
            print("READY", flush=True)
            sys.stdin.readline()
        journal.append("line", {"writer": args["writer"]})
        result["seq"] = len(journal)
    else:
        anchor = Path(args["anchor"]) if args.get("anchor") else None
        with FileEventBus(Path(args["root"]), anchor=anchor) as bus:
            for text in args.get("publish", ()):
                bus.publish(BusMessage.build(args["topic"], text, {"text": text}))
            if args.get("hold"):
                print("READY", flush=True)
                sys.stdin.readline()
                for text in args.get("publish_after", ()):
                    bus.publish(BusMessage.build(args["topic"], text, {"text": text}))
            for message in bus.poll(args["consumer"], args["topic"], 100)[: args.get("ack", 0)]:
                bus.ack(args["consumer"], args["topic"], message.message_id)
            result["pending"] = [m.key for m in bus.poll(args["consumer"], args["topic"], 100)]
except Exception as exc:
    result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
print("RESULT " + json.dumps(result), flush=True)
"""


def _command(args: dict[str, Any]) -> list[str]:
    return [sys.executable, "-c", CHILD, json.dumps(args)]


def _result(stdout: str) -> dict[str, Any] | None:
    lines = [line for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) <= 1, stdout
    return json.loads(lines[0].removeprefix(RESULT_PREFIX)) if lines else None


def _child(args: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    done = subprocess.run(
        _command(args), cwd=REPO, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )
    assert not done.stderr or done.returncode != 0, done.stderr
    return done.returncode, _result(done.stdout)


def _bus(root: Path, **extra: Any) -> dict[str, Any]:
    return {"mode": "bus", "root": str(root), "topic": TOPIC, "consumer": CONSUMER, **extra}


def _ok(args: dict[str, Any]) -> dict[str, Any]:
    code, result = _child(args)
    assert code == 0 and result is not None and result["ok"], (code, result)
    return result


def _refused(args: dict[str, Any]) -> dict[str, Any]:
    code, result = _child(args)
    assert code == 0 and result is not None and not result["ok"], (code, result)
    return result


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != ".lock"
    }


class _Holder:
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
        line = self.process.stdout.readline()
        if line.strip() != "READY":
            self.stop()
            raise AssertionError(f"the holder did not start: {line!r}")

    def release(self) -> dict[str, Any]:
        out, err = self.process.communicate("go\n", timeout=TIMEOUT)
        assert self.process.returncode == 0, err
        result = _result(out)
        assert result is not None and result["ok"], result
        return result

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate(timeout=TIMEOUT)


def _keys(root: Path) -> list[str]:
    with FileEventBus(root) as bus:
        return [m.key for m in bus.poll(CONSUMER, TOPIC, 100)]


# ----------------------------------------------------------------------------------- the lock


def test_a_second_process_gets_bus_locked_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "bus"
    holder = _Holder(_bus(root, publish=["m1"], publish_after=["m2"]))
    try:
        before = _snapshot(root)
        with pytest.raises(BusLocked):  # this pytest process is another process too
            FileEventBus(root)
        refusal = _refused(_bus(root, publish=["intruder"], ack=1))
        assert refusal["error"] == "BusLocked"
        assert _snapshot(root) == before
        assert holder.release()["pending"] == ["m1", "m2"]
    finally:
        holder.stop()
    assert _keys(root) == ["m1", "m2"]  # the lock is free again; nothing of the intruder


def test_a_killed_holder_releases_the_lock_and_loses_nothing(tmp_path: Path) -> None:
    root = tmp_path / "bus"
    holder = _Holder(_bus(root, publish=["m1"]))
    holder.process.send_signal(signal.SIGKILL)
    holder.process.communicate(timeout=TIMEOUT)
    assert holder.process.returncode == KILLED
    assert _ok(_bus(root))["pending"] == ["m1"]  # opened by a new process, no manual unlock


# ------------------------------------------------------------------------------- crash windows


def test_a_process_killed_inside_ack_redelivers_on_reopening(tmp_path: Path) -> None:
    """Killed after the new consumer state's ``.tmp`` was written and before the atomic rename:
    the previous state stands, the message is delivered again (at least once), and acking it in
    the next process works (the stale ``.tmp`` is ignored, then replaced)."""
    root = tmp_path / "bus"
    _ok(_bus(root, publish=["m1", "m2"]))
    code, result = _child(_bus(root, ack=1, kill="ack_before_replace"))
    assert (code, result) == (KILLED, None)
    assert any(p.name.endswith(".tmp") for p in (root / "consumers").iterdir())
    assert _ok(_bus(root))["pending"] == ["m1", "m2"]
    assert _ok(_bus(root, ack=1))["pending"] == ["m2"]
    assert _keys(root) == ["m2"]


def test_a_process_killed_between_publish_and_anchor_is_reanchored(tmp_path: Path) -> None:
    """The anchor's one legitimate crash window: the log line is on disk, its anchor line is not.
    The next process accepts the longer log and re-anchors it; a later truncation is refused."""
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _ok(_bus(root, anchor=str(anchor), publish=["m1"]))
    code, _ = _child(_bus(root, anchor=str(anchor), publish=["m2"], kill="publish_before_anchor"))
    assert code == KILLED
    assert len(AppendOnlyJournal(anchor)) == 1  # m2 was never anchored
    assert _ok(_bus(root, anchor=str(anchor)))["pending"] == ["m1", "m2"]
    assert [e.payload["length"] for e in AppendOnlyJournal(anchor).entries] == [1, 2]
    log = root / "topics" / f"{TOPIC}.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    log.write_text(lines[0], encoding="utf-8")
    refusal = _refused(_bus(root, anchor=str(anchor)))
    assert refusal["error"] == "BusCorrupted" and "dropped" in refusal["message"]


# ------------------------------------------------------------------------------------ tampering


def test_a_log_tampered_between_processes_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "bus"
    _ok(_bus(root, publish=["m1", "m2"], ack=1))
    log = root / "topics" / f"{TOPIC}.jsonl"
    original = log.read_text(encoding="utf-8")
    log.write_text(original.replace('"text":"m1"', '"text":"m9"'), encoding="utf-8")
    before = _snapshot(root)
    refusal = _refused(_bus(root))
    assert refusal["error"] == "BusCorrupted"
    assert _snapshot(root) == before

    log.write_text(original, encoding="utf-8")
    [state] = (root / "consumers").iterdir()
    raw = json.loads(state.read_text(encoding="utf-8"))
    raw["offset"] = 0  # "un-acknowledge" m1 without re-hashing
    state.write_text(json.dumps(raw), encoding="utf-8")
    refusal = _refused(_bus(root))
    assert refusal["error"] == "BusCorrupted" and "edited" in refusal["message"]


# ------------------------------------------------------------------ journals without the lock


def test_unlocked_journal_writers_are_detected_on_reopen_not_prevented(tmp_path: Path) -> None:
    """Two processes append to one journal **without** a ``FileEventBus`` (so without its lock):
    the stale writer's line repeats a ``seq``. Nothing stops the write (the journal only refuses a
    file that shrank), but the next reader refuses the file — it is never silently accepted.
    This is why every journal writer sits behind a single-writer lock (see also the xfail in
    ``tests/research/loop/test_loop_cross_process.py``)."""
    path = tmp_path / "shared.jsonl"
    AppendOnlyJournal(path).append("line", {"writer": "setup"})
    holder = _Holder({"mode": "journal", "path": str(path), "writer": "stale"})
    try:
        assert _ok({"mode": "journal", "path": str(path), "writer": "fresh"})["seq"] == 2
        assert holder.release()["seq"] == 2  # the stale process also believes it wrote seq 2
    finally:
        holder.stop()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    with pytest.raises(JournalCorrupted, match="wrong sequence number"):
        AppendOnlyJournal(path)


def test_the_parent_can_publish_what_a_child_reads(tmp_path: Path) -> None:
    """Sanity of the harness: a message published here is read by a fresh process."""
    root = tmp_path / "bus"
    with FileEventBus(root) as bus:
        bus.publish(BusMessage.build(TOPIC, "p1", {"text": "p1"}))
    assert _ok(_bus(root))["pending"] == ["p1"]
