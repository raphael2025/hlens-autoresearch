"""Hash-chained append-only JSON-lines journal (debugging pass, 2026-09-25).

Each mutation of a durable ledger is one line::

    {"seq": <int>, "type": <str>, "payload": <json>,
     "prev_hash": <sha256 hex>, "hash": <sha256 hex>}

``seq`` starts at 1 and increases by one per line. ``prev_hash`` of the first line is
``GENESIS_HASH`` (64 zero characters); every later line's ``prev_hash`` is the previous line's
``hash``. ``hash`` is the SHA-256 of the canonical JSON (``core.domain.base.canonical_json``) of
``{"seq", "type", "payload", "prev_hash"}`` — the line authenticates its own content and its
position in the chain.

Durability matches ``research.strategies.failure_registry.FailureRegistry``: the only write is
``append`` (open in append mode, write one line, flush, ``fsync``); there is no update, delete or
rewrite; the journal remembers how many bytes it has seen and refuses to append after the file
shrank (``JournalCorrupted``, fail closed). On top of that, opening an existing file replays every
line and verifies the chain: a bad hash, a broken ``prev_hash`` link, a line that is not valid
JSON or is missing a field, a wrong ``seq``, or a truncated trailing line is corruption — refused,
never silently skipped or repaired.

This module only knows about the chain, not what the lines mean; each ledger (sealed OOS, trial,
lineage) interprets ``type`` / ``payload`` itself and must fail closed on a ``type`` it does not
recognize (CLAUDE.md H4/H6: never weaken a check, never drop history).
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

__all__ = ["GENESIS_HASH", "AppendOnlyJournal", "JournalCorrupted", "JournalEntry"]

#: ``prev_hash`` of the first line in a journal — 64 ``"0"`` characters, not a real SHA-256 output.
GENESIS_HASH = "0" * 64


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
    """One hash-chained JSON-lines file; ``append`` is the only mutation.

    Opening an existing file replays and verifies the whole chain before any append is accepted;
    a corrupted file stays corrupted (no auto-repair) and every further operation on it raises
    ``JournalCorrupted`` again.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._entries: list[JournalEntry] = []
        self._seen = 0
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        """Every entry in append order (never mutated; replay result or post-append record)."""
        return tuple(self._entries)

    @property
    def head_hash(self) -> str:
        """The chain's current tip: ``GENESIS_HASH`` for an empty journal.

        Deterministic given the file's content — this is the journal's state hash: two journals
        replayed from byte-identical files always agree on it, and it changes iff an entry does.
        """
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
            try:
                seq = raw["seq"]
                type_ = raw["type"]
                payload = raw["payload"]
                recorded_prev = raw["prev_hash"]
                recorded_hash = raw["hash"]
            except KeyError as exc:
                raise JournalCorrupted(f"{self._path}:{number} is missing a field") from exc
            if set(raw) != {"seq", "type", "payload", "prev_hash", "hash"}:
                raise JournalCorrupted(f"{self._path}:{number} has unexpected fields")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq != number:
                raise JournalCorrupted(f"{self._path}:{number} has the wrong sequence number")
            if not isinstance(type_, str) or not type_:
                raise JournalCorrupted(f"{self._path}:{number} has a bad record type")
            if recorded_prev != prev_hash:
                raise JournalCorrupted(f"{self._path}:{number} breaks the hash chain")
            expected = _entry_hash(seq, type_, payload, recorded_prev)
            if not isinstance(recorded_hash, str) or expected != recorded_hash:
                raise JournalCorrupted(f"{self._path}:{number} content hash does not match")
            entry = JournalEntry(seq, type_, payload, recorded_prev, recorded_hash)
            entries.append(entry)
            prev_hash = entry.hash
        self._entries = entries
        self._seen = self._size()

    def append(self, type_: str, payload: Mapping[str, Any]) -> JournalEntry:
        """Append one record; refuses if the file shrank since it was last read or written."""
        if not type_:
            raise ValueError("a journal record needs a non-empty type")
        self._size()
        seq = len(self._entries) + 1
        prev_hash = self.head_hash
        payload_dict = canonical_json(payload)  # round-trip: reject NaN/Infinity, normalize keys
        payload_json: Mapping[str, Any] = json.loads(payload_dict)
        entry_hash = _entry_hash(seq, type_, payload_json, prev_hash)
        record = {
            "seq": seq,
            "type": type_,
            "payload": payload_json,
            "prev_hash": prev_hash,
            "hash": entry_hash,
        }
        line = canonical_json(record)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        entry = JournalEntry(seq, type_, payload_json, prev_hash, entry_hash)
        self._entries.append(entry)
        self._seen = self._size()
        return entry
