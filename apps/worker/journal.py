"""Hash-chained append-only JSON-lines journal for the worker (ADR-0049 durable audit, 2026-09-26).

This is an **independent implementation of the same on-disk contract** as the research plane's
``research.persistence.AppendOnlyJournal`` (a test replays files written by either with the
other). It is not imported from there: ``apps/`` must not import ``research/`` (01-system.md §3),
the worker loop depends only on ``core`` and the standard library (ADR-0049 §1, statically
tested), and moving research code into ``apps/`` would be a direct research → production move
(CLAUDE.md H5). ``core`` cannot host it either (the Domain has no I/O).

The contract. Each record is one line::

    {"seq": <int>, "type": <str>, "payload": <json>,
     "prev_hash": <sha256 hex>, "hash": <sha256 hex>}

``seq`` starts at 1 and increases by one per line. ``prev_hash`` of the first line is
``GENESIS_HASH`` (64 zero characters); every later line's ``prev_hash`` is the previous line's
``hash``. ``hash`` is the SHA-256 of the canonical JSON (``core.domain.base.canonical_json``) of
``{"seq", "type", "payload", "prev_hash"}``.

Durability: the only write is ``append`` (open in append mode, write one line, flush, ``fsync``);
there is no update, delete or rewrite. Opening an existing file replays every line and verifies
the chain: a bad hash, a broken link, a line that is not a JSON object with exactly the five
fields, a wrong ``seq`` or a partial trailing line is ``JournalCorrupted`` — refused, never
skipped or repaired (fail closed; CLAUDE.md H4 / H6).

Concurrent holders (2026-09-27, same rule as the research journal). Two instances on one file —
two processes, or two objects in one process — each replay the file when opened; neither sees what
the other appends later. Replay therefore reads under a shared ``fcntl.flock`` (never a
half-written line of a concurrent append) and remembers exactly the bytes it parsed; ``append``
holds an exclusive ``fcntl.flock`` across write and ``fsync`` and writes only if the file is still
**exactly** the size this instance last read or wrote. A file that grew (another writer appended)
is ``JournalCorrupted`` and nothing is written — the stale instance must be reopened to replay the
other writer's lines — and a file that shrank is refused as rewritten history. Without this a stale
instance appended with a duplicate ``seq`` / stale ``prev_hash``, and every later open refused the
broken chain. POSIX only; the lock is advisory, so every writer must use this contract.

Honest boundary: dropping whole lines from the **end** of the file leaves a valid, shorter chain;
only an externally anchored head hash (e.g. the ``record_hash`` published on the bus) detects it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.domain.base import canonical_json

__all__ = ["GENESIS_HASH", "AppendOnlyJournal", "JournalCorrupted", "JournalEntry", "JournalPath"]

#: ``prev_hash`` of the first line — 64 ``"0"`` characters, not a real SHA-256 output.
GENESIS_HASH = "0" * 64

#: Where a journal lives (a path-like; never a URL or a database).
type JournalPath = str | os.PathLike[str]

_FIELDS = frozenset({"seq", "type", "payload", "prev_hash", "hash"})


class JournalCorrupted(RuntimeError):
    """The journal file lost bytes, broke its hash chain, or holds an unparsable/tampered line."""


@dataclass(frozen=True)
class JournalEntry:
    """One replayed/appended line, already verified against the chain."""

    seq: int
    type: str
    payload: Mapping[str, Any]
    prev_hash: str
    hash: str


def _entry_hash(seq: int, type_: str, payload: Any, prev_hash: str) -> str:
    body = canonical_json({"seq": seq, "type": type_, "payload": payload, "prev_hash": prev_hash})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AppendOnlyJournal:
    """One hash-chained JSON-lines file; ``append`` is the only mutation."""

    def __init__(self, path: JournalPath) -> None:
        self._path = Path(path)
        self._entries: list[JournalEntry] = []
        self._seen = 0
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        """Every entry in append order."""
        return tuple(self._entries)

    @property
    def head_hash(self) -> str:
        """The chain's tip (``GENESIS_HASH`` when empty); deterministic given the file content."""
        return self._entries[-1].hash if self._entries else GENESIS_HASH

    def _size(self) -> int:
        size = self._path.stat().st_size if self._path.exists() else 0
        if size < self._seen:
            raise JournalCorrupted(f"{self._path} shrank: journal history was rewritten")
        return size

    def _load(self) -> None:
        self._seen = self._size()
        if not self._path.exists():
            return
        with self._path.open("rb") as handle:
            # shared lock: a concurrent ``append`` (exclusive) is wholly visible or not at all
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                data = handle.read()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if len(data) < self._seen:
            raise JournalCorrupted(f"{self._path} shrank: journal history was rewritten")
        text = data.decode("utf-8")
        if text and not text.endswith("\n"):
            raise JournalCorrupted(f"{self._path} ends in a partial trailing line")
        prev_hash = GENESIS_HASH
        entries: list[JournalEntry] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line:
                raise JournalCorrupted(f"{self._path}:{number} is a blank line")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalCorrupted(f"{self._path}:{number} is not valid JSON") from exc
            if not isinstance(raw, dict):
                raise JournalCorrupted(f"{self._path}:{number} is not a JSON object")
            if set(raw) != _FIELDS:
                raise JournalCorrupted(f"{self._path}:{number} does not have exactly {_FIELDS}")
            seq, type_, payload = raw["seq"], raw["type"], raw["payload"]
            recorded_prev, recorded_hash = raw["prev_hash"], raw["hash"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq != number:
                raise JournalCorrupted(f"{self._path}:{number} has the wrong sequence number")
            if not isinstance(type_, str) or not type_:
                raise JournalCorrupted(f"{self._path}:{number} has a bad record type")
            if recorded_prev != prev_hash:
                raise JournalCorrupted(f"{self._path}:{number} breaks the hash chain")
            if not isinstance(recorded_hash, str) or recorded_hash != _entry_hash(
                seq, type_, payload, recorded_prev
            ):
                raise JournalCorrupted(f"{self._path}:{number} content hash does not match")
            entries.append(JournalEntry(seq, type_, payload, recorded_prev, recorded_hash))
            prev_hash = recorded_hash
        self._entries = entries
        # exactly the bytes parsed — never a re-stat, which could count a concurrent append that
        # this replay did not see and so let a stale instance append after it
        self._seen = len(data)

    def append(self, type_: str, payload: Mapping[str, Any]) -> JournalEntry:
        """Append one record (fsync'd); refuses if the file changed since last read/written."""
        if not type_:
            raise ValueError("a journal record needs a non-empty type")
        self._size()
        seq = len(self._entries) + 1
        prev_hash = self.head_hash
        # round-trip through canonical JSON: rejects NaN / Infinity and non-JSON values
        payload_json: Mapping[str, Any] = json.loads(canonical_json(payload))
        entry_hash = _entry_hash(seq, type_, payload_json, prev_hash)
        line = canonical_json(
            {
                "seq": seq,
                "type": type_,
                "payload": payload_json,
                "prev_hash": prev_hash,
                "hash": entry_hash,
            }
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            # exclusive lock, held until the line is fsync'd (released when the file closes)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            size = os.fstat(handle.fileno()).st_size
            if size < self._seen:
                raise JournalCorrupted(f"{self._path} shrank: journal history was rewritten")
            if size != self._seen:
                raise JournalCorrupted(
                    f"{self._path} changed since this instance last read it (another writer "
                    f"appended: {size} bytes on disk, {self._seen} seen); nothing was written — "
                    "reopen the journal to replay it"
                )
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            self._seen = os.fstat(handle.fileno()).st_size
        entry = JournalEntry(seq, type_, payload_json, prev_hash, entry_hash)
        self._entries.append(entry)
        return entry
