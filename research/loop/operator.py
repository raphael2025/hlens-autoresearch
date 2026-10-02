"""Finite synthetic-loop operator CLI (ADR-0074 §1 / §3.3 / §4 / §7 / §8).

Usage (the only command; no defaults, no infinite or daemon mode)::

    python -m research.loop.operator run --config PATH --rounds N

One invocation runs **at most** ``N`` new rounds of one durable synthetic loop and exits; running
continuously is an external scheduler's job, re-invoking the same command with the same
configuration (ADR-0074 §1.2 / §5.4). ``--rounds`` bounds this process only; the ``LoopBudget`` in
the configuration stays the loop's lifetime limit and is never raised or reset here (§5.3).

**Before any state is opened** (every refusal is exit code 2 unless noted):

1. ``load_operator_config`` parses and verifies the strict TOML v1 configuration, its hash-bound
   artifacts and the ADR-0062 freeze registry (a registry I/O / corruption / lock fault is 5);
2. ``build_providers`` constructs the six allowlisted providers and verifies their descriptors
   (§3.1 / §3.2); ``OperatorConfig.build`` compiles the one ``SyntheticLoopConfig``;
3. ``code_commit`` must equal the full 40-hex HEAD of the repository this module runs from, whose
   tracked worktree must be clean; ``environment_lock`` must equal
   ``uv.lock sha256=<hex>;python=<platform.python_version()>;platform=<sys.platform>/<machine>``
   recomputed from that repository's ``uv.lock`` bytes (§3.3). Git runs without the caller's
   ``GIT_*`` environment, without optional index locks and without an fsmonitor hook;
4. the state and bus anchors are re-checked: resolved exactly as loaded, outside ``state_dir`` (and
   so outside ``state_dir/bus``), distinct, not the same file, regular single-link files or new
   files in an existing directory (§4.1 / §4.3);
5. ``state_dir`` must be new (missing, empty, or only the v5 creation window ``open_state``
   finishes) or a recognized operator **v5** state whose header fingerprint is exactly this
   configuration's — a v3 / v4 directory or a foreign one is never taken over (§4.2 / §5.2).

**Run.** ``open_synthetic_loop`` opens the state with the explicit ``operator_identity``, the
configured ``state_anchor`` and ``bus_anchor`` and its own bus. Every verified audit record is then
written, idempotently, with ``write_research_loop_rounds`` (the §7.1 catch-up after a crash
between the audit fsync and the report). Rounds then run one at a time (``run_unattended(1)``);
each new record's report is written and printed before the next round starts. The Phase 6 matrix
sink ``run_unattended_and_report`` is deliberately not used (§7.2). ``DurableLoop.close()`` always
runs.

**Signals.** SIGINT / SIGTERM only request a stop at the next round boundary: the round in flight
finishes, is recorded, anchored and reported, then the process exits 0 (§7.3). A hard kill leaves
an interrupted round that the next invocation refuses (exit 4).

**Exit codes** (§8): ``0`` the batch ran (or stopped at a boundary on a signal) and every report
was written; ``2`` command / configuration / provider / identity / path / state-identity refusal,
nothing run; ``3`` the loop is halted (budget or another halting status, record kept); ``4`` an
interrupted round or ADR-0070 recovery-required — a human reviews it and a new ``loop_id`` /
``state_dir`` is used; ``5`` state / bus / anchor / report I/O, corruption, verification failure or
writer lock. stdout carries only ``key=value`` summary lines (no environment values, credentials
or configuration content); refusals and faults go to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Final

from apps.worker.jobs import JobInterrupted
from apps.worker.loop import LoopAuditLog, LoopHalted, LoopRecord, ResearchLoop, StageStatus
from core.domain.base import canonical_json, content_hash
from infrastructure.event_bus import BusLocked
from research.loop.compose import BUS_DIR, DurableLoop, loop_fingerprint, open_synthetic_loop
from research.loop.durable import (
    AUDIT_FILE,
    FAILURES_FILE,
    LEDGER_FILE,
    LINEAGE_FILE,
    LOCK_FILE,
    LOOP_STATE_OPENED,
    MEMORY_FILE,
    OPERATOR_STATE_VERSION,
    PLAN_ADMISSION_FILE,
    REVIEWS_FILE,
    ROUND_MEMORY,
    SEALED_OOS_FILE,
    FileAnchor,
    LoopStateLocked,
    _line_heads,
)
from research.loop.operator_config import (
    CompiledOperatorConfig,
    FreezeRegistryFault,
    OperatorConfigError,
    OperatorPaths,
    load_operator_config,
)
from research.loop.operator_providers import OperatorProviderError, build_providers
from research.persistence import AppendOnlyJournal, JournalEntry
from research.reports import ReportConflict, write_research_loop_rounds
from research.strategies.failure_registry import FailureRegistry

__all__ = [
    "EXIT_FAULT",
    "EXIT_HALTED",
    "EXIT_OK",
    "EXIT_RECOVERY_REQUIRED",
    "EXIT_REFUSED",
    "OperatorRefused",
    "RecoveryRequired",
    "StateFault",
    "StopRequest",
    "boundary_stop",
    "check_anchor_paths",
    "check_code_identity",
    "check_state_dir",
    "current_code_commit",
    "current_environment_lock",
    "emit",
    "fail",
    "main",
    "needs_review",
    "preparation_failure",
    "run_durable",
    "run_rounds",
]

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 2
EXIT_HALTED: Final = 3
EXIT_RECOVERY_REQUIRED: Final = 4
EXIT_FAULT: Final = 5

#: The repository this module runs from: ``code_commit`` / ``environment_lock`` bind to it.
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT_SECONDS: Final = 60
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")
_ROUNDS_RE: Final = re.compile(r"[1-9][0-9]*")
#: Every entry ``open_state`` / the composition's own bus may leave in a state directory.
_STATE_ENTRIES: Final = frozenset(
    {
        AUDIT_FILE,
        MEMORY_FILE,
        LEDGER_FILE,
        PLAN_ADMISSION_FILE,
        SEALED_OOS_FILE,
        LINEAGE_FILE,
        REVIEWS_FILE,
        FAILURES_FILE,
        LOCK_FILE,
        BUS_DIR,
    }
)


class OperatorRefused(ValueError):
    """Refused before any state was opened (exit code 2): command, identity, path or state."""


class StateFault(RuntimeError):
    """A state file could not be read or verified during the pre-open check (exit code 5)."""


class RecoveryRequired(RuntimeError):
    """The verified audit has a fail-stop condition requiring human review (exit code 4)."""


# ---- summary --------------------------------------------------------------------------------


def _line(text: object) -> str:
    return " ".join(str(text).split())  # one line, no control characters


def emit(key: str, value: object) -> None:
    print(f"{key}={_line(value)}", flush=True)


def fail(code: int, what: str, exc: BaseException) -> int:
    print(f"operator: {what} (exit {code}): {type(exc).__name__}: {_line(exc)}", file=sys.stderr)
    return code


# ---- command line ---------------------------------------------------------------------------


def _positive_rounds(text: str) -> int:
    """``--rounds``: a plain positive decimal integer (no sign, no leading zero, no space)."""
    if _ROUNDS_RE.fullmatch(text) is None:
        raise argparse.ArgumentTypeError("must be a positive integer such as 1 or 24")
    return int(text)


def _config_path(text: str) -> Path:
    if not text or text != text.strip() or "\x00" in text:
        raise argparse.ArgumentTypeError("must be a non-empty path")
    return Path(text)


class _Once(argparse.Action):
    """Refuse an option given twice (argparse would silently keep the last one)."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be given only once")
        setattr(namespace, self.dest, values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.loop.operator",
        description="Run a bounded batch of the ADR-0074 synthetic research loop.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="run")
    run = commands.add_parser(
        "run",
        help="run at most N new rounds, then exit",
        description="Run at most N new rounds of the configured loop, then exit.",
        allow_abbrev=False,
    )
    run.add_argument(
        "--config", required=True, type=_config_path, action=_Once, help="operator TOML file"
    )
    run.add_argument(
        "--rounds",
        required=True,
        type=_positive_rounds,
        action=_Once,
        help="maximum number of new rounds in this invocation (positive integer)",
    )
    return parser


# ---- code / environment identity (ADR-0074 §3.3) --------------------------------------------


def _git(*args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    command = [
        "git",
        "-C",
        str(_REPO_ROOT),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        *args,
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, env=env, timeout=_GIT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperatorRefused(f"git {args[0]} could not run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        raise OperatorRefused(f"git {args[0]} failed with exit status {done.returncode}")
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OperatorRefused(f"git {args[0]} printed non-UTF-8 output") from exc


def current_code_commit() -> str:
    """The full HEAD commit of this module's repository; refused unless the tracked worktree is
    clean (staged or unstaged changes to tracked files, submodules included)."""
    toplevel = _git("rev-parse", "--show-toplevel").strip()
    if not toplevel or Path(toplevel).resolve() != _REPO_ROOT:
        raise OperatorRefused("the operator is not running from the root of its Git worktree")
    head = _git("rev-parse", "--verify", "HEAD^{commit}").strip()
    if _COMMIT_RE.fullmatch(head) is None:
        raise OperatorRefused("HEAD is not a full 40-character lowercase commit id")
    changes = _git(
        "status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=none"
    )
    if changes:
        count = len([item for item in changes.split("\0") if item])
        raise OperatorRefused(f"the tracked worktree has uncommitted changes ({count} entries)")
    return head


def current_environment_lock() -> str:
    """The canonical ADR-0074 §3.3 environment lock of this process and repository."""
    try:
        data = (_REPO_ROOT / "uv.lock").read_bytes()
    except OSError as exc:
        raise OperatorRefused(f"cannot read the repository uv.lock ({type(exc).__name__})") from exc
    return (
        f"uv.lock sha256={hashlib.sha256(data).hexdigest()};"
        f"python={platform.python_version()};"
        f"platform={sys.platform}/{platform.machine()}"
    )


def check_code_identity(code_commit: str, environment_lock: str) -> None:
    head = current_code_commit()
    if code_commit != head:
        raise OperatorRefused(f"code_commit {code_commit} is not the current HEAD {head}")
    actual = current_environment_lock()
    if environment_lock != actual:
        raise OperatorRefused(
            f"environment_lock does not match this environment (recomputed: {actual})"
        )


# ---- anchors and state directory (ADR-0074 §4 / §5.2) ---------------------------------------


def check_anchor_paths(paths: OperatorPaths) -> None:
    """Re-check the anchors right before the state is opened (``load_operator_config`` already
    refused overlapping, nested and aliased ``[paths]``; this closes a change since then)."""
    state = paths.state_dir
    if state.resolve() != state:
        raise OperatorRefused("state_dir no longer resolves to the configured path")
    if os.path.lexists(state) and not stat.S_ISDIR(os.lstat(state).st_mode):
        raise OperatorRefused("state_dir exists and is not a directory")
    anchors = (("state_anchor", paths.state_anchor), ("bus_anchor", paths.bus_anchor))
    for label, anchor in anchors:
        if anchor.resolve() != anchor:
            raise OperatorRefused(f"{label} no longer resolves to the configured path")
        if anchor.is_relative_to(state):
            raise OperatorRefused(f"{label} lies inside state_dir (or its bus directory)")
        if not anchor.parent.is_dir():
            raise OperatorRefused(f"{label}'s directory does not exist")
        if os.path.lexists(anchor):
            info = os.lstat(anchor)
            if not stat.S_ISREG(info.st_mode):
                raise OperatorRefused(f"{label} exists and is not a regular file")
            if info.st_nlink != 1:
                raise OperatorRefused(f"{label} is hardlinked to another file")
    if paths.state_anchor == paths.bus_anchor:
        raise OperatorRefused("state_anchor and bus_anchor are the same path")
    if (
        paths.state_anchor.exists()
        and paths.bus_anchor.exists()
        and paths.state_anchor.samefile(paths.bus_anchor)
    ):
        raise OperatorRefused("state_anchor and bus_anchor are the same file")


def _journal_entries(path: Path) -> tuple[JournalEntry, ...]:
    """A state journal's verified entries, read only (``StateFault`` when unreadable)."""
    try:
        return AppendOnlyJournal(path).entries
    except Exception as exc:  # noqa: BLE001 - any read / verification failure is a state fault
        raise StateFault(f"{path.name} cannot be read or verified: {exc}") from exc


def _check_state_anchor(
    state_dir: Path, anchor_path: Path, memory_entries: Sequence[JournalEntry]
) -> None:
    """Verify the external state head against its named state prefix without opening state."""
    try:
        anchored = FileAnchor(anchor_path).load()
        audit = LoopAuditLog(state_dir / AUDIT_FILE)
    except Exception as exc:  # noqa: BLE001 - malformed anchor/audit is durable-state corruption
        raise StateFault(f"the state anchor or audit cannot be replayed: {exc}") from exc
    records = audit.records
    if anchored is None:
        if len(memory_entries) > 1 or records:
            raise StateFault("the state anchor is empty or lost beside recorded state")
        return  # header-only v5 creation window
    if anchored.memory_seq > len(memory_entries) or anchored.rounds > len(records):
        raise StateFault("the state directory is behind its external state anchor")
    entry = memory_entries[anchored.memory_seq - 1]
    if (
        entry.hash != anchored.memory_head
        or canonical_json(_line_heads(entry)) != canonical_json(anchored.heads)
        or sum(1 for item in memory_entries[: anchored.memory_seq] if item.type == ROUND_MEMORY)
        != anchored.rounds
        or (anchored.rounds > 0 and records[anchored.rounds - 1].record_hash != anchored.audit_head)
    ):
        raise StateFault("the external state anchor disagrees with its state history prefix")

    positions = anchored.heads
    if not positions:
        return
    expected_files = {
        "trial_ledger": LEDGER_FILE,
        "sealed_oos": SEALED_OOS_FILE,
        "lineage": LINEAGE_FILE,
        "reviews": REVIEWS_FILE,
        "plan_admission": PLAN_ADMISSION_FILE,
    }
    expected_keys = {*expected_files, "failures"}
    if set(positions) != expected_keys:
        raise StateFault("the state anchor has an unknown or incomplete journal-head set")
    for name, filename in expected_files.items():
        position = positions[name]
        if not isinstance(position, Mapping) or set(position) != {"seq", "hash"}:
            raise StateFault(f"the state anchor has an invalid {name} position")
        seq, expected_hash = position["seq"], position["hash"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise StateFault(f"the state anchor has an invalid {name} sequence")
        path = state_dir / filename
        entries = _journal_entries(path) if path.exists() else ()
        actual_hash = entries[seq - 1].hash if 0 < seq <= len(entries) else None
        if (
            seq > len(entries)
            or (seq == 0 and expected_hash != "0" * 64)
            or (seq > 0 and actual_hash != expected_hash)
        ):
            raise StateFault(f"{name} is behind or differs from its anchored position")
    failure_position = positions["failures"]
    if not isinstance(failure_position, Mapping) or set(failure_position) != {
        "count",
        "digest",
    }:
        raise StateFault("the state anchor has an invalid failures position")
    try:
        failures = FailureRegistry(state_dir / FAILURES_FILE).records()
        failure_hashes = [record.content_hash() for record in failures]
    except Exception as exc:  # noqa: BLE001 - malformed failure history is corruption
        raise StateFault(f"the failure registry cannot be replayed: {exc}") from exc
    count, digest = failure_position["count"], failure_position["digest"]
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or count > len(failure_hashes)
        or content_hash(failure_hashes[:count]) != digest
    ):
        raise StateFault("the failure registry differs from its anchored position")


def _check_bus_anchor(state_dir: Path, anchor_path: Path) -> None:
    """Verify every anchored topic prefix read-only; a longer unanchored tail is a valid crash."""
    topic_root = state_dir / BUS_DIR / "topics"
    if not anchor_path.exists():
        raise StateFault("the external bus anchor is missing for an existing state")
    entries = _journal_entries(anchor_path)
    anchored: dict[str, tuple[int, str]] = {}
    for entry in entries:
        payload = entry.payload
        topic = payload.get("topic")
        length = payload.get("length")
        head_hash = payload.get("head_hash")
        if (
            entry.type != "topic_head"
            or set(payload) != {"topic", "length", "head_hash"}
            or not isinstance(topic, str)
            or re.fullmatch(r"[a-z][a-z0-9_.]*", topic) is None
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
            or not isinstance(head_hash, str)
        ):
            raise StateFault("the external bus anchor contains an invalid topic head")
        previous = anchored.get(topic)
        if previous is not None and length <= previous[0]:
            raise StateFault(f"the external bus anchor moves topic {topic!r} backwards")
        anchored[topic] = (length, head_hash)
    for topic, (length, head_hash) in anchored.items():
        path = topic_root / f"{topic}.jsonl"
        topic_entries = _journal_entries(path) if path.exists() else ()
        if length > len(topic_entries) or topic_entries[length - 1].hash != head_hash:
            raise StateFault(f"bus topic {topic!r} is behind or differs from its anchor")


def check_state_dir(paths: OperatorPaths, fingerprint: Mapping[str, Any]) -> bool:
    """Refuse a ``state_dir`` that is neither new nor this configuration's operator v5 state;
    return whether it already holds a state header. ``open_state`` repeats checks under the
    directory lock; this read-only pass classifies foreign identity, corruption and recovery
    first."""
    state_dir = paths.state_dir
    if not state_dir.exists():
        for label, anchor in (
            ("state_anchor", paths.state_anchor),
            ("bus_anchor", paths.bus_anchor),
        ):
            if anchor.exists() and _journal_entries(anchor):
                raise OperatorRefused(
                    f"{label} already contains state, but state_dir is missing; use a new anchor"
                )
        return False
    unknown = sorted(
        entry.name for entry in state_dir.iterdir() if entry.name not in _STATE_ENTRIES
    )
    if unknown:
        raise StateFault(
            f"state_dir is not a loop state directory (unexpected entries: {unknown[:5]})"
        )
    for entry in state_dir.iterdir():
        info = os.lstat(entry)
        if entry.name == BUS_DIR:
            if not stat.S_ISDIR(info.st_mode):
                raise StateFault("state_dir's bus entry is not a real directory")
            for bus_entry in entry.rglob("*"):
                bus_info = os.lstat(bus_entry)
                if stat.S_ISDIR(bus_info.st_mode):
                    continue
                if not stat.S_ISREG(bus_info.st_mode) or bus_info.st_nlink != 1:
                    raise StateFault("state_dir's bus contains a non-regular or aliased entry")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StateFault(f"state_dir entry {entry.name!r} is not a regular, unaliased file")
    memory = state_dir / MEMORY_FILE
    children = tuple(state_dir.iterdir())
    if not children:
        for label, anchor in (
            ("state_anchor", paths.state_anchor),
            ("bus_anchor", paths.bus_anchor),
        ):
            if anchor.exists() and _journal_entries(anchor):
                raise OperatorRefused(
                    f"{label} already contains state, but state_dir is empty; use a new anchor"
                )
        return False
    if not memory.is_file():
        raise StateFault(
            "a non-empty state_dir without its memory header is not a recognized operator state"
        )
    entries = _journal_entries(memory)
    if not entries:
        raise StateFault("state_dir's memory journal is empty and has no operator header")
    header = entries[0]
    if header.type != LOOP_STATE_OPENED:
        raise StateFault("state_dir's memory journal does not start with a state header")
    version = header.payload.get("state_version")
    if type(version) is not int:
        raise StateFault("state_dir's memory header has no valid state_version")
    if version in {3, 4}:
        raise OperatorRefused(
            f"state_dir is a v{version!r} loop state: the operator only creates / opens v"
            f"{OPERATOR_STATE_VERSION} and never takes over or migrates v3 / v4 directories"
        )
    if version != OPERATOR_STATE_VERSION:
        raise StateFault(f"state_dir has unknown state version {version!r}")
    if set(header.payload) != {"state_version", "fingerprint"}:
        raise StateFault("state_dir's operator state header has unexpected fields")
    recorded = header.payload.get("fingerprint")
    if not isinstance(recorded, Mapping):
        raise StateFault("state_dir's operator state header has no fingerprint object")
    if canonical_json(recorded) != canonical_json(fingerprint):
        changed = sorted(
            key
            for key in set(recorded) | set(fingerprint)
            if canonical_json(recorded.get(key)) != canonical_json(fingerprint.get(key))
        )
        raise OperatorRefused(
            "state_dir belongs to another operator configuration (fields that differ: "
            f"{changed}); a changed configuration needs a new loop_id and state_dir"
        )
    for label, anchor in (
        ("state_anchor", paths.state_anchor),
        ("bus_anchor", paths.bus_anchor),
    ):
        if not anchor.is_file():
            raise StateFault(f"{label} is missing for an existing operator state")
    _check_state_anchor(state_dir, paths.state_anchor, entries)
    _check_bus_anchor(state_dir, paths.bus_anchor)
    audit = LoopAuditLog(state_dir / AUDIT_FILE)
    if audit.open_round is not None:
        raise RecoveryRequired(
            f"round {audit.open_round} was started but never recorded; human review is required"
        )
    records = audit.records
    if records and any(
        stage.name == "experiment" and stage.status is StageStatus.FAILED
        for stage in records[-1].stages
    ):
        raise RecoveryRequired(
            "the last round has a failed experiment stage; ADR-0070 requires human review"
        )
    return True


def _expected_fingerprint(compiled: CompiledOperatorConfig) -> Mapping[str, Any]:
    """Exactly what ``open_synthetic_loop`` writes into / checks against the v5 header (no LLM:
    ``llm_content_fingerprint(None)`` adds nothing), as plain JSON data."""
    fingerprint = {
        **loop_fingerprint(compiled.loop_config),
        "operator_identity": compiled.operator_identity,
    }
    loaded: Mapping[str, Any] = json.loads(canonical_json(fingerprint))
    return loaded


def needs_review(state_dir: Path) -> bool:
    """After ``open_synthetic_loop`` refused: whether the audit shows an interrupted round or an
    ADR-0070 failed experiment (exit 4) rather than corruption (exit 5). Read only."""
    try:
        audit = LoopAuditLog(state_dir / AUDIT_FILE)
    except Exception:  # noqa: BLE001 - an unreadable audit is a fault, not a review case
        return False
    if audit.open_round is not None:
        return True
    records = audit.records
    return bool(records) and any(
        stage.name == "experiment" and stage.status is StageStatus.FAILED
        for stage in records[-1].stages
    )


# ---- signals (ADR-0074 §7.3) ----------------------------------------------------------------


class StopRequest:
    """Set by SIGINT / SIGTERM; read only at round boundaries."""

    def __init__(self) -> None:
        self.signal_name: str | None = None

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        if self.signal_name is None:
            self.signal_name = signal.Signals(signum).name
            os.write(2, b"operator: stop requested; finishing the round in flight first\n")


@contextmanager
def boundary_stop() -> Iterator[StopRequest]:
    request = StopRequest()
    previous = {sig: signal.signal(sig, request) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield request
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)


# ---- run ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Prepared:
    compiled: CompiledOperatorConfig
    existing: bool


def _prepare(config: Path) -> _Prepared:
    """Everything that must pass before the state directory is opened (module docs, 1-5)."""
    operator_config = load_operator_config(config)
    providers = build_providers(operator_config)
    compiled = operator_config.build(providers)
    if compiled.operator_identity != operator_config.operator_identity:
        raise OperatorRefused("the compiled operator identity is not the configuration's")
    wiring = compiled.loop_config.wiring
    check_code_identity(wiring.code_commit, wiring.environment_lock)
    check_anchor_paths(compiled.paths)
    existing = check_state_dir(compiled.paths, _expected_fingerprint(compiled))
    return _Prepared(compiled, existing)


def _write_reports(root: Path, records: Sequence[LoopRecord]) -> tuple[Path, ...]:
    """The records' round reports (ADR-0074 §7.1); raises on a writer conflict or bad record."""
    return tuple(written.path for written in write_research_loop_rounds(root, records))


def _final_state(loop: ResearchLoop) -> tuple[int, str]:
    """The exit code and outcome the loop's own state implies (ADR-0074 §8)."""
    if loop.recovery_required is not None:
        return EXIT_RECOVERY_REQUIRED, "recovery_required"
    if loop.stopped is not None:
        interrupted = loop.audit.open_round is not None
        return (EXIT_RECOVERY_REQUIRED, "interrupted") if interrupted else (EXIT_FAULT, "stopped")
    if loop.halted is not None:
        return EXIT_HALTED, "halted"
    return EXIT_OK, "completed"


def _summarize_loop(loop: ResearchLoop, code: int, outcome: str, ran: int, requested: int) -> None:
    emit("rounds_requested", requested)
    emit("rounds_run", ran)
    emit("rounds_recorded_total", len(loop.audit.records))
    emit("total_usage", canonical_json(loop.total_usage.payload()))
    emit("outcome", outcome)
    if loop.halted is not None:
        emit("halted", loop.halted.value)
    if loop.recovery_required is not None:
        emit("recovery_required", loop.recovery_required)
    if loop.stopped is not None:
        emit("stopped", loop.stopped)
    if code == EXIT_HALTED:
        emit("action", "halted: a new budget or configuration needs a new loop_id and state_dir")
    elif code == EXIT_RECOVERY_REQUIRED:
        emit(
            "action",
            "human review required: do not rerun this state_dir; keep its audit, ledgers and "
            "failure evidence and use a new loop_id and state_dir",
        )


def run_rounds(durable: DurableLoop, reports_root: Path, rounds: int, stop: StopRequest) -> int:
    loop = durable.loop
    records = loop.audit.records
    try:
        caught_up = write_research_loop_rounds(reports_root, records)
    except (ReportConflict, ValueError, OSError) as exc:
        return fail(EXIT_FAULT, "round report catch-up failed", exc)
    emit("reports_caught_up", sum(1 for written in caught_up if written.written))
    emit("rounds_recorded_before", len(records))
    ran = 0
    for _ in range(rounds):
        if stop.signal_name is not None:
            break
        if (
            loop.halted is not None
            or loop.recovery_required is not None
            or loop.stopped is not None
        ):
            break
        try:
            new = loop.run_unattended(1)
        except Exception as exc:  # noqa: BLE001 - classified from the loop's own state
            interrupted = loop.audit.open_round is not None or isinstance(
                exc, (LoopHalted, JobInterrupted)
            )
            code = EXIT_RECOVERY_REQUIRED if interrupted else EXIT_FAULT
            fail(code, "the round could not be completed", exc)
            _summarize_loop(loop, code, "interrupted" if interrupted else "fault", ran, rounds)
            return code
        try:
            written = _write_reports(reports_root, new)
        except (ReportConflict, ValueError, OSError) as exc:
            fail(EXIT_FAULT, "the round report could not be written", exc)
            _summarize_loop(loop, EXIT_FAULT, "report_fault", ran + len(new), rounds)
            return EXIT_FAULT
        for record, path in zip(new, written, strict=True):
            print(
                f"round={record.round_index} record_hash={record.record_hash} "
                f"status={record.status.value} report={path}",
                flush=True,
            )
        ran += len(new)
        if not new:
            break  # nothing ran although the loop could: classified below
    code, outcome = _final_state(loop)
    if code == EXIT_OK and ran < rounds:
        if stop.signal_name is not None:
            outcome = f"stopped_by_{stop.signal_name}"
        else:
            code, outcome = EXIT_FAULT, "no_round_ran"
    _summarize_loop(loop, code, outcome, ran, rounds)
    return code


def preparation_failure(exc: Exception) -> int:
    """Report a refusal or fault raised before any state was opened and return its exit code
    (ADR-0074 §8); shared with the Dataset-sourced operator (ADR-0105 §5)."""
    if isinstance(exc, (OperatorConfigError, OperatorProviderError, OperatorRefused)):
        return fail(EXIT_REFUSED, "refused", exc)
    if isinstance(exc, FreezeRegistryFault):
        return fail(EXIT_FAULT, "freeze registry fault", exc)
    if isinstance(exc, StateFault):
        return fail(EXIT_FAULT, "state_dir cannot be verified", exc)
    if isinstance(exc, (ValueError, TypeError)):  # a compiled loop config / LoopWiring refusal
        return fail(EXIT_REFUSED, "refused", exc)
    if isinstance(exc, RecoveryRequired):
        return fail(EXIT_RECOVERY_REQUIRED, "state requires human review", exc)
    return fail(EXIT_FAULT, "preparation failed", exc)  # nothing was opened; never a success


def run_durable(
    open_loop: Callable[[], DurableLoop],
    *,
    loop_id: str,
    operator_identity: str,
    paths: OperatorPaths,
    existing: bool,
    rounds: int,
    stop: StopRequest,
) -> int:
    """Open the prepared loop with ``open_loop``, run at most ``rounds`` rounds with their reports
    and always close it (module docs, **Run**); shared with the Dataset-sourced operator."""
    emit("loop_id", loop_id)
    emit("operator_identity", operator_identity)
    emit("state_dir", paths.state_dir)
    emit("state", "existing" if existing else "new")
    emit("reports_root", paths.reports_root)
    try:
        durable = open_loop()
    except (LoopStateLocked, BusLocked) as exc:
        return fail(EXIT_FAULT, "state or bus is locked by another writer", exc)
    except Exception as exc:  # noqa: BLE001 - classified from the audit, read only
        if needs_review(paths.state_dir):
            fail(EXIT_RECOVERY_REQUIRED, "state_dir requires human review", exc)
            emit("outcome", "recovery_required")
            emit(
                "action",
                "human review required: do not rerun this state_dir; use a new loop_id and "
                "state_dir",
            )
            emit("exit_code", EXIT_RECOVERY_REQUIRED)
            return EXIT_RECOVERY_REQUIRED
        return fail(EXIT_FAULT, "state_dir could not be opened", exc)
    try:
        code = run_rounds(durable, paths.reports_root, rounds, stop)
    except Exception as exc:  # noqa: BLE001 - never exit with a success or a traceback code
        code = fail(EXIT_FAULT, "the operator failed", exc)
    finally:
        try:
            durable.close()
        except Exception as exc:  # noqa: BLE001 - a failed release is a lock / I/O fault
            fail(EXIT_FAULT, "closing the loop failed", exc)
            code = EXIT_FAULT
    emit("exit_code", code)
    return code


def _run(config: Path, rounds: int, stop: StopRequest) -> int:
    try:
        prepared = _prepare(config)
    except Exception as exc:  # noqa: BLE001 - classified; nothing was opened
        return preparation_failure(exc)
    compiled = prepared.compiled
    paths = compiled.paths
    return run_durable(
        lambda: open_synthetic_loop(
            compiled.loop_config,
            state_dir=paths.state_dir,
            provider=compiled.market_provider,
            anchor=paths.state_anchor,
            bus_anchor=paths.bus_anchor,
            operator_identity=compiled.operator_identity,
        ),
        loop_id=compiled.loop_config.loop_id,
        operator_identity=compiled.operator_identity,
        paths=paths,
        existing=prepared.existing,
        rounds=rounds,
        stop=stop,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line (argparse exits 2 on a usage error) and run one bounded batch."""
    args = _parser().parse_args(None if argv is None else list(argv))
    with boundary_stop() as stop:
        return _run(args.config, args.rounds, stop)


if __name__ == "__main__":
    raise SystemExit(main())
