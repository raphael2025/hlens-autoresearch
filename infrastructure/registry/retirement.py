"""File-backed, append-only Retirement Registry (ADR-0086 决策 2; 2026-09-28).

**Why.** ``core.domain.research.RetirementRecord`` is a Contract with no writer and no storage
(ADR-0086 背景 §2): ``research.evolution.operators.retire`` only *constructs* the record object,
it never persists it. RETIRED is a Control Plane lifecycle fact about an already-promoted (ACTIVE)
strategy and, like every terminal lifecycle transition here, must be append-only, replayable and
tamper-evident — the same durability the Failure Registry gives REJECTED / FAILED (H6: never
delete lifecycle history) — but **RETIRED ≠ FAILED**
(``core.domain.research.RetirementRecord`` 文档字符串 / 07-validation.md §4.2), so it is a
separate store, not a new case in the Failure Registry.

**Placement and the dependency-direction note.** ADR-0086 决策 2 的原文写「基于
``research/persistence/journal.py`` 的哈希链只追加日志」。那个模块在 ``research/`` 下，而
``tests/test_architecture_boundaries.py::test_plugins_and_infrastructure_do_not_import_research``
断言 ``infrastructure/`` 不得 import ``research/``（H5 / ADR-0005 §1：research 代码不是生产代码；
依赖方向 ``apps -> application -> domain <- plugins / infrastructure``，01-system.md §3）。
``infrastructure/event_bus/journal.py`` 的模块文档已经记录了同一个磁盘契约的三份**独立**实现
（``research.persistence``、``infrastructure.event_bus.journal``、``apps.worker.journal``），
专门是为了不让 ``infrastructure/`` 或 ``apps/`` 反向依赖 ``research/``。本模块因此复用
``infrastructure.event_bus.journal.AppendOnlyJournal``——``infrastructure.registry.registry.
StrategyRegistry`` 与 ``infrastructure.registry.profile_freeze.ProfileFreezeRegistry`` 的同一个
登记先例——而不是新增第四份实现，也不违反依赖方向。ADR 条款关心的是「哈希链只追加」这个存储
形状，不是某一个具体模块路径；这里落地时替换成了架构边界允许的同形状实现。

**No global singleton.** ``research.evolution.operators.retire`` keeps building a plain
``RetirementRecord`` and takes no storage argument. The caller who decides an object is retired
(the research loop / the Control Plane path that calls ``retire``) constructs a
``RetirementRegistry`` explicitly and calls ``register_retirement`` — nothing here is imported or
opened implicitly (ADR-0086 决策 2: "不引入全局单例，由调用方显式传入存储")。

**On disk** (``root``): ``retirements.jsonl`` — the hash-chained journal (shared on-disk contract
of ``infrastructure.event_bus.journal``); ``.lock`` — the single-writer ``fcntl.flock`` lock
(``RegistryLocked``; POSIX only, like every other file-backed registry here). ``anchor``
(optional, a path outside ``root``, following ``StrategyRegistry``'s optional-anchor shape):
appends a hash-chained ``(length, head)`` record after every write and, on open, verifies the
registry still has at least the anchored length and that the entry at that length carries the
anchored hash — this is what catches whole trailing lines being dropped, which the journal's own
chain alone cannot (``infrastructure.event_bus.journal`` module docs). Without an anchor the
registry still replays and verifies its own hash chain (tamper detection, crash recovery) on every
open; it just cannot additionally prove no trailing line was ever dropped.

**The one record type** (``retirement.recorded``; payload has exactly these keys):

- ``format_version``: ``"1.0.0"``;
- ``record``: ``RetirementRecord.model_dump(mode="json")`` (the exact contract from
  ``core.domain.research``, built by ``research.evolution.operators.retire`` or equivalently);
- ``record_id``: ``record.content_hash()``.

**Rules** (the same on append and on replay; a refusal before any write is ``RegistryRefused`` /
``DuplicateRecord``, nothing written; a broken record found on replay is ``RegistryCorrupted`` —
the registry cannot be opened at all): ``format_version`` must be the current one; the payload must
have exactly the three keys above; ``record`` must validate as a ``RetirementRecord`` and its
canonical re-dump must equal the stored JSON byte-for-byte (no ``model_construct`` shortcut);
``record_id`` must equal the freshly recomputed ``record.content_hash()``.

**Same-object retirement is rejected exactly once** (ADR-0086 决策 2: "同一对象重复退役时拒绝").
A ``RetirementRecord`` carries no identity of its own beyond its content hash, and a second,
differently-worded retirement of the same subject (a different ``retirement_reason`` string, a
different ``recorded_at``) must still be refused — the object was already retired once, full stop.
"Same object" is therefore judged the way this repository always judges it —
``Ref.target_identity()`` (``core/domain/base.py``: "跨对象判断'是否是同一目标'... 必须用它，而不
是 Pydantic 全结构相等"), i.e. ``subject_ref``'s ``(kind, name, version)`` — not the record's full
content hash, which would let a reworded second retirement of the same subject through.

Fail closed, same as ``StrategyRegistry`` / ``ProfileFreezeRegistry``: tampering, a broken hash
chain, a partial trailing line (a crash mid-write) or a shrunken file are ``RegistryCorrupted`` via
the journal; a write that fails after it has begun (the journal line is written but the anchor
append then fails, for instance) **poisons** the instance — every further read or write raises
``RegistryCorrupted`` until a fresh ``RetirementRegistry`` replays and verifies the disk, mirroring
``ProfileFreezeRegistry``'s crash-window handling (the one legitimate window — record fsync'd, its
anchor line not yet — is re-anchored automatically on the next open). There is no edit, delete or
un-retire operation: RETIRED is terminal (07-validation.md §4.2) and H6 forbids deleting lifecycle
history.

**Honest boundary.** This is a file-backed stand-in for Control Plane storage, exactly like
``StrategyRegistry`` (ADR-0005 §7: PostgreSQL is not available to this batch, H12). It stores
exactly the ``RetirementRecord`` a caller gives it; it does not itself decide that a subject
*should* be retired, does not check the subject was ever ACTIVE, and does not evaluate the
retirement's evidence — those are ``research.evolution.operators.retire`` and the lifecycle rules
elsewhere. ``retirement_reason`` / ``lessons`` / ``recorded_at`` are declared by the caller and are
not independently verified, the same honest boundary ``ProfileFreezeRegistry`` documents for
``approved_by``.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from pydantic import ValidationError

from core.domain.base import Ref, RefTargetIdentity, canonical_json
from core.domain.research import RetirementRecord
from infrastructure.event_bus.journal import AppendOnlyJournal, JournalCorrupted
from infrastructure.registry.registry import (
    DuplicateRecord,
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
)

__all__ = [
    "FORMAT_VERSION",
    "RETIREMENT_RECORDED",
    "RetirementRegistry",
    "verify_integrity",
    "verify_integrity_snapshot",
]

RETIREMENT_RECORDED: Final = "retirement.recorded"
FORMAT_VERSION: Final = "1.0.0"
_ANCHOR_TYPE: Final = "retirement_registry.head"
_KEYS: Final = frozenset({"format_version", "record", "record_id"})


def _payload_of(record: RetirementRecord) -> Any:
    return record.model_dump(mode="json")


def _record_of(raw: object) -> RetirementRecord:
    try:
        record = RetirementRecord.model_validate_json(canonical_json(raw))
    except (ValidationError, TypeError, ValueError) as exc:
        raise RegistryRefused(f"the recorded retirement does not validate: {exc}") from exc
    if record.model_dump(mode="json") != raw:
        raise RegistryRefused("the recorded retirement is not its own canonical JSON")
    return record


class RetirementRegistry:
    """The append-only Retirement Registry (see module docs). Use as a context manager or
    ``close()`` it to release the single-writer lock. ``anchor`` is optional (see module docs)."""

    def __init__(self, root: Path, *, anchor: Path | None = None) -> None:
        self._root = Path(root)
        if anchor is not None and Path(anchor).resolve().is_relative_to(self._root.resolve()):
            raise ValueError(
                "the retirement registry anchor must live outside the registry directory"
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
            self._by_subject: dict[RefTargetIdentity, RetirementRecord] = {}
            self._order: list[RefTargetIdentity] = []
            try:
                self._journal = AppendOnlyJournal(self._root / "retirements.jsonl")
            except JournalCorrupted as exc:
                raise RegistryCorrupted(f"retirement journal: {exc}") from exc
            for entry in self._journal.entries:
                try:
                    self._apply(entry.type, entry.payload)()
                except (RegistryRefused, ValidationError, ValueError) as exc:
                    raise RegistryCorrupted(f"retirement record {entry.seq}: {exc}") from exc
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
                f"this retirement registry instance is poisoned ({self._poisoned}); open a new "
                "one to replay and verify the disk"
            )
        if self._lock_fd is None:
            raise RegistryError("the retirement registry is closed")

    def _poison(self, reason: str) -> None:
        self._poisoned = reason
        self.close()

    # ---- anchor (optional) ----------------------------------------------------------------

    def _open_anchor(self, path: Path) -> None:
        try:
            anchor = AppendOnlyJournal(path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"retirement registry anchor: {exc}") from exc
        length, head = 0, ""
        for entry in anchor.entries:
            payload = entry.payload
            if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
                raise RegistryCorrupted(
                    f"retirement anchor record {entry.seq} is not a head record"
                )
            new_length, new_head = payload["length"], payload["head"]
            if not isinstance(new_length, int) or isinstance(new_length, bool) or new_length < 1:
                raise RegistryCorrupted(f"retirement anchor record {entry.seq} has a bad length")
            if new_length <= length or not isinstance(new_head, str):
                raise RegistryCorrupted(f"retirement anchor record {entry.seq} goes backwards")
            length, head = new_length, new_head
        have = len(self._journal)
        if length:
            if have < length:
                raise RegistryCorrupted(
                    f"the retirement registry was anchored at {length} records and now has "
                    f"{have}: records were removed"
                )
            if self._journal.entry(length - 1).hash != head:
                raise RegistryCorrupted("the retirement registry is not the history its anchor saw")
        if have > length + 1:
            raise RegistryCorrupted(
                f"the retirement registry has {have} records but its anchor saw {length}: "
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
            raise RegistryCorrupted(f"retirement registry anchor: {exc}") from exc

    # ---- rules (shared by append and replay) ----------------------------------------------

    def _apply(self, type_: str, payload: Mapping[str, Any]) -> Callable[[], None]:
        """Check one record against every rule; returns the commit that admits it in memory.

        Nothing is mutated before the commit runs, so a refused record (or a failed disk write
        after the checks) leaves the in-memory view untouched.
        """
        if type_ != RETIREMENT_RECORDED:
            raise RegistryRefused(f"unknown record type {type_!r}")
        if set(payload) != _KEYS:
            raise RegistryRefused(
                f"a {RETIREMENT_RECORDED} record has exactly the keys {sorted(_KEYS)}"
            )
        if payload["format_version"] != FORMAT_VERSION:
            raise RegistryRefused(
                f"format_version {payload['format_version']!r} is not {FORMAT_VERSION!r}"
            )
        record = _record_of(payload["record"])
        record_id = payload["record_id"]
        if record_id != record.content_hash():
            raise RegistryRefused("the recorded record_id is not the record's content hash")
        subject = record.subject_ref.target_identity()
        if subject in self._by_subject:
            raise DuplicateRecord(f"{record.subject_ref} is already retired")

        def commit() -> None:
            self._by_subject[subject] = record
            self._order.append(subject)

        return commit

    # ---- writes -------------------------------------------------------------------------

    def register_retirement(self, record: RetirementRecord) -> RetirementRecord:
        """Append one retirement record; refuses a second retirement of the same subject.

        ``record`` is typically what ``research.evolution.operators.retire`` returned — this
        method is the only thing that persists it (ADR-0086 决策 2: no global singleton, the
        caller passes the storage and the record explicitly).
        """
        self._require_open()
        if not isinstance(record, RetirementRecord):
            raise RegistryRefused("register_retirement needs a RetirementRecord")
        try:  # no model_construct shortcut: re-validate from the record's own canonical JSON
            record = RetirementRecord.model_validate_json(record.model_dump_json())
        except ValidationError as exc:
            raise RegistryRefused(f"the retirement record does not validate: {exc}") from exc
        payload: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "record": _payload_of(record),
            "record_id": record.content_hash(),
        }
        # every rule — duplicate subject included — before anything is written
        commit = self._apply(RETIREMENT_RECORDED, payload)
        try:
            self._journal.append(RETIREMENT_RECORDED, payload)
            self._write_anchor()
        except BaseException as exc:
            # the disk may have changed: never trust this instance's memory again
            self._poison(f"register_retirement failed after writing began: {type(exc).__name__}")
            if isinstance(exc, RegistryError):
                raise
            raise RegistryCorrupted(
                f"register_retirement failed after writing began ({type(exc).__name__}: {exc}); "
                "this instance is closed — open a new one to replay and verify the disk"
            ) from exc
        commit()  # admitted in memory only once the record (and its anchor, if any) is durable
        return self._by_subject[record.subject_ref.target_identity()]

    # ---- reads --------------------------------------------------------------------------

    def __len__(self) -> int:
        self._require_open()
        return len(self._journal)

    @property
    def head_hash(self) -> str:
        self._require_open()
        return self._journal.head_hash

    @property
    def retirements(self) -> tuple[RetirementRecord, ...]:
        """Every verified record, in journal (append) order."""
        self._require_open()
        return tuple(self._by_subject[subject] for subject in self._order)

    def is_retired(self, ref: Ref) -> bool:
        """Whether ``ref``'s subject (``target_identity()``) has a retirement record."""
        self._require_open()
        return ref.target_identity() in self._by_subject

    def retirement_of(self, ref: Ref) -> RetirementRecord | None:
        """The record retiring exactly this subject (same ``target_identity()``), or ``None``."""
        self._require_open()
        return self._by_subject.get(ref.target_identity())


def verify_integrity(root: Path, *, anchor: Path | None = None) -> int:
    """Replay and verify every retirement record (and, if given, the anchor); return the count.

    This is the read-only integrity check ADR-0086 决策 2 asks for ("提供读取与完整性校验"):
    opening a ``RetirementRegistry`` already replays and verifies its whole hash chain (and its
    anchor, if any) before any read is allowed, so this is a thin, explicit entry point for a
    caller that only wants to audit — it takes and releases the single-writer lock and appends no
    record and no anchor line. (Like any open, it may create ``root`` and its ``.lock`` file if
    they do not exist yet — the same as opening any empty registry.) Raises ``RegistryCorrupted``
    if the chain, any record, or the anchor is broken; raises ``RegistryLocked`` if another
    instance already holds the registry open.
    """
    with RetirementRegistry(root, anchor=anchor) as registry:
        return len(registry)


def verify_integrity_snapshot(root: Path, *, anchor: Path | None = None) -> dict[str, object]:
    """Verify a stable Retirement Registry snapshot without locks or anchor recovery."""
    root = Path(root)
    journal_path = root / "retirements.jsonl"
    if not root.is_dir() or not journal_path.is_file():
        raise RegistryCorrupted(f"the Retirement Registry does not exist at {root}")
    resolved_root = root.resolve()
    if anchor is not None:
        anchor = Path(anchor)
        if anchor.resolve().is_relative_to(resolved_root):
            raise RegistryCorrupted("the retirement registry anchor must be outside its directory")

    def signature(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        stamp = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if stamp != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise RegistryCorrupted(f"{path} changed while the audit snapshot was read")
        return data, stamp

    journal_bytes, journal_stamp = signature(journal_path)
    try:
        journal = AppendOnlyJournal(journal_path)
    except JournalCorrupted as exc:
        raise RegistryCorrupted(f"retirement registry journal: {exc}") from exc
    replay = object.__new__(RetirementRegistry)
    replay._by_subject = {}
    replay._order = []
    for entry in journal.entries:
        try:
            replay._apply(entry.type, entry.payload)()
        except (RegistryRefused, ValidationError, ValueError) as exc:
            raise RegistryCorrupted(f"retirement record {entry.seq}: {exc}") from exc

    anchor_status = "UNANCHORED"
    anchor_path: Path | None = None
    anchor_bytes: bytes | None = None
    anchor_stamp: tuple[int, int, int, int] | None = None
    if anchor is not None:
        anchor_path = Path(anchor)
        if not anchor_path.is_file():
            raise RegistryCorrupted(f"the Retirement Registry anchor does not exist: {anchor_path}")
        anchor_bytes, anchor_stamp = signature(anchor_path)
        try:
            anchor_journal = AppendOnlyJournal(anchor_path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"retirement registry anchor: {exc}") from exc
        length, head = 0, ""
        for entry in anchor_journal.entries:
            payload = entry.payload
            if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
                raise RegistryCorrupted(
                    f"retirement anchor record {entry.seq} is not a head record"
                )
            new_length, new_head = payload["length"], payload["head"]
            if (
                not isinstance(new_length, int)
                or isinstance(new_length, bool)
                or new_length <= length
                or not isinstance(new_head, str)
            ):
                raise RegistryCorrupted(f"retirement anchor record {entry.seq} is invalid")
            if new_length > len(journal) or journal.entry(new_length - 1).hash != new_head:
                raise RegistryCorrupted(
                    f"retirement anchor record {entry.seq} does not match its journal prefix"
                )
            length, head = new_length, new_head
        if length != len(journal) or (length and journal.entry(length - 1).hash != head):
            raise RegistryCorrupted("the Retirement Registry anchor does not match the journal tip")
        if not length and head:
            raise RegistryCorrupted("the empty Retirement Registry anchor has a non-empty head")
        anchor_status = "VERIFIED"

    if signature(journal_path) != (journal_bytes, journal_stamp):
        raise RegistryCorrupted(f"{journal_path} changed during the audit")
    if anchor_path is not None and signature(anchor_path) != (anchor_bytes, anchor_stamp):
        raise RegistryCorrupted(f"{anchor_path} changed during the audit")
    return {
        "status": "OK",
        "evidence": "HASH_CHAIN",
        "path": str(resolved_root),
        "journal_records": len(journal),
        "journal_head_hash": journal.head_hash,
        "anchor_status": anchor_status,
        "anchor_path": None if anchor_path is None else str(anchor_path.resolve()),
        "limitations": [
            "rollback of the journal and its external anchor together is not detectable"
        ]
        if anchor_status == "VERIFIED"
        else ["without an external anchor, whole trailing journal records cannot be detected"],
    }
