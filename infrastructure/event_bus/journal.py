"""Hash-chained append-only JSON-lines journal behind ``FileEventBus`` (ADR-0044 implementation
note, file-backed bus, 2026-09-26).

This is the **third independent implementation of one on-disk contract**, next to
``research.persistence.AppendOnlyJournal`` and ``apps.worker.journal.AppendOnlyJournal``; a test
writes the same records with this one and the worker's and requires byte-identical files that
replay in either. It is not imported from either of them:

- ``infrastructure/`` must not import ``research/`` (research code is never production code,
  CLAUDE.md H5; dependency direction ``apps -> application -> domain <- infrastructure``);
- ``infrastructure/`` must not import ``apps/`` (that inverts the same direction);
- ``core/`` cannot host it: the Domain has no I/O, and adding a journal there would change the
  frozen core packages (H1) for a storage concern.

The contract. Each record is one line::

    {"seq": <int>, "type": <str>, "payload": <json>,
     "prev_hash": <sha256 hex>, "hash": <sha256 hex>}

``seq`` starts at 1 and increases by one per line. ``prev_hash`` of the first line is
``GENESIS_HASH`` (64 zero characters); every later line's ``prev_hash`` is the previous line's
``hash``. ``hash`` is the SHA-256 of the canonical JSON (``core.domain.base.canonical_json``) of
``{"seq", "type", "payload", "prev_hash"}``.

Durability: the only write is ``append`` (append mode, one line, flush, ``fsync``); there is no
update, delete or rewrite, and appending after the file shrank is refused. Opening an existing file
replays every line and verifies the chain: a bad hash, a broken link, a line that is not a JSON
object with exactly the five fields, a wrong ``seq``, a blank line or a partial trailing line is
``JournalCorrupted`` — refused, never skipped or repaired (fail closed).

Honest boundary: dropping whole lines from the **end** of the file leaves a valid, shorter chain;
only an anchor outside the file (``FileEventBus`` binds each consumer's saved state to the log
head it had seen) detects it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.domain.base import canonical_json

__all__ = [
    "GENESIS_HASH",
    "AppendOnlyJournal",
    "JournalCorrupted",
    "JournalEntry",
    "fsync_directory",
]

#: ``prev_hash`` of the first line — 64 ``"0"`` characters, not a real SHA-256 output.
GENESIS_HASH = "0" * 64

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

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[JournalEntry] = []
        self._seen = 0
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, index: int) -> JournalEntry:
        """The entry at 0-based ``index`` (``seq == index + 1``)."""
        return self._entries[index]

    @property
    def head_hash(self) -> str:
        """The chain's tip (``GENESIS_HASH`` when empty)."""
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
        text = self._path.read_text(encoding="utf-8")
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
        self._seen = self._size()

    def append(self, type_: str, payload: Mapping[str, Any]) -> JournalEntry:
        """Append one record (fsync'd); refuses if the file shrank since it was last seen."""
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
        created = not self._path.exists()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            fsync_directory(self._path.parent)
        entry = JournalEntry(seq, type_, payload_json, prev_hash, entry_hash)
        self._entries.append(entry)
        self._seen = self._size()
        return entry


def fsync_directory(path: Path) -> None:
    """Make a directory entry (a new or replaced file name) durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
