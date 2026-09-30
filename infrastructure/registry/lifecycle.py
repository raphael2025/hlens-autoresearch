"""File-backed, append-only Lifecycle Registry: the lifecycle authority of ADR-0098 §1 (2026-09-30).

**Why.** ``core.lifecycle.strategy.LifecycleHistory`` is an immutable value object a caller passes
around; nothing persisted the transitions, so "is this strategy ACTIVE *now*" and "which strategies
are ACTIVE" had no readable authority (ADR-0080, superseded by ADR-0098). This registry is that
authority: every ``LifecycleTransition`` is appended to one hash-chained journal, and the current
state of a strategy, the ACTIVE set and the history a degradation check cites are **replays** of
that journal at an explicit head.

**On disk** (``root``): ``lifecycle.jsonl`` — the hash-chained journal (the shared on-disk contract
of ``infrastructure.event_bus.journal.AppendOnlyJournal``, reused like ``StrategyRegistry`` /
``RetirementRegistry``); ``.lock`` — the single-writer ``fcntl.flock`` lock (``RegistryLocked``;
POSIX only). ``anchor`` (optional, outside ``root``): a hash-chained ``(length, head)`` journal
appended after every write and verified on open, exactly as ``RetirementRegistry`` does — the only
way to detect whole trailing records being dropped.

**The one record type** (``lifecycle.transition_recorded``; payload has exactly these keys):

- ``format_version``: ``"1.0.0"``;
- ``record_index``: the 1-based record number (equal to the journal ``seq``);
- ``prev_record_hash``: the previous record's hash (``GENESIS_HASH`` for the first; equal to the
  journal ``prev_hash``);
- ``strategy``: ``str(transition.subject)`` — the strategy target ``Ref``;
- ``transition``: ``LifecycleTransition.model_dump(mode="json")``;
- ``transition_hash``: ``transition.content_hash()``.

A record's hash is its journal entry hash (it covers ``seq``, the payload and ``prev_hash``).

**Rules** (the same on append and on replay; before any write a refusal is ``RegistryRefused``
and nothing is written; on open a broken record is ``RegistryCorrupted`` and the registry cannot be
used): the keys, version and identities above; ``transition`` validates as a ``LifecycleTransition``
and is its own canonical JSON; the transition's ``from_state`` is the state the strategy (same
``Ref.target_identity()``) replays to at that point (``IDEA`` when it has no record yet), the move
passes ``core.lifecycle.strategy.validate_transition`` (legal edge, human approval where required),
and the strategy's whole history re-validates as a ``LifecycleHistory`` (subject, chain and
non-decreasing ``occurred_at``) — the core contract's own rules, nothing added.

**Optimistic concurrency.** ``append`` requires ``expected_head``: the head the caller read before
deciding the transition. If the registry's head is not exactly that ``LifecycleHead``, nothing is
written (``HeadMismatch``) — a concurrent write, or a caller working from a stale / rolled-back
view, is detected instead of silently interleaved.

**Head identity and reads.** A head is ``LifecycleHead(record_count, last_record_hash)``; the empty
registry's head is ``(0, GENESIS_HASH)``. Every read takes an explicit head — ``head`` (the latest)
or a head the caller pinned earlier — and a pinned head that is not on this chain (count beyond the
journal, or the hash at that count differs) is ``UnknownHead``. A strategy's lifecycle at a head is
the replay of its records up to that head; the ACTIVE set at a head is every strategy whose replay
ends in ``ACTIVE``, so ``ACTIVE → DEGRADED / RETIRED`` removes it naturally.

**Writers.** Only explicit calls (a promotion / lifecycle CLI or function the operator invokes).
The research loop, the ADR-0074 synthetic operator and the API never open this registry for writing
(ADR-0098 §1); there is no global singleton.

**Honest boundary.** A file-backed stand-in for Control Plane storage (like ``StrategyRegistry``,
ADR-0005 §7). It proves the stored transitions are a legal, append-only, hash-chained lifecycle; it
does not authenticate ``triggered_by`` / ``approved_by`` (declared names) and does not check that the
evidence references exist. Backfilling existing strategies means appending their **real** history
record by record (ADR-0098 后果); nothing here fabricates one. Rolling the anchor back together with
the registry is still undetectable.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from pydantic import ValidationError

from core.domain.base import Ref, RefTargetIdentity, canonical_json
from core.errors import LifecycleViolation
from core.lifecycle.strategy import (
    LifecycleHistory,
    LifecycleState,
    LifecycleTransition,
    validate_transition,
)
from infrastructure.event_bus.journal import GENESIS_HASH, AppendOnlyJournal, JournalCorrupted
from infrastructure.registry.registry import (
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
)

__all__ = [
    "FORMAT_VERSION",
    "LIFECYCLE_TRANSITION_RECORDED",
    "ActiveSet",
    "HeadMismatch",
    "LifecycleHead",
    "LifecycleRecord",
    "LifecycleRegistry",
    "StrategyLifecycle",
    "UnknownHead",
    "verify_integrity",
    "verify_integrity_snapshot",
]

LIFECYCLE_TRANSITION_RECORDED: Final = "lifecycle.transition_recorded"
FORMAT_VERSION: Final = "1.0.0"
JOURNAL_NAME: Final = "lifecycle.jsonl"
_ANCHOR_TYPE: Final = "lifecycle_registry.head"
_KEYS: Final = frozenset(
    {
        "format_version",
        "record_index",
        "prev_record_hash",
        "strategy",
        "transition",
        "transition_hash",
    }
)
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class HeadMismatch(RegistryRefused):
    """``append``'s ``expected_head`` is not the registry's current head; nothing was written."""


class UnknownHead(RegistryRefused, LookupError):
    """A pinned head is not on this registry's chain."""


# ---- value objects ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LifecycleHead:
    """A head identity: how many records, and the hash of the last one (``GENESIS_HASH`` when
    there are none)."""

    record_count: int
    last_record_hash: str

    def __post_init__(self) -> None:
        count = self.record_count
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("record_count must be a non-negative int")
        if not isinstance(self.last_record_hash, str) or not _SHA256.fullmatch(
            self.last_record_hash
        ):
            raise ValueError("last_record_hash must be a lowercase SHA-256 hex string")
        if (count == 0) != (self.last_record_hash == GENESIS_HASH):
            raise ValueError("only the empty head (record_count 0) has the genesis hash")

    def payload(self) -> dict[str, Any]:
        return {"record_count": self.record_count, "last_record_hash": self.last_record_hash}


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """One verified record: its position, hash, predecessor, strategy and transition."""

    record_index: int
    record_hash: str
    prev_record_hash: str
    strategy: Ref
    transition: LifecycleTransition


@dataclass(frozen=True, slots=True)
class StrategyLifecycle:
    """A strategy's replayed lifecycle at ``head``: the ``LifecycleHistory`` of its records up to
    that head, in record order, and the hashes of those records."""

    head: LifecycleHead
    history: LifecycleHistory
    record_hashes: tuple[str, ...]

    @property
    def current_state(self) -> LifecycleState:
        return self.history.current_state

    @property
    def history_hash(self) -> str:
        return self.history.content_hash()


@dataclass(frozen=True, slots=True)
class ActiveSet:
    """Every strategy whose replay ends in ``ACTIVE`` at ``head``, sorted by ``str(ref)``."""

    head: LifecycleHead
    strategies: tuple[Ref, ...]

    def contains(self, ref: Ref) -> bool:
        target = ref.target_identity()
        return any(item.target_identity() == target for item in self.strategies)


# ---- rules shared by append, replay and the snapshot audit --------------------------------


def _transition_of(raw: object) -> LifecycleTransition:
    try:
        transition = LifecycleTransition.model_validate_json(canonical_json(raw))
    except (ValidationError, TypeError, ValueError) as exc:
        raise RegistryRefused(f"the recorded transition does not validate: {exc}") from exc
    if transition.model_dump(mode="json") != raw:
        raise RegistryRefused("the recorded transition is not its own canonical JSON")
    return transition


class _Replay:
    """The in-memory replay state (records in order + each strategy's latest history).

    Used by the registry instance and by the lock-free snapshot audit, so the rules are one code
    path.
    """

    def __init__(self) -> None:
        self.records: list[LifecycleRecord] = []
        self.histories: dict[RefTargetIdentity, LifecycleHistory] = {}

    def check(
        self, type_: str, payload: Mapping[str, Any], *, seq: int, prev_hash: str
    ) -> Callable[[str], None]:
        """Check one record at position ``seq`` after ``prev_hash``; returns the commit that
        admits it (given its record hash). Nothing is mutated before the commit runs."""
        if type_ != LIFECYCLE_TRANSITION_RECORDED:
            raise RegistryRefused(f"unknown record type {type_!r}")
        if set(payload) != _KEYS:
            raise RegistryRefused(
                f"a {LIFECYCLE_TRANSITION_RECORDED} record has exactly the keys {sorted(_KEYS)}"
            )
        if payload["format_version"] != FORMAT_VERSION:
            raise RegistryRefused(
                f"format_version {payload['format_version']!r} is not {FORMAT_VERSION!r}"
            )
        index = payload["record_index"]
        if not isinstance(index, int) or isinstance(index, bool) or index != seq:
            raise RegistryRefused(f"record_index {index!r} is not the record's position {seq}")
        if payload["prev_record_hash"] != prev_hash:
            raise RegistryRefused("prev_record_hash is not the previous record's hash")
        transition = _transition_of(payload["transition"])
        if payload["transition_hash"] != transition.content_hash():
            raise RegistryRefused("transition_hash is not the transition's content hash")
        strategy_text = payload["strategy"]
        if not isinstance(strategy_text, str) or strategy_text != str(transition.subject):
            raise RegistryRefused("strategy is not the transition's subject Ref")
        try:
            strategy = Ref.parse(strategy_text)
        except ValueError as exc:
            raise RegistryRefused(f"strategy is not a Ref: {exc}") from exc
        identity = transition.subject.target_identity()
        previous = self.histories.get(identity)
        current = LifecycleState.IDEA if previous is None else previous.current_state
        if transition.from_state is not current:
            raise RegistryRefused(
                f"{strategy_text} replays to {current}, not the transition's from_state "
                f"{transition.from_state}"
            )
        try:
            validate_transition(
                transition.from_state, transition.to_state, approved_by=transition.approved_by
            )
            history = LifecycleHistory(
                subject=transition.subject if previous is None else previous.subject,
                transitions=(() if previous is None else previous.transitions) + (transition,),
            )
        except (LifecycleViolation, ValidationError, ValueError) as exc:
            raise RegistryRefused(
                f"illegal lifecycle transition for {strategy_text}: {exc}"
            ) from exc

        def commit(record_hash: str) -> None:
            self.records.append(
                LifecycleRecord(
                    record_index=seq,
                    record_hash=record_hash,
                    prev_record_hash=prev_hash,
                    strategy=strategy,
                    transition=transition,
                )
            )
            self.histories[identity] = history

        return commit

    def replay(self, journal: AppendOnlyJournal, what: str) -> None:
        for entry in journal.entries:
            try:
                self.check(entry.type, entry.payload, seq=entry.seq, prev_hash=entry.prev_hash)(
                    entry.hash
                )
            except (RegistryRefused, ValidationError, ValueError) as exc:
                raise RegistryCorrupted(f"{what} record {entry.seq}: {exc}") from exc


def _anchor_heads(anchor: AppendOnlyJournal) -> tuple[int, str]:
    """The last ``(length, head)`` of a well-formed anchor journal (``(0, "")`` when empty)."""
    length, head = 0, ""
    for entry in anchor.entries:
        payload = entry.payload
        if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
            raise RegistryCorrupted(f"lifecycle anchor record {entry.seq} is not a head record")
        new_length, new_head = payload["length"], payload["head"]
        if not isinstance(new_length, int) or isinstance(new_length, bool) or new_length < 1:
            raise RegistryCorrupted(f"lifecycle anchor record {entry.seq} has a bad length")
        if new_length <= length or not isinstance(new_head, str):
            raise RegistryCorrupted(f"lifecycle anchor record {entry.seq} goes backwards")
        length, head = new_length, new_head
    return length, head


def _head_of(records: Sequence[LifecycleRecord], count: int) -> LifecycleHead:
    return LifecycleHead(count, records[count - 1].record_hash if count else GENESIS_HASH)


def _lifecycle_at(
    records: Sequence[LifecycleRecord], head: LifecycleHead, subject: Ref
) -> StrategyLifecycle:
    target = subject.target_identity()
    mine = [r for r in records[: head.record_count] if r.strategy.target_identity() == target]
    history = LifecycleHistory(
        subject=mine[0].transition.subject if mine else subject,
        transitions=tuple(r.transition for r in mine),
    )
    return StrategyLifecycle(
        head=head, history=history, record_hashes=tuple(r.record_hash for r in mine)
    )


def _active_at(records: Sequence[LifecycleRecord], head: LifecycleHead) -> ActiveSet:
    state: dict[RefTargetIdentity, tuple[Ref, LifecycleState]] = {}
    for record in records[: head.record_count]:
        identity = record.strategy.target_identity()
        subject = state[identity][0] if identity in state else record.strategy
        state[identity] = (subject, record.transition.to_state)
    active = sorted(
        (ref for ref, current in state.values() if current is LifecycleState.ACTIVE), key=str
    )
    return ActiveSet(head=head, strategies=tuple(active))


# ---- the registry -------------------------------------------------------------------------


class LifecycleRegistry:
    """The append-only Lifecycle Registry (see module docs). Use as a context manager or
    ``close()`` it to release the single-writer lock. ``anchor`` is optional (module docs)."""

    def __init__(self, root: Path, *, anchor: Path | None = None) -> None:
        self._root = Path(root)
        if anchor is not None and Path(anchor).resolve().is_relative_to(self._root.resolve()):
            raise ValueError(
                "the lifecycle registry anchor must live outside the registry directory"
            )
        self._root.mkdir(parents=True, exist_ok=True)
        #: Why this instance may no longer be trusted (a write that failed after it began writing).
        self._poisoned: str | None = None
        self._lock_fd: int | None = os.open(self._root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RegistryLocked(f"{self._root} is open elsewhere") from exc
        try:
            self._state = _Replay()
            try:
                self._journal = AppendOnlyJournal(self._root / JOURNAL_NAME)
            except JournalCorrupted as exc:
                raise RegistryCorrupted(f"lifecycle journal: {exc}") from exc
            self._state.replay(self._journal, "lifecycle")
            self._anchor: AppendOnlyJournal | None = None
            if anchor is not None:
                self._open_anchor(Path(anchor))
        except BaseException:
            self.close()
            raise

    # ---- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._poisoned is not None:
            raise RegistryCorrupted(
                f"this lifecycle registry instance is poisoned ({self._poisoned}); open a new "
                "one to replay and verify the disk"
            )
        if self._lock_fd is None:
            raise RegistryError("the lifecycle registry is closed")

    def _poison(self, reason: str) -> None:
        self._poisoned = reason
        self.close()

    # ---- anchor (optional) --------------------------------------------------------------

    def _open_anchor(self, path: Path) -> None:
        try:
            anchor = AppendOnlyJournal(path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"lifecycle registry anchor: {exc}") from exc
        length, head = _anchor_heads(anchor)
        have = len(self._journal)
        if length:
            if have < length:
                raise RegistryCorrupted(
                    f"the lifecycle registry was anchored at {length} records and now has "
                    f"{have}: records were removed"
                )
            if self._journal.entry(length - 1).hash != head:
                raise RegistryCorrupted(
                    "the lifecycle registry is not the history its anchor saw"
                )
        if have > length + 1:
            raise RegistryCorrupted(
                f"the lifecycle registry has {have} records but its anchor saw {length}: "
                "records were written without the anchor"
            )
        self._anchor = anchor
        if have == length + 1:  # the one legitimate crash window: record fsync'd, anchor not
            self._write_anchor()

    def _write_anchor(self) -> None:
        if self._anchor is None:
            return
        try:
            self._anchor.append(
                _ANCHOR_TYPE, {"length": len(self._journal), "head": self._journal.head_hash}
            )
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"lifecycle registry anchor: {exc}") from exc

    # ---- writes -------------------------------------------------------------------------

    def append(
        self, transition: LifecycleTransition, *, expected_head: LifecycleHead
    ) -> LifecycleHead:
        """Append one transition if ``expected_head`` is the current head; returns the new head.

        Refuses (nothing written): a head mismatch (``HeadMismatch``), a transition that does
        not start from the strategy's replayed state or breaks ``validate_transition`` /
        ``LifecycleHistory`` (``RegistryRefused``).
        """
        self._require_open()
        if not isinstance(transition, LifecycleTransition):
            raise RegistryRefused("append needs a LifecycleTransition")
        if not isinstance(expected_head, LifecycleHead):
            raise RegistryRefused("append needs the expected LifecycleHead")
        try:  # no model_construct shortcut: re-validate from the transition's own JSON
            transition = LifecycleTransition.model_validate_json(transition.model_dump_json())
        except ValidationError as exc:
            raise RegistryRefused(f"the transition does not validate: {exc}") from exc
        current = self.head
        if expected_head != current:
            raise HeadMismatch(
                f"expected head {expected_head.record_count}/{expected_head.last_record_hash} "
                f"but the registry is at {current.record_count}/{current.last_record_hash}"
            )
        payload: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "record_index": current.record_count + 1,
            "prev_record_hash": current.last_record_hash,
            "strategy": str(transition.subject),
            "transition": transition.model_dump(mode="json"),
            "transition_hash": transition.content_hash(),
        }
        commit = self._state.check(
            LIFECYCLE_TRANSITION_RECORDED,
            payload,
            seq=current.record_count + 1,
            prev_hash=current.last_record_hash,
        )
        try:
            entry = self._journal.append(LIFECYCLE_TRANSITION_RECORDED, payload)
            self._write_anchor()
        except BaseException as exc:
            self._poison(f"append failed after writing began: {type(exc).__name__}")
            if isinstance(exc, RegistryError):
                raise
            raise RegistryCorrupted(
                f"append failed after writing began ({type(exc).__name__}: {exc}); this "
                "instance is closed — open a new one to replay and verify the disk"
            ) from exc
        commit(entry.hash)  # admitted in memory only once the record (and anchor) is durable
        return self.head

    # ---- reads --------------------------------------------------------------------------

    def __len__(self) -> int:
        self._require_open()
        return len(self._journal)

    @property
    def head(self) -> LifecycleHead:
        """The latest head (``(0, GENESIS_HASH)`` for an empty registry)."""
        self._require_open()
        return _head_of(self._state.records, len(self._state.records))

    def verify_head(self, head: LifecycleHead) -> LifecycleHead:
        """``head`` itself when it is on this chain, else ``UnknownHead``."""
        self._require_open()
        if not isinstance(head, LifecycleHead):
            raise UnknownHead("a head must be a LifecycleHead")
        records = self._state.records
        if head.record_count > len(records) or _head_of(records, head.record_count) != head:
            raise UnknownHead(
                f"head {head.record_count}/{head.last_record_hash} is not on this registry's "
                "chain"
            )
        return head

    def head_for_hash(self, record_hash: str) -> LifecycleHead:
        """The head whose last record has ``record_hash`` (``GENESIS_HASH``: the empty head);
        ``UnknownHead`` when no record of this chain has it."""
        self._require_open()
        if not isinstance(record_hash, str) or not _SHA256.fullmatch(record_hash):
            raise UnknownHead("a head hash is a lowercase SHA-256 hex string")
        if record_hash == GENESIS_HASH:
            return LifecycleHead(0, GENESIS_HASH)
        for record in self._state.records:
            if record.record_hash == record_hash:
                return LifecycleHead(record.record_index, record_hash)
        raise UnknownHead(f"no record of this registry has the hash {record_hash}")

    def records(self, head: LifecycleHead) -> tuple[LifecycleRecord, ...]:
        """Every verified record up to ``head``, in record order."""
        head = self.verify_head(head)
        return tuple(self._state.records[: head.record_count])

    def lifecycle_of(self, subject: Ref, head: LifecycleHead) -> StrategyLifecycle:
        """``subject``'s replayed lifecycle at ``head`` (an empty history — ``IDEA`` — when it
        has no record up to that head)."""
        if not isinstance(subject, Ref):
            raise RegistryRefused("lifecycle_of needs a Ref")
        head = self.verify_head(head)
        return _lifecycle_at(self._state.records, head, subject)

    def active_set(self, head: LifecycleHead) -> ActiveSet:
        """Every strategy whose replay ends in ``ACTIVE`` at ``head``."""
        head = self.verify_head(head)
        return _active_at(self._state.records, head)


# ---- read-only integrity checks -----------------------------------------------------------


def verify_integrity(root: Path, *, anchor: Path | None = None) -> int:
    """Replay and verify every record (and, if given, the anchor); return the record count.

    Opening a ``LifecycleRegistry`` already replays and verifies the whole chain before any read;
    this is the explicit audit entry point. It takes and releases the single-writer lock and
    appends no record (like any open, it may re-anchor the one legitimate crash window and may
    create ``root`` / ``.lock`` when they do not exist). ``verify_integrity_snapshot`` is the
    strictly read-only variant.
    """
    with LifecycleRegistry(root, anchor=anchor) as registry:
        return len(registry)


def verify_integrity_snapshot(root: Path, *, anchor: Path | None = None) -> dict[str, object]:
    """Verify a stable Lifecycle Registry snapshot without locks, writes or anchor recovery.

    A supplied anchor must match the journal tip exactly; without one the result is marked
    ``UNANCHORED`` (whole trailing records cannot be detected). Raises ``RegistryCorrupted``.
    """
    root = Path(root)
    journal_path = root / JOURNAL_NAME
    if not root.is_dir() or not journal_path.is_file():
        raise RegistryCorrupted(f"the Lifecycle Registry does not exist at {root}")
    resolved_root = root.resolve()
    anchor_path = None if anchor is None else Path(anchor)
    if anchor_path is not None and anchor_path.resolve().is_relative_to(resolved_root):
        raise RegistryCorrupted("the lifecycle registry anchor must be outside its directory")

    def signature(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        stamp = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if stamp != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise RegistryCorrupted(f"{path} changed while the audit snapshot was read")
        return data, stamp

    journal_signature = signature(journal_path)
    try:
        journal = AppendOnlyJournal(journal_path)
    except JournalCorrupted as exc:
        raise RegistryCorrupted(f"lifecycle registry journal: {exc}") from exc
    replay = _Replay()
    replay.replay(journal, "lifecycle")

    anchor_status = "UNANCHORED"
    anchor_signature: tuple[bytes, tuple[int, int, int, int]] | None = None
    if anchor_path is not None:
        if not anchor_path.is_file():
            raise RegistryCorrupted(f"the Lifecycle Registry anchor does not exist: {anchor_path}")
        anchor_signature = signature(anchor_path)
        try:
            anchor_journal = AppendOnlyJournal(anchor_path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"lifecycle registry anchor: {exc}") from exc
        length, head = _anchor_heads(anchor_journal)  # shape and monotonic lengths
        for entry in anchor_journal.entries:  # every anchored prefix must match the journal
            prefix, prefix_head = entry.payload["length"], entry.payload["head"]
            if prefix > len(journal) or journal.entry(prefix - 1).hash != prefix_head:
                raise RegistryCorrupted(
                    f"lifecycle anchor record {entry.seq} does not match its journal prefix"
                )
        if length != len(journal) or (length and journal.entry(length - 1).hash != head):
            raise RegistryCorrupted("the Lifecycle Registry anchor does not match the journal tip")
        anchor_status = "VERIFIED"

    if signature(journal_path) != journal_signature:
        raise RegistryCorrupted(f"{journal_path} changed during the audit")
    if anchor_path is not None and signature(anchor_path) != anchor_signature:
        raise RegistryCorrupted(f"{anchor_path} changed during the audit")
    head = _head_of(replay.records, len(replay.records))
    return {
        "status": "OK",
        "evidence": "HASH_CHAIN",
        "path": str(resolved_root),
        "journal_records": len(journal),
        "journal_head_hash": journal.head_hash,
        "head": head.payload(),
        "active_strategies": [str(ref) for ref in _active_at(replay.records, head).strategies],
        "anchor_status": anchor_status,
        "anchor_path": None if anchor_path is None else str(anchor_path.resolve()),
        "limitations": [
            "rollback of the journal and its external anchor together is not detectable"
        ]
        if anchor_status == "VERIFIED"
        else ["without an external anchor, whole trailing journal records cannot be detected"],
    }

