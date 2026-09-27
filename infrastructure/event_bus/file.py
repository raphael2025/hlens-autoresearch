"""Durable, file-backed, at-least-once event bus (ADR-0044 implementation note, file-backed bus,
2026-09-26; backlog P11 "总线只在内存中").

The interim durable option until an external bus exists: NATS is not introduced before Phase 11+
(ADR-0021 / D-10), and installing it is an environment change that needs Raphael (H12). This adapter
needs no software beyond the standard library and passes the same provider-agnostic contract suite
as ``InMemoryEventBus`` (``tests/contract_suites/event_bus.py``).

Semantics — **identical to** ``InMemoryEventBus`` (a differential test runs the same operation
sequences against both):

- ``publish`` appends; publishing the same content twice appends it twice. The contract
  (``core/contracts/event_bus.py``) is at-least-once with consumer-side de-duplication by
  ``message_id``; neither the Protocol nor the in-memory bus de-duplicates on publish, so this
  adapter does not either.
- ``poll(consumer, topic, limit)`` returns the messages whose ``message_id`` that consumer has not
  acknowledged on that topic, in publish order; an acknowledged id is never delivered to that
  consumer again (including later copies of the same content).
- ``ack`` is idempotent; acknowledging an id the topic does not (yet) hold is accepted, as in
  memory. One deliberate difference: an ``ack`` whose ``message_id`` is not a content hash
  (64 lowercase hex characters) cannot name any message and is refused with ``ValueError``
  (the in-memory bus silently records it).
- Returned messages are the JSON form of what was published (``message_id`` is computed from that
  form, so the identity is unchanged), the same before and after a restart.

Layout under ``root`` (one bus per directory)::

    .lock                       exclusive flock while a FileEventBus has it open
    bus.json                    {"format": "hlens.file_event_bus", "schema_version": "1.0.0"}
    topics/<topic>.jsonl        per-topic hash-chained append-only log (``journal.py``),
                                one ``bus_message`` record per publish
    consumers/<sha256>.json     per-(consumer, topic) state, replaced atomically

Durability: a ``publish`` that returned is fsync'd in the topic log; an ``ack`` that returned is
fsync'd in the consumer state (temporary file, ``fsync``, ``os.replace``, directory ``fsync``). A
crash between ``poll`` and ``ack`` re-delivers after reopening (at least once). The consumer state
holds the acknowledged id set, the **offset** (the low-water mark: every log entry before it is
acknowledged; ``poll`` starts there), and the topic log's length and head hash when it was written;
a ``state_hash`` over all of it detects edits.

Fail closed: opening replays and verifies everything and raises ``BusCorrupted`` — never skips or
repairs — on a tampered, reordered or partial log line, a broken chain, a record that is not a valid
``BusMessage`` of that topic, an edited consumer state, a consumer state whose log length / head no
longer matches the log (lines dropped from the end, or a different log), an offset that disagrees
with the log and acked set, an unknown file in the bus directories, or a non-empty directory that is
not a bus. Honest boundary: whole lines dropped from the end of a topic log **after** the last
consumer state was written are not detectable (no anchor saw them) — unless an external anchor is
used.

External anchor (optional, ``anchor=<path>``; 2026-09-26): a hash-chained journal file **outside**
``root``. After every ``publish`` the topic's new length and head hash are appended to it as one
``topic_head`` line (fsync'd). Opening the bus verifies every anchored topic: its log must still
have at least the anchored length and the entry at that length must carry the anchored head hash;
a topic the anchor knows but whose log is gone is refused too — all ``BusCorrupted``. This detects
lines dropped from the end of *any* topic, including ones no consumer has acknowledged. A log
longer than its anchor is the one legitimate crash window (the message was fsync'd, the anchor line
was not) and is re-anchored on opening. The anchor file itself is append-only and hash-chained;
removing lines from *its* end together with the matching log lines stays undetectable — keep it
on storage the bus directory's writer cannot roll back.

Concurrency: one writer. The constructor takes a non-blocking exclusive ``fcntl.flock`` on
``root/.lock`` and raises ``BusLocked`` if another ``FileEventBus`` (in this or any process) holds
it; the lock is released by ``close()`` or by process exit (the kernel drops flocks), so a crash
never leaves a stale lock. POSIX only. Concurrent writers and cross-process readers are out of
scope.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from pydantic import ValidationError

from core.contracts.event_bus import BusMessage
from core.domain.base import canonical_json
from infrastructure.event_bus.journal import (
    GENESIS_HASH,
    AppendOnlyJournal,
    JournalCorrupted,
    fsync_directory,
)

__all__ = ["BUS_FORMAT", "BUS_SCHEMA_VERSION", "BusCorrupted", "BusLocked", "FileEventBus"]

BUS_FORMAT: Final = "hlens.file_event_bus"
BUS_SCHEMA_VERSION: Final = "1.0.0"
MESSAGE_RECORD: Final = "bus_message"
ANCHOR_RECORD: Final = "topic_head"

_MARKER: Final = "bus.json"
_LOCK: Final = ".lock"
_TOPICS: Final = "topics"
_CONSUMERS: Final = "consumers"
_TOPIC_RE: Final = re.compile(r"^[a-z][a-z0-9_.]*$")  # BusMessage.topic's pattern
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_STATE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "consumer",
        "topic",
        "offset",
        "acked",
        "log_length",
        "log_head_hash",
        "state_hash",
    }
)


class BusCorrupted(RuntimeError):
    """The bus directory does not replay to a consistent state; nothing is skipped or repaired."""


class BusLocked(RuntimeError):
    """Another ``FileEventBus`` holds this directory (one writer only)."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _consumer_file(consumer: str, topic: str) -> str:
    """Consumer names are free text, so the file name is a hash of ``(consumer, topic)``."""
    return _sha256(canonical_json([consumer, topic])) + ".json"


class _Consumer:
    """Acknowledged ids and the low-water mark of one (consumer, topic)."""

    __slots__ = ("acked", "offset")

    def __init__(self, acked: set[str], offset: int) -> None:
        self.acked = acked
        self.offset = offset


class FileEventBus:
    """``EventBusAdapter`` over a directory; see the module docstring for the disk contract."""

    def __init__(self, root: Path, *, anchor: Path | None = None) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        if anchor is not None:
            resolved_root, resolved_anchor = self._root.resolve(), Path(anchor).resolve()
            if resolved_anchor.is_relative_to(resolved_root):
                raise ValueError("the bus anchor must live outside the bus directory")
        self._lock_fd: int | None = self._acquire_lock()
        try:
            self._logs: dict[str, AppendOnlyJournal] = {}
            self._messages: dict[str, list[BusMessage]] = {}
            self._consumers: dict[tuple[str, str], _Consumer] = {}
            self._anchor: AppendOnlyJournal | None = None
            self._open_layout()
            self._load_topics()
            self._load_consumers()
            if anchor is not None:
                self._open_anchor(Path(anchor))
        except BaseException:
            self.close()
            raise

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def _acquire_lock(self) -> int:
        fd = os.open(self._root / _LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise BusLocked(f"{self._root} is held by another FileEventBus") from exc
        except BaseException:
            os.close(fd)
            raise
        return fd

    def close(self) -> None:
        """Release the directory lock; the object is unusable afterwards. Idempotent."""
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> FileEventBus:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._lock_fd is None:
            raise RuntimeError("this FileEventBus is closed")

    # -- opening -----------------------------------------------------------------------------

    def _open_layout(self) -> None:
        marker = self._root / _MARKER
        expected = {"format": BUS_FORMAT, "schema_version": BUS_SCHEMA_VERSION}
        if not marker.exists():
            others = sorted(p.name for p in self._root.iterdir() if p.name != _LOCK)
            if others:
                raise BusCorrupted(f"{self._root} is not empty and is not a bus: {others}")
            self._write_atomically(marker, expected)
        else:
            try:
                found = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise BusCorrupted(f"{marker} is not valid JSON") from exc
            if found != expected:
                raise BusCorrupted(f"{marker} is {found!r}, expected {expected!r}")
            allowed = {_LOCK, _MARKER, _TOPICS, _CONSUMERS}
            unknown = sorted(p.name for p in self._root.iterdir() if p.name not in allowed)
            if unknown:
                raise BusCorrupted(f"{self._root} holds unknown entries: {unknown}")
        (self._root / _TOPICS).mkdir(exist_ok=True)
        (self._root / _CONSUMERS).mkdir(exist_ok=True)

    def _load_topics(self) -> None:
        for path in sorted((self._root / _TOPICS).iterdir()):
            topic = path.name.removesuffix(".jsonl")
            if not path.is_file() or path.suffix != ".jsonl" or not _TOPIC_RE.match(topic):
                raise BusCorrupted(f"{path} is not a topic log")
            try:
                log = AppendOnlyJournal(path)
            except JournalCorrupted as exc:
                raise BusCorrupted(f"topic {topic!r}: {exc}") from exc
            messages: list[BusMessage] = []
            for entry in log.entries:
                if entry.type != MESSAGE_RECORD:
                    raise BusCorrupted(f"{path}:{entry.seq} is a {entry.type!r} record")
                try:
                    message = BusMessage.model_validate(entry.payload)
                except ValidationError as exc:
                    raise BusCorrupted(f"{path}:{entry.seq} is not a valid BusMessage") from exc
                if message.topic != topic:
                    raise BusCorrupted(f"{path}:{entry.seq} belongs to topic {message.topic!r}")
                messages.append(message)
            self._logs[topic] = log
            self._messages[topic] = messages

    def _load_consumers(self) -> None:
        for path in sorted((self._root / _CONSUMERS).iterdir()):
            if path.name.endswith(".tmp"):
                continue  # an interrupted atomic write; the state it would replace is intact
            if not path.is_file() or path.suffix != ".json":
                raise BusCorrupted(f"{path} is not a consumer state")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise BusCorrupted(f"{path} is not valid JSON") from exc
            consumer, topic, state = self._verify_consumer_state(path, raw)
            self._consumers[(consumer, topic)] = state

    def _verify_consumer_state(self, path: Path, raw: Any) -> tuple[str, str, _Consumer]:
        if not isinstance(raw, dict) or set(raw) != _STATE_FIELDS:
            raise BusCorrupted(f"{path} does not have exactly {sorted(_STATE_FIELDS)}")
        body = {k: v for k, v in raw.items() if k != "state_hash"}
        if raw["state_hash"] != _sha256(canonical_json(body)):
            raise BusCorrupted(f"{path}: state hash does not match (edited)")
        consumer, topic = raw["consumer"], raw["topic"]
        if raw["schema_version"] != BUS_SCHEMA_VERSION:
            raise BusCorrupted(f"{path}: unknown schema_version {raw['schema_version']!r}")
        if not isinstance(consumer, str) or not isinstance(topic, str):
            raise BusCorrupted(f"{path}: consumer and topic must be text")
        if path.name != _consumer_file(consumer, topic):
            raise BusCorrupted(f"{path}: file name does not match its (consumer, topic)")
        acked, offset, length = raw["acked"], raw["offset"], raw["log_length"]
        if (
            not isinstance(acked, list)
            or not all(isinstance(i, str) and _HEX64_RE.match(i) for i in acked)
            or acked != sorted(set(acked))
        ):
            raise BusCorrupted(f"{path}: acked must be sorted, unique message ids")
        for name, value in (("offset", offset), ("log_length", length)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise BusCorrupted(f"{path}: {name} must be a non-negative integer")
        messages = self._messages.get(topic, [])
        log = self._logs.get(topic)
        if length > len(messages):
            raise BusCorrupted(
                f"{path}: topic {topic!r} had {length} messages when this state was saved, "
                f"it now has {len(messages)} (the log lost lines)"
            )
        head = log.entry(length - 1).hash if (log is not None and length) else GENESIS_HASH
        if raw["log_head_hash"] != head:
            raise BusCorrupted(f"{path}: topic {topic!r} is not the log this state was saved on")
        acked_set = set(acked)
        if offset != _low_water_mark(messages[:length], acked_set, 0):
            raise BusCorrupted(f"{path}: offset disagrees with the log and the acked set")
        return consumer, topic, _Consumer(acked_set, offset)

    def _open_anchor(self, path: Path) -> None:
        try:
            anchor = AppendOnlyJournal(path)
        except JournalCorrupted as exc:
            raise BusCorrupted(f"bus anchor: {exc}") from exc
        anchored: dict[str, tuple[int, str]] = {}
        for entry in anchor.entries:
            payload = entry.payload
            if entry.type != ANCHOR_RECORD or set(payload) != {"topic", "length", "head_hash"}:
                raise BusCorrupted(f"{path}:{entry.seq} is not a topic head")
            topic, length, head = payload["topic"], payload["length"], payload["head_hash"]
            if (
                not isinstance(topic, str)
                or not _TOPIC_RE.match(topic)
                or not isinstance(length, int)
                or isinstance(length, bool)
                or length < 1
                or not isinstance(head, str)
            ):
                raise BusCorrupted(f"{path}:{entry.seq} is not a valid topic head")
            previous = anchored.get(topic)
            if previous is not None and length <= previous[0]:
                raise BusCorrupted(f"{path}:{entry.seq} moves topic {topic!r} backwards")
            anchored[topic] = (length, head)
        for topic, (length, head) in sorted(anchored.items()):
            log = self._logs.get(topic)
            have = len(log) if log is not None else 0
            if log is None or have < length:
                raise BusCorrupted(
                    f"topic {topic!r} was anchored at {length} messages, the log now has {have} "
                    "(lines were dropped from its end)"
                )
            if log.entry(length - 1).hash != head:
                raise BusCorrupted(f"topic {topic!r} is not the log the anchor saw")
        self._anchor = anchor
        for topic, log in sorted(self._logs.items()):  # re-anchor the legitimate crash window
            if len(log) and anchored.get(topic, (0, ""))[0] < len(log):
                self._publish_anchor(topic, log)

    def _publish_anchor(self, topic: str, log: AppendOnlyJournal) -> None:
        if self._anchor is None:
            return
        try:
            self._anchor.append(
                ANCHOR_RECORD, {"topic": topic, "length": len(log), "head_hash": log.head_hash}
            )
        except JournalCorrupted as exc:
            raise BusCorrupted(f"bus anchor: {exc}") from exc

    # -- EventBusAdapter ---------------------------------------------------------------------

    def publish(self, message: BusMessage) -> None:
        if not isinstance(message, BusMessage):
            raise TypeError("publish needs a BusMessage")
        self._check_open()
        stored = BusMessage.model_validate(message.model_dump(mode="json"))
        log = self._logs.get(message.topic)
        if log is None:
            log = AppendOnlyJournal(self._root / _TOPICS / f"{message.topic}.jsonl")
        try:
            log.append(MESSAGE_RECORD, stored.model_dump(mode="json"))
        except JournalCorrupted as exc:
            raise BusCorrupted(f"topic {message.topic!r}: {exc}") from exc
        self._logs[message.topic] = log
        self._messages.setdefault(message.topic, []).append(stored)
        self._publish_anchor(message.topic, log)

    def poll(self, consumer: str, topic: str, limit: int) -> tuple[BusMessage, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._check_open()
        state = self._consumers.get((consumer, topic))
        acked, start = (state.acked, state.offset) if state is not None else (set(), 0)
        pending: list[BusMessage] = []
        for message in self._messages.get(topic, [])[start:]:
            if message.message_id not in acked:
                pending.append(message)
                if len(pending) == limit:
                    break
        return tuple(pending)

    def ack(self, consumer: str, topic: str, message_id: str) -> None:
        self._check_open()
        if not isinstance(message_id, str) or not _HEX64_RE.match(message_id):
            raise ValueError("message_id must be a content hash (64 lowercase hex characters)")
        state = self._consumers.get((consumer, topic))
        if state is not None and message_id in state.acked:
            return  # idempotent
        acked = (set(state.acked) if state is not None else set()) | {message_id}
        messages = self._messages.get(topic, [])
        offset = _low_water_mark(messages, acked, state.offset if state is not None else 0)
        log = self._logs.get(topic)
        body: dict[str, Any] = {
            "schema_version": BUS_SCHEMA_VERSION,
            "consumer": consumer,
            "topic": topic,
            "offset": offset,
            "acked": sorted(acked),
            "log_length": len(messages),
            "log_head_hash": log.head_hash if log is not None else GENESIS_HASH,
        }
        body["state_hash"] = _sha256(canonical_json(body))
        self._write_atomically(self._root / _CONSUMERS / _consumer_file(consumer, topic), body)
        self._consumers[(consumer, topic)] = _Consumer(acked, offset)

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _write_atomically(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)


def _low_water_mark(messages: list[BusMessage], acked: set[str], start: int) -> int:
    """First index at or after ``start`` whose message is not acknowledged."""
    index = start
    while index < len(messages) and messages[index].message_id in acked:
        index += 1
    return index
