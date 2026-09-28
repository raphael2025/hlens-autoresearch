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

Retrieval (docs/research/failure-registry.md: the registry is searchable before a new hypothesis
is registered, and keeps a per-``reason_code`` tally; MOD-VALID 2026-09-28): ``query`` filters the
validated records by subject, hypothesis family, reason code, terminal state and / or gate (every
filter optional, exact match, append order kept); ``reason_counts`` tallies the records per
``reason_code``. Both read through ``records``, so corruption still fails closed; neither writes.

This is the research-plane write path. The authoritative registry is the Control Plane (not built
yet); ``docs/research/failure-registry.md`` stays the human-readable index.

Write gate (ADR-0073 admission lease review, 2026-09-28): a loop state directory binds its registry
to its admission gate (``bind_write_gate``; ``research.persistence.gate``), so every ``append`` runs
inside the gate and is refused before writing while the state does not accept writes.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from core.domain.base import Ref
from core.domain.research import FailureRecord
from core.errors import ReasonCode
from research.persistence import WriteGate, gate_scope

__all__ = ["FailureRegistry", "FailureRegistryCorrupted"]


class FailureRegistryCorrupted(RuntimeError):
    """The registry file lost bytes or holds an unparsable line; history must not be rewritten."""


class FailureRegistry:
    """Append-only JSON-lines store of ``FailureRecord``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        #: Orders appends against reads of the file (taken after the gate).
        self._lock = RLock()
        self._gate: WriteGate | None = None
        self._seen = 0
        self._seen = self._size()

    def bind_write_gate(self, gate: WriteGate) -> None:
        """Run every later ``append`` inside ``gate`` (module docs); once only."""
        with self._lock:
            if self._gate is not None:
                raise ValueError("this failure registry is already bound to a write gate")
            self._gate = gate

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
        with gate_scope(self._gate, "appending a failure record"), self._lock:
            self._size()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._seen = self._size()

    def records(self) -> tuple[FailureRecord, ...]:
        """Every record in append order; any unparsable line is corruption."""
        with self._lock:
            self._size()
            if not self._path.exists():
                return ()
            text = self._path.read_text(encoding="utf-8")
        out: list[FailureRecord] = []
        for number, line in enumerate(text.splitlines(), 1):
            try:
                out.append(FailureRecord.model_validate_json(line))
            except ValidationError as exc:
                raise FailureRegistryCorrupted(f"{self._path}:{number} is not a record") from exc
        return tuple(out)

    def query(
        self,
        *,
        subject: Ref | None = None,
        family: str | None = None,
        reason: ReasonCode | None = None,
        terminal_state: str | None = None,
        gate_id: str | None = None,
    ) -> tuple[FailureRecord, ...]:
        """The records matching every given filter (exact match; ``None`` = any), in append
        order. ``subject`` matches the record's ``subject_ref`` target (kind, name, version)."""
        if terminal_state is not None and terminal_state not in {"REJECTED", "FAILED"}:
            raise ValueError("the Failure Registry only holds REJECTED / FAILED records")
        wanted = None if subject is None else subject.target_identity()
        return tuple(
            record
            for record in self.records()
            if (wanted is None or record.subject_ref.target_identity() == wanted)
            and (family is None or record.hypothesis_family_id == family)
            and (reason is None or record.reason_code is reason)
            and (terminal_state is None or record.terminal_state == terminal_state)
            and (gate_id is None or record.gate_id == gate_id)
        )

    def reason_counts(self) -> dict[str, int]:
        """Records per ``reason_code`` value, sorted by code (the failure-mode tally of
        docs/research/failure-registry.md); a code without records is absent."""
        counts = Counter(record.reason_code.value for record in self.records())
        return dict(sorted(counts.items()))
