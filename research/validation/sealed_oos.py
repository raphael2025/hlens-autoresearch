"""Sealed OOS: a fixed-date window that each hypothesis family may unseal exactly once.

Constitution C-S1 ~ C-S3 and 07-validation.md §3: the window starts at the Profile's
``data_split.sealed_oos_boundary`` (UTC midnight) and lasts ``sealed_oos_length``; it does not roll
with the run time. Before an unsealing is recorded, the vault refuses to hand out any sample of the
window (``SealedOosLocked``). ``unseal`` records an ``OosUnsealing`` in an append-only ledger and
refuses a second unsealing of the same family (``OosAlreadyUnsealed``); the record is never
removed.

Budget and one-shot evaluation (Phase 8 fix, ADR-0041):

- the ledger's global unsealing count (C-S2: counted in a global budget) is bounded by the vault's
  ``max_unsealings``. A Profile that carries ``data_split.sealed_oos_max_unsealings`` (ADR-0052 §2)
  supplies the budget itself (``budget_source`` = that path) and an explicit ``max_unsealings``
  given as well is refused (``ExplicitParamRefused``, C-A4); a Profile without the field (every
  Profile before ADR-0052) needs the **required explicit parameter** as before (no default;
  ``budget_source`` records ``param:max_unsealings``). Switching the ``family_id`` therefore cannot
  re-open the same window without limit (``OosBudgetExhausted``);
- an unsealing buys **one** evaluation: ``sealed_view`` hands the window's samples out once per
  family and records that in the ledger; a second read raises ``SealedOosAlreadyEvaluated``;
- one-shot accounting at release (ADR-0041 review fixes 2, 2026-09-25): a caller that needs the
  window's raw data *before* it can build the labels (the research loop re-runs the strategy on
  the sealed bars first) takes a ``SealedEvaluation`` with ``claim_evaluation``. The claim marks
  the family evaluated in the ledger **before** any sealed sample leaves the vault, so the
  evaluation is consumed even when it later ends without a result (the caller records that as an
  INCONCLUSIVE ``consumed_without_result`` report, ``pipeline.sealed_oos_without_result``). The
  claim hands the bars out once (``take("bars")``) and the label spans once (``view``); after the
  claim nobody, the claimant included, can read the window through the vault again.

``InMemoryUnsealingLedger`` is the pure-memory ledger; ``DurableUnsealingLedger`` (debugging pass,
2026-09-25, ADR-0041 implementation note) is the same Protocol backed by a hash-chained
append-only file (``research.persistence.AppendOnlyJournal``), so "one unsealing per family",
"one evaluation per unsealing" and the global unsealing count survive a process restart. A
Control Plane ledger may implement the same ``UnsealingLedger`` Protocol later; that is not this
batch.

Write gate (ADR-0073 admission lease review, 2026-09-28): a loop state directory binds its
``DurableUnsealingLedger`` to its admission gate (``bind_write_gate``;
``research.persistence.gate``), so every ``record`` / ``mark_evaluated`` runs inside the gate and is
refused before writing while the state does not accept writes. The backing journal is never handed
out (its ``append`` would bypass the gate and the replayed state): cross-file checks read
``journal_head()`` or ``journal_snapshot()``.

Pre-registered replacement windows (ADR-0100 item 7, 2026-09-30; additive, journal format 2): the
research loop's optional replacement proposal trigger (``research.loop.replacement``) may only
evaluate on an **independent** sealed window — not the Profile's window above — that was
**pre-registered** (``RegisteredSealedWindow``: an id, fixed UTC bounds, who registered it and
when; the registration must not be after the window's first instant, so nobody saw its data when
it was declared) and that has **never been opened**. Opening one is recorded in the same unsealing
ledger as a ``replacement_window_opened`` line (``WindowOpening``: window id, registration hash,
the subject it was opened for, the trial that counts it, the loop and round); a window opens
**once**, for one subject, and is never reusable — not by the same subject, not by its
descendants, not by any other subject (``SealedWindowAlreadyOpened``). Openings do **not** count
against the Profile window's unsealing budget (``count()`` stays the number of family unsealings):
each replacement window is its own single-use budget. Journal format: format 1 is the ``unseal`` /
``mark_evaluated`` lines (unchanged bytes); format 2 adds ``replacement_window_opened``, whose
payload carries ``"format_version": 2``. A format-1 reader refuses the new line type (fail
closed); a ledger without openings is byte-identical to before.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Final, Protocol

from core.contracts.profile_selection import OosUnsealing
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import content_hash
from research.persistence import (
    AppendOnlyJournal,
    JournalCorrupted,
    JournalSnapshot,
    WriteGate,
    gate_scope,
    journal_snapshot,
)
from research.validation.gates import sourced_parameter
from research.validation.splits import LabeledSpan, midnight_utc

__all__ = [
    "REPLACEMENT_WINDOW_OPENED",
    "UNSEALING_LEDGER_FORMAT",
    "DurableUnsealingLedger",
    "InMemoryUnsealingLedger",
    "OosAlreadyUnsealed",
    "OosBudgetExhausted",
    "RegisteredSealedWindow",
    "SealedWindowAlreadyOpened",
    "WindowOpening",
    "SealedOosAlreadyEvaluated",
    "SealedOosLocked",
    "SealedEvaluation",
    "SealedOosVault",
    "SealedWindow",
    "UnsealingLedger",
]


class OosAlreadyUnsealed(Exception):
    """The family has already used its one unsealing."""


class SealedOosLocked(Exception):
    """Sealed OOS data was requested without a recorded unsealing."""


class OosBudgetExhausted(Exception):
    """The global unsealing budget of the window is used up."""


class SealedOosAlreadyEvaluated(Exception):
    """The family's single sealed OOS evaluation has already been handed out."""


class SealedWindowAlreadyOpened(Exception):
    """A pre-registered replacement window was opened before (single use; module docs)."""


#: Unsealing ledger journal format (module docs, **Pre-registered replacement windows**).
UNSEALING_LEDGER_FORMAT: Final = 2
#: The format-2 line type of one replacement window opening.
REPLACEMENT_WINDOW_OPENED: Final = "replacement_window_opened"
_WINDOW_ID: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_OPENING_FIELDS: Final = frozenset(
    {
        "format_version",
        "window_id",
        "registration_hash",
        "subject",
        "subject_hash",
        "trial",
        "trial_hash",
        "loop_id",
        "round_index",
    }
)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware (UTC) datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SealedWindow:
    start: datetime
    end: datetime

    @classmethod
    def from_profile(cls, profile: ValidationProfile) -> SealedWindow:
        start = midnight_utc(profile.data_split.sealed_oos_boundary)
        return cls(start=start, end=start + profile.data_split.sealed_oos_length)

    def touches(self, span: LabeledSpan) -> bool:
        """The span reaches into the window (its label is not known before the boundary)."""
        return span.end >= self.start and span.start < self.end

    def contains(self, span: LabeledSpan) -> bool:
        return self.start <= span.start and span.end <= self.end


@dataclass(frozen=True)
class RegisteredSealedWindow:
    """An independent sealed evaluation window, pre-registered (module docs).

    ``start`` / ``end`` are fixed UTC instants (``[start, end)``); ``registered_at`` must not be
    after ``start`` (the window's data did not exist when it was declared); ``registered_by`` is
    the declaring human. ``registration_hash`` covers the whole registration.
    """

    window_id: str
    start: datetime
    end: datetime
    registered_by: str
    registered_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or _WINDOW_ID.fullmatch(self.window_id) is None:
            raise ValueError(
                "window_id must be 1-128 chars of [a-z0-9_.-], starting with a letter or digit"
            )
        for name in ("start", "end", "registered_at"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.end <= self.start:
            raise ValueError(f"window {self.window_id!r} must end after it starts")
        if self.registered_at > self.start:
            raise ValueError(
                f"window {self.window_id!r} was registered at {self.registered_at.isoformat()}, "
                f"after it started ({self.start.isoformat()}): a pre-registered window is "
                "declared before any of its data exists"
            )
        if not isinstance(self.registered_by, str) or not self.registered_by.strip():
            raise ValueError("a registered window needs a non-empty registered_by")

    @property
    def window(self) -> SealedWindow:
        return SealedWindow(start=self.start, end=self.end)

    def payload(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "registered_by": self.registered_by,
            "registered_at": self.registered_at.isoformat(),
        }

    def registration_hash(self) -> str:
        return content_hash({"kind": "registered_sealed_window", **self.payload()})

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """``[start, end)`` shares an instant with this window."""
        return start < self.end and self.start < end

    def same_bounds(self, window: SealedWindow) -> bool:
        return (window.start, window.end) == (self.start, self.end)


@dataclass(frozen=True)
class WindowOpening:
    """One opening of a pre-registered replacement window (module docs; the ledger's format-2
    ``replacement_window_opened`` line). ``opening_hash`` covers the whole payload."""

    window_id: str
    registration_hash: str
    subject: str
    subject_hash: str
    trial: str
    trial_hash: str
    loop_id: str
    round_index: int

    def __post_init__(self) -> None:
        for name in (
            "window_id",
            "registration_hash",
            "subject",
            "subject_hash",
            "trial",
            "trial_hash",
            "loop_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a window opening needs a non-empty {name}")
        if (
            isinstance(self.round_index, bool)
            or not isinstance(self.round_index, int)
            or self.round_index < 0
        ):
            raise ValueError("round_index must be a non-negative int")

    def payload(self) -> dict[str, Any]:
        return {
            "format_version": UNSEALING_LEDGER_FORMAT,
            "window_id": self.window_id,
            "registration_hash": self.registration_hash,
            "subject": self.subject,
            "subject_hash": self.subject_hash,
            "trial": self.trial,
            "trial_hash": self.trial_hash,
            "loop_id": self.loop_id,
            "round_index": self.round_index,
        }

    def opening_hash(self) -> str:
        return content_hash({"kind": REPLACEMENT_WINDOW_OPENED, **self.payload()})

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WindowOpening:
        """Rebuild one recorded opening; any other shape or format is refused (``ValueError``)."""
        if set(payload) != _OPENING_FIELDS:
            raise ValueError("a window opening has exactly the recorded fields")
        if payload["format_version"] != UNSEALING_LEDGER_FORMAT:
            raise ValueError(f"unknown window opening format {payload['format_version']!r}")
        opening = cls(
            window_id=payload["window_id"],
            registration_hash=payload["registration_hash"],
            subject=payload["subject"],
            subject_hash=payload["subject_hash"],
            trial=payload["trial"],
            trial_hash=payload["trial_hash"],
            loop_id=payload["loop_id"],
            round_index=payload["round_index"],
        )
        if opening.payload() != dict(payload):
            raise ValueError("the window opening does not round-trip")
        return opening


class UnsealingLedger(Protocol):
    def get(self, family_id: str) -> OosUnsealing | None: ...

    def record(self, family_id: str, unsealing: OosUnsealing) -> None: ...

    def count(self) -> int: ...

    def mark_evaluated(self, family_id: str) -> None:
        """Record that the family's one sealed evaluation was handed out (append-only)."""
        ...

    def is_evaluated(self, family_id: str) -> bool: ...


class InMemoryUnsealingLedger:
    """Append-only: a family is recorded once and never removed."""

    def __init__(self) -> None:
        self._records: dict[str, OosUnsealing] = {}
        self._evaluated: set[str] = set()
        self._openings: dict[str, WindowOpening] = {}

    def get(self, family_id: str) -> OosUnsealing | None:
        return self._records.get(family_id)

    def record(self, family_id: str, unsealing: OosUnsealing) -> None:
        if family_id in self._records:
            raise OosAlreadyUnsealed(f"family {family_id!r} has already been unsealed")
        self._records[family_id] = unsealing

    def window_opening(self, window_id: str) -> WindowOpening | None:
        return self._openings.get(window_id)

    def window_openings(self) -> tuple[WindowOpening, ...]:
        return tuple(self._openings.values())

    def open_window(self, opening: WindowOpening) -> None:
        """Record a replacement window's single opening (module docs)."""
        known = self._openings.get(opening.window_id)
        if known is not None:
            raise SealedWindowAlreadyOpened(
                f"window {opening.window_id!r} was already opened for {known.subject}"
            )
        self._openings[opening.window_id] = opening

    def count(self) -> int:
        return len(self._records)

    def mark_evaluated(self, family_id: str) -> None:
        if family_id not in self._records:
            raise SealedOosLocked(f"family {family_id!r} has not unsealed the OOS window")
        if family_id in self._evaluated:
            raise SealedOosAlreadyEvaluated(f"family {family_id!r} already evaluated sealed OOS")
        self._evaluated.add(family_id)

    def is_evaluated(self, family_id: str) -> bool:
        return family_id in self._evaluated


class DurableUnsealingLedger:
    """``UnsealingLedger`` backed by a hash-chained append-only file (debugging pass, 2026-09-25).

    Every ``record`` / ``mark_evaluated`` call is one journal line; replaying the file on open
    restores exactly the state ``InMemoryUnsealingLedger`` would have built from the same calls,
    so a family unsealed (or evaluated) by one process cannot be unsealed (or evaluated) again by
    another that opens the same file. A tampered, truncated or foreign-type file is refused
    (``research.persistence.JournalCorrupted``); nothing is ever rewritten or deleted.
    """

    def __init__(self, path: Path) -> None:
        self._journal = AppendOnlyJournal(path)
        #: Orders a write's check / append / apply against snapshots (taken after the gate).
        self._lock = RLock()
        self._gate: WriteGate | None = None
        self._records: dict[str, OosUnsealing] = {}
        self._evaluated: set[str] = set()
        self._openings: dict[str, WindowOpening] = {}
        for entry in self._journal.entries:
            if entry.type == "unseal":
                family_id = entry.payload["family_id"]
                unsealing = OosUnsealing.model_validate(entry.payload["unsealing"])
                if family_id in self._records:
                    raise JournalCorrupted(
                        f"{path}: family {family_id!r} was unsealed twice in the journal"
                    )
                self._records[family_id] = unsealing
            elif entry.type == "mark_evaluated":
                family_id = entry.payload["family_id"]
                if family_id not in self._records:
                    raise JournalCorrupted(
                        f"{path}: family {family_id!r} evaluated before it was unsealed"
                    )
                if family_id in self._evaluated:
                    raise JournalCorrupted(
                        f"{path}: family {family_id!r} was marked evaluated twice"
                    )
                self._evaluated.add(family_id)
            elif entry.type == REPLACEMENT_WINDOW_OPENED:  # format 2 (module docs)
                try:
                    opening = WindowOpening.from_payload(entry.payload)
                except (KeyError, TypeError, ValueError) as exc:
                    raise JournalCorrupted(
                        f"{path}:{entry.seq} is not a valid replacement window opening: {exc}"
                    ) from exc
                if opening.window_id in self._openings:
                    raise JournalCorrupted(
                        f"{path}: window {opening.window_id!r} was opened twice in the journal"
                    )
                self._openings[opening.window_id] = opening
            else:
                raise JournalCorrupted(f"{path}: unknown record type {entry.type!r}")

    @property
    def path(self) -> Path:
        return self._journal.path

    def bind_write_gate(self, gate: WriteGate) -> None:
        """Run every later ``record`` / ``mark_evaluated`` inside ``gate`` (module docs); once."""
        with self._lock:
            if self._gate is not None:
                raise ValueError("this unsealing ledger is already bound to a write gate")
            self._gate = gate

    def journal_head(self) -> tuple[int, str]:
        """``(entry count, chain head)`` of the backing journal."""
        with self._lock:
            return len(self._journal.entries), self._journal.head_hash

    def journal_snapshot(self) -> JournalSnapshot:
        """Detached read-only copy of the backing journal's verified entries."""
        with self._lock:
            return journal_snapshot(self._journal)

    def get(self, family_id: str) -> OosUnsealing | None:
        return self._records.get(family_id)

    def record(self, family_id: str, unsealing: OosUnsealing) -> None:
        with gate_scope(self._gate, f"unsealing the sealed OOS for {family_id!r}"), self._lock:
            if family_id in self._records:
                raise OosAlreadyUnsealed(f"family {family_id!r} has already been unsealed")
            self._journal.append(
                "unseal",
                {"family_id": family_id, "unsealing": unsealing.model_dump(mode="json")},
            )
            self._records[family_id] = unsealing

    def count(self) -> int:
        return len(self._records)

    def mark_evaluated(self, family_id: str) -> None:
        with gate_scope(self._gate, f"marking {family_id!r} sealed-OOS evaluated"), self._lock:
            if family_id not in self._records:
                raise SealedOosLocked(f"family {family_id!r} has not unsealed the OOS window")
            if family_id in self._evaluated:
                raise SealedOosAlreadyEvaluated(
                    f"family {family_id!r} already evaluated sealed OOS"
                )
            self._journal.append("mark_evaluated", {"family_id": family_id})
            self._evaluated.add(family_id)

    def is_evaluated(self, family_id: str) -> bool:
        return family_id in self._evaluated

    def window_opening(self, window_id: str) -> WindowOpening | None:
        return self._openings.get(window_id)

    def window_openings(self) -> tuple[WindowOpening, ...]:
        return tuple(self._openings.values())

    def open_window(self, opening: WindowOpening) -> None:
        """Journal a replacement window's single opening (module docs), inside the write gate."""
        with (
            gate_scope(self._gate, f"opening the replacement window {opening.window_id!r}"),
            self._lock,
        ):
            known = self._openings.get(opening.window_id)
            if known is not None:
                raise SealedWindowAlreadyOpened(
                    f"window {opening.window_id!r} was already opened for {known.subject}"
                )
            self._journal.append(REPLACEMENT_WINDOW_OPENED, opening.payload())
            self._openings[opening.window_id] = opening


class SealedEvaluation:
    """A family's single sealed OOS evaluation, already recorded as consumed (see module docs).

    Only ``SealedOosVault.claim_evaluation`` creates one. Each part of the window (``"bars"``,
    ``"labels"``) can be taken once; a second take raises ``SealedOosAlreadyEvaluated``.
    """

    PARTS: frozenset[str] = frozenset({"bars", "labels"})

    def __init__(self, family_id: str, window: SealedWindow) -> None:
        self.family_id = family_id
        self.window = window
        self._taken: set[str] = set()

    def take(self, part: str) -> None:
        """Record that ``part`` of the window was handed out (once per claim)."""
        if part not in self.PARTS:
            raise ValueError(f"unknown sealed part {part!r}")
        if part in self._taken:
            raise SealedOosAlreadyEvaluated(
                f"family {self.family_id!r} already took the sealed {part}"
            )
        self._taken.add(part)

    def taken(self, part: str) -> bool:
        return part in self._taken

    def view(self, spans: Sequence[LabeledSpan]) -> tuple[LabeledSpan, ...]:
        """Spans fully inside the window (the ``labels`` part, handed out once)."""
        self.take("labels")
        return tuple(span for span in spans if self.window.contains(span))


#: The Profile field of the sealed OOS unsealing budget (ADR-0052 §2, C-S2).
MAX_UNSEALINGS_FIELD = "data_split.sealed_oos_max_unsealings"


class SealedOosVault:
    """The window of one Profile with a global unsealing budget (see module docs)."""

    def __init__(
        self,
        profile: ValidationProfile,
        ledger: UnsealingLedger | None = None,
        *,
        max_unsealings: int | None = None,
        path: Path | None = None,
    ) -> None:
        """``ledger`` is the framework default; pass ``path`` instead for a durable ledger file.

        ``path`` is additive (debugging pass, 2026-09-25): omit it and behavior is unchanged
        (in-memory, never persisted). Passing both ``ledger`` and ``path`` is ambiguous and
        refused.

        ``max_unsealings`` (ADR-0052 §2): required when the Profile has no
        ``data_split.sealed_oos_max_unsealings``; refused when it has one (the Profile's budget
        applies and cannot be overridden, C-A4).
        """
        if ledger is not None and path is not None:
            raise ValueError("pass either ledger or path, not both")
        budget, source = sourced_parameter(
            profile, MAX_UNSEALINGS_FIELD, max_unsealings, "max_unsealings"
        )
        if budget is None:  # an old Profile: the explicit argument stays required, as before
            raise TypeError(
                "SealedOosVault() missing required keyword argument 'max_unsealings' "
                f"(the Profile has no {MAX_UNSEALINGS_FIELD})"
            )
        if isinstance(budget, bool) or budget < 1:
            raise ValueError("max_unsealings must be a positive int")
        self.window = SealedWindow.from_profile(profile)
        self.max_unsealings: int = budget
        self.budget_source = source
        if ledger is not None:
            self._ledger = ledger
        elif path is not None:
            self._ledger = DurableUnsealingLedger(path)
        else:
            self._ledger = InMemoryUnsealingLedger()

    def research_view(self, spans: Sequence[LabeledSpan]) -> tuple[LabeledSpan, ...]:
        """Spans that do not touch the sealed window (always allowed)."""
        return tuple(span for span in spans if not self.window.touches(span))

    def is_unsealed(self, family_id: str) -> bool:
        return self._ledger.get(family_id) is not None

    def unseal(self, family_id: str, approved_by: str, at: datetime) -> OosUnsealing:
        """Record the family's single unsealing; a second call raises ``OosAlreadyUnsealed``."""
        if not family_id.strip():
            raise ValueError("family_id must not be blank")
        if self._ledger.get(family_id) is not None:
            raise OosAlreadyUnsealed(f"family {family_id!r} has already been unsealed")
        if self._ledger.count() >= self.max_unsealings:
            raise OosBudgetExhausted(
                f"the sealed OOS budget ({self.max_unsealings} unsealings, "
                f"{self.budget_source}) is used up"
            )
        unsealing = OosUnsealing(unsealed_at=at, approved_by=approved_by, family_unseal_count=1)
        self._ledger.record(family_id, unsealing)
        return unsealing

    def is_evaluated(self, family_id: str) -> bool:
        return self._ledger.is_evaluated(family_id)

    def claim_evaluation(self, family_id: str) -> SealedEvaluation:
        """Consume the family's single evaluation **now** and return its one-shot handle."""
        if self._ledger.get(family_id) is None:
            raise SealedOosLocked(f"family {family_id!r} has not unsealed the OOS window")
        if self._ledger.is_evaluated(family_id):
            raise SealedOosAlreadyEvaluated(f"family {family_id!r} already evaluated sealed OOS")
        self._ledger.mark_evaluated(family_id)
        return SealedEvaluation(family_id, self.window)

    def sealed_view(self, family_id: str, spans: Sequence[LabeledSpan]) -> tuple[LabeledSpan, ...]:
        """Spans fully inside the window, handed out **once** after the family's unsealing."""
        return self.claim_evaluation(family_id).view(spans)
