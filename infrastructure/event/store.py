"""Local, content-addressed store of event runs (Phase 3; ADR-0036; 2026-09-26).

``EventResultStore(root)`` keeps whole ``EventResult`` objects as ``<root>/<result_hash>.json``
(canonical JSON), so an event table can be re-read later instead of recomputed. It is an
**artifact store**, not a data-plane table: registering a physical Iceberg ``event.*`` table with
partitions and revision semantics is an architecture decision still open (see ``table.py``).

- ``put(result)``: publishes atomically (temporary file, ``fsync``, ``os.link`` to the final name,
  directory ``fsync``); an existing file with identical bytes is a no-op, anything else under the
  same name is refused (``EventStoreCorrupted``) — never overwritten.
- ``get(result_hash)``: reads the file, rebuilds the ``EventResult`` (its own validator re-checks
  ``result_hash`` against the request / provider / events) and requires the file name, the
  rebuilt hash and the exact canonical bytes to agree — any edit, truncation or rename is
  ``EventStoreCorrupted``; a missing file is ``KeyError``.
- ``hashes()``: every stored ``result_hash`` (each file name must be a content hash).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from core.contracts.event import EventResult
from core.domain.base import canonical_json

__all__ = ["EventResultStore", "EventStoreCorrupted"]

_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class EventStoreCorrupted(RuntimeError):
    """A stored event run does not match its name or content (never repaired)."""


def _encode(result: EventResult) -> bytes:
    return (canonical_json(result.model_dump(mode="json")) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EventResultStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, result_hash: str) -> Path:
        if not isinstance(result_hash, str) or not _HASH_RE.match(result_hash):
            raise ValueError("result_hash must be a content hash (64 lowercase hex characters)")
        return self._root / f"{result_hash}.json"

    def put(self, result: EventResult) -> Path:
        if not isinstance(result, EventResult):
            raise TypeError("put needs an EventResult")
        path = self._path(result.result_hash)
        data = _encode(result)
        if path.exists():
            if path.read_bytes() != data:
                raise EventStoreCorrupted(f"{path} exists with other content; never overwritten")
            return path
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:  # a concurrent identical put won the race
                if path.read_bytes() != data:
                    raise EventStoreCorrupted(f"{path} appeared with other content") from None
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_directory(self._root)
        return path

    def get(self, result_hash: str) -> EventResult:
        path = self._path(result_hash)
        if not path.exists():
            raise KeyError(result_hash)
        data = path.read_bytes()
        try:
            result = EventResult.model_validate_json(data)
        except ValidationError as exc:
            raise EventStoreCorrupted(f"{path} is not a valid EventResult") from exc
        if result.result_hash != result_hash:
            raise EventStoreCorrupted(f"{path} holds event run {result.result_hash}")
        if _encode(result) != data:
            raise EventStoreCorrupted(f"{path} is not the canonical form of its event run")
        return result

    def hashes(self) -> tuple[str, ...]:
        names: list[str] = []
        for path in sorted(self._root.iterdir()):
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                continue  # an interrupted put; the final name was never linked
            stem = path.name.removesuffix(".json")
            if not path.is_file() or path.suffix != ".json" or not _HASH_RE.match(stem):
                raise EventStoreCorrupted(f"{path} is not a stored event run")
            names.append(stem)
        return tuple(names)
