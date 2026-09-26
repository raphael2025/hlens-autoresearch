"""Child-process side of ``test_loop_cross_process.py`` (Phase 11 cross-process durability).

TEST ONLY. Run as ``python -m tests.research.loop.cross_process_child '<json>'`` from the
repository root; the parent test starts a **fresh interpreter** for every step, so nothing but the
files of the state directory is shared between the process that writes and the one that reopens.

The scenario is ``test_loop_durable``'s (same ``_config`` / ``_llm`` / ``DRAFT`` / ``REVIEWER``,
every number TEST ONLY). A step opens the durable loop (``open_synthetic_loop`` with a
``state_dir``), optionally runs rounds, optionally approves the LLM draft between rounds, and
prints one ``RESULT <json>`` line: ``{"ok": true, ...}`` or ``{"ok": false, "error": <type>,
"message": <text>}`` for a refusal (the refusal is the documented behaviour under test).

Deterministic crash points (``kill``): the child wraps one method **in its own process** (a
test-side monkeypatch, no production seam) so that it ``SIGKILL``s itself at exactly that point —
no ``finally``, no ``atexit``, no flush: the kernel drops its file locks and nothing else happens.

- ``begin_round:<i>``: right after the audit's ``loop_round_started`` line of round ``i``;
- ``checkpoint:<i>``: right after round ``i``'s memory checkpoint line, before the audit records it;
- ``round_publish:<i>``: after the audit recorded round ``i``, before the bus saw it;
- ``between_rounds``: after an approval was journaled, before its between-rounds checkpoint.

``hold``: after opening (and any rounds), print ``READY`` and block on stdin until the parent
writes a line (or closes the pipe); used to keep the loop — and its bus lock — open while another
process tries the same directory.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.worker import LoopAuditLog
from apps.worker.loop import ROUND_TOPIC
from core.contracts.event_bus import BusMessage
from infrastructure.event_bus import FileEventBus, InMemoryEventBus
from plugins.synthetic import RandomWalkMarket
from research.loop import DurableLoop, ReviewQueue, open_synthetic_loop
from research.loop.compose import BUS_DIR
from research.loop.durable import REVIEWS_FILE, MemoryCheckpoint
from tests.research.loop import loop_fixtures as fx
from tests.research.loop.test_loop_durable import DRAFT, REVIEWER, _config, _llm, _outcome

RESULT_PREFIX = "RESULT "


def _die() -> None:
    """A process death at this exact point (SIGKILL cannot be caught or cleaned up after)."""
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _arm(kill: str | None) -> None:
    if kill is None:
        return
    point, _, index_text = kill.partition(":")
    index = int(index_text) if index_text else -1
    if point == "begin_round":
        real_begin = LoopAuditLog.begin_round

        def begin_then_die(audit: LoopAuditLog, loop_id: str, round_index: int) -> None:
            real_begin(audit, loop_id, round_index)
            if round_index == index:
                _die()

        LoopAuditLog.begin_round = begin_then_die  # type: ignore[method-assign,assignment]
    elif point == "checkpoint":
        real_call = MemoryCheckpoint.__call__

        def checkpoint_then_die(checkpoint: MemoryCheckpoint, record: Any) -> None:
            real_call(checkpoint, record)
            if record.round_index == index:
                _die()

        MemoryCheckpoint.__call__ = checkpoint_then_die  # type: ignore[method-assign,assignment]
    elif point == "round_publish":
        real_publish = FileEventBus.publish

        def die_before_round(bus: FileEventBus, message: BusMessage) -> None:
            if message.topic == ROUND_TOPIC and message.key.endswith(f":{index}"):
                _die()
            real_publish(bus, message)

        FileEventBus.publish = die_before_round  # type: ignore[method-assign,assignment]
    elif point == "between_rounds":

        def die_instead(*_: Any) -> None:
            _die()

        MemoryCheckpoint.between_rounds = die_instead  # type: ignore[method-assign,assignment]
    else:
        raise ValueError(f"unknown kill point {kill!r}")


def _open(state_dir: Path, consumed: int, bus: str, budget: str) -> DurableLoop:
    config = _config()
    if budget == "bigger":  # a larger total trial budget: an over-budget reopening attempt
        config = _config(budget=replace(fx.TEST_ONLY_BUDGET, max_trials_total=1000))
    elif budget == "bigger_compute":
        config = _config(budget=replace(fx.TEST_ONLY_BUDGET, max_compute_seconds=Decimal(10**9)))
    opened: DurableLoop = open_synthetic_loop(
        config,
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus() if bus == "memory" else None,
        llm=_llm(consumed),
    )
    return opened


def _round_hashes(state_dir: Path) -> list[str]:
    with FileEventBus(state_dir / BUS_DIR) as bus:
        return [m.payload["record_hash"] for m in bus.poll("auditor", ROUND_TOPIC, 100)]


def outcome_json(opened: DurableLoop) -> dict[str, Any]:
    """``test_loop_durable.Outcome`` as JSON (every field's ``repr``; all deterministic)."""
    outcome = _outcome(opened.loop, opened.memory)
    return {
        "record_hashes": outcome.record_hashes,
        "total_usage": repr(outcome.total_usage),
        "trial_log": [repr(item) for item in outcome.trial_log],
        "family_trials": outcome.family_trials,
        "lifecycle": repr(outcome.lifecycle),
        "sealed": repr(outcome.sealed),
        "failures": outcome.failures,
        "approvals": [repr(item) for item in outcome.approvals],
        "lineage": outcome.lineage,
    }


def _step(args: dict[str, Any]) -> dict[str, Any]:
    state_dir = Path(args["state_dir"])
    if args.get("forge_approval"):  # a third process appends an approval outside the loop
        ReviewQueue(state_dir / REVIEWS_FILE).approve(args["forge_approval"], reviewer="mallory")
        return {"ok": True}
    _arm(args.get("kill"))
    opened = _open(
        state_dir, int(args.get("consumed", 0)), args.get("bus", "auto"), args.get("budget", "")
    )
    try:
        if args.get("run", 0):
            opened.loop.run_unattended(int(args["run"]))
        if args.get("approve"):
            opened.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
        if args.get("then_run", 0):
            opened.loop.run_unattended(int(args["then_run"]))
        result = {"ok": True, "outcome": outcome_json(opened)}
        if args.get("hold"):
            print("READY", flush=True)
            sys.stdin.readline()
    finally:
        opened.close()
    if args.get("bus", "auto") == "auto":
        result["round_bus"] = _round_hashes(state_dir)
    return result


def main(argv: list[str]) -> int:
    args = json.loads(argv[1])
    try:
        result = _step(args)
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
