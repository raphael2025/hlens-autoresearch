"""Append-only Failure Registry write path for rejected / failed strategies (Phase 5; ADR-0038).

Failed experiments are research assets and are never deleted (CLAUDE.md H6,
docs/research/failure-registry.md). ``FailureRegistry`` stores
``core.domain.research.FailureRecord`` payloads as one JSON document per line in a caller-chosen
file:

- the only write is ``append``: open in append mode, write one line, flush and ``fsync``;
- there is no delete, update or rewrite method; a correction is a new record;
- the registry remembers how many bytes it has seen and refuses to append after the file shrank
  (someone truncated or rewrote history) — ``FailureRegistryCorrupted``, fail closed;
- ``records`` re-validates every line; an unparsable line is corruption, not something to skip.

This is the research-plane write path. The authoritative registry is the Control Plane (not built
yet); ``docs/research/failure-registry.md`` stays the human-readable index.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from core.domain.research import FailureRecord

__all__ = ["FailureRegistry", "FailureRegistryCorrupted"]


class FailureRegistryCorrupted(RuntimeError):
    """The registry file lost bytes or holds an unparsable line; history must not be rewritten."""


class FailureRegistry:
    """Append-only JSON-lines store of ``FailureRecord``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._seen = 0
        self._seen = self._size()

    @property
    def path(self) -> Path:
        return self._path

    def _size(self) -> int:
        size = self._path.stat().st_size if self._path.exists() else 0
        if size < self._seen:
            raise FailureRegistryCorrupted(f"{self._path} shrank: failure history was rewritten")
        return size

    def append(self, record: FailureRecord) -> None:
        """Append one record (never overwrites; a correction is a new record)."""
        if type(record) is not FailureRecord:
            raise TypeError("append needs a FailureRecord")
        line = FailureRecord.model_validate_json(record.model_dump_json()).model_dump_json()
        self._size()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._seen = self._size()

    def records(self) -> tuple[FailureRecord, ...]:
        """Every record in append order; any unparsable line is corruption."""
        self._size()
        if not self._path.exists():
            return ()
        out: list[FailureRecord] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                out.append(FailureRecord.model_validate_json(line))
            except ValidationError as exc:
                raise FailureRegistryCorrupted(f"{self._path}:{number} is not a record") from exc
        return tuple(out)
