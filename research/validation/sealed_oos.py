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

Replacement windows (ADR-0100 item 7, 2026-09-30; additive; journal format 3 since the
2026-09-30 integrity fixes): the research loop's optional replacement proposal trigger
(``research.loop.replacement``) may only evaluate on an **independent** sealed window that has
**never been opened**. Constitution C-S3 fixes every sealed boundary in a Profile and allows
rotation only by publishing a new Profile version, so a replacement window is exactly the sealed
OOS window of one published Profile version: ``RegisteredSealedWindow`` is only a reference to that
version (Profile ref + content hash) and the bounds its ``data_split`` defines
(``RegisteredSealedWindow.of(profile)``); that the version is published and frozen (ADR-0062
freeze record) is checked by the trigger against the ``ProfileFreezeRegistry``. Opening one is
recorded in the same unsealing ledger as a ``replacement_window_opened`` line (``WindowOpening``:
window id, registration hash, the subject it was opened for, the trial that counts it, the loop,
round and the round's scheduled time ``opened_at``); a window opens **once**, for one subject, and
is never reusable — not by the same subject, not by its descendants, not by any other subject
(``SealedWindowAlreadyOpened``). The evidence evaluated under that opening is bound to it by a
single ``replacement_window_consumed`` line (``WindowConsumption``: window id, opening hash, the
claimed report hashes, subject, round): one consumption per window, only for its opening's
subject, in a later round, and a report consumed for one window can never be consumed again
(``SealedWindowAlreadyConsumed``).

**Budget (C-S2).** Every opening is audited (the ledger line) **and counted toward the same global
unsealing budget as the family unsealings**: ``count()`` is the number of family unsealings plus
the number of window openings, so a ``SealedOosVault.unseal`` after an opening sees it, and
``open_window(..., max_unsealings=)`` refuses (``OosBudgetExhausted``) before writing when the
count has reached the budget. Journal format: format 1 is the ``unseal`` / ``mark_evaluated`` lines
(unchanged bytes); format 3 adds ``replacement_window_opened`` and ``replacement_window_consumed``,
whose payloads carry ``"format_version": 3``. The format-2 opening line of the first
implementation (no ``opened_at``) is refused on replay, like any unknown shape (fail closed); a
ledger without openings is byte-identical to before.
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
from core.domain.base import SHA256_PATTERN, Kind, Ref, content_hash
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
    "REPLACEMENT_WINDOW_CONSUMED",
    "REPLACEMENT_WINDOW_OPENED",
    "UNSEALING_LEDGER_FORMAT",
    "DurableUnsealingLedger",
    "InMemoryUnsealingLedger",
    "OosAlreadyUnsealed",
    "OosBudgetExhausted",
    "RegisteredSealedWindow",
    "SealedWindowAlreadyConsumed",
    "SealedWindowAlreadyOpened",
    "WindowConsumption",
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
    """A replacement window was opened before (single use; module docs)."""


class SealedWindowAlreadyConsumed(Exception):
    """A replacement window's evidence (or one of its reports) was consumed before (module docs)."""


#: Unsealing ledger journal format of the replacement-window lines (module docs).
UNSEALING_LEDGER_FORMAT: Final = 3
#: The line type of one replacement window opening.
REPLACEMENT_WINDOW_OPENED: Final = "replacement_window_opened"
#: The line type of the single consumption of a replacement window's evidence.
REPLACEMENT_WINDOW_CONSUMED: Final = "replacement_window_consumed"
_SHA256: Final = re.compile(SHA256_PATTERN)
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
        "opened_at",
    }
)
_CONSUMPTION_FIELDS: Final = frozenset(
    {
        "format_version",
        "window_id",
        "opening_hash",
        "subject",
        "report_hashes",
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
    """A replacement window: the sealed OOS window of one published Profile version (module docs,
    C-S3). Only a reference — ``profile_ref`` + ``profile_hash`` — and the ``[start, end)`` bounds
    that Profile's ``data_split`` defines; build it with ``of(profile)``. Whether that version is
    published and frozen is the caller's check against the ADR-0062 freeze registry.
    ``registration_hash`` covers the whole reference."""

    profile_ref: Ref
    profile_hash: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.profile_ref, Ref) or self.profile_ref.kind is not Kind.PROFILE:
            raise ValueError("a registered window references a Profile (kind profile)")
        if not isinstance(self.profile_hash, str) or _SHA256.fullmatch(self.profile_hash) is None:
            raise ValueError("profile_hash must be a lowercase SHA-256 content hash")
        for name in ("start", "end"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.end <= self.start:
            raise ValueError(f"window {self.window_id!r} must end after it starts")

    @classmethod
    def of(cls, profile: ValidationProfile) -> RegisteredSealedWindow:
        """The sealed OOS window of ``profile`` (its fixed boundary and length, C-S3)."""
        sealed = SealedWindow.from_profile(profile)
        return cls(
            profile_ref=profile.ref,
            profile_hash=profile.content_hash(),
            start=sealed.start,
            end=sealed.end,
        )

    @property
    def window_id(self) -> str:
        """The Profile version's ref (one window per published version)."""
        return str(self.profile_ref)

    @property
    def window(self) -> SealedWindow:
        return SealedWindow(start=self.start, end=self.end)

    def describes(self, profile: ValidationProfile) -> bool:
        """``profile`` is exactly the referenced version (ref + hash) and defines these bounds."""
        return (
            profile.ref.target_identity() == self.profile_ref.target_identity()
            and profile.content_hash() == self.profile_hash
            and self.same_bounds(SealedWindow.from_profile(profile))
        )

    def payload(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "profile_ref": str(self.profile_ref),
            "profile_hash": self.profile_hash,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }

    def registration_hash(self) -> str:
        return content_hash(
            {
                "kind": "registered_sealed_window",
                "format_version": UNSEALING_LEDGER_FORMAT,
                **self.payload(),
            }
        )

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
    #: the opening round's scheduled time (the loop's clock; UTC)
    opened_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", _aware(self.opened_at, "opened_at"))
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
            "opened_at": self.opened_at.isoformat(),
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
            opened_at=datetime.fromisoformat(payload["opened_at"]),
        )
        if opening.payload() != dict(payload):
            raise ValueError("the window opening does not round-trip")
        return opening


@dataclass(frozen=True)
class WindowConsumption:
    """The single consumption of a replacement window's evidence (module docs; the ledger's
    ``replacement_window_consumed`` line): the reports evaluated under that window's opening.
    ``consumption_hash`` covers the whole payload."""

    window_id: str
    opening_hash: str
    subject: str
    report_hashes: tuple[str, ...]
    round_index: int

    def __post_init__(self) -> None:
        for name in ("window_id", "opening_hash", "subject"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a window consumption needs a non-empty {name}")
        hashes = self.report_hashes
        if (
            not isinstance(hashes, tuple)
            or not hashes
            or not all(isinstance(h, str) and _SHA256.fullmatch(h) for h in hashes)
            or list(hashes) != sorted(set(hashes))
        ):
            raise ValueError("report_hashes must be a non-empty sorted tuple of distinct SHA-256")
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
            "opening_hash": self.opening_hash,
            "subject": self.subject,
            "report_hashes": list(self.report_hashes),
            "round_index": self.round_index,
        }

    def consumption_hash(self) -> str:
        return content_hash({"kind": REPLACEMENT_WINDOW_CONSUMED, **self.payload()})

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WindowConsumption:
        """Rebuild one recorded consumption; any other shape or format is refused."""
        if set(payload) != _CONSUMPTION_FIELDS:
            raise ValueError("a window consumption has exactly the recorded fields")
        if payload["format_version"] != UNSEALING_LEDGER_FORMAT:
            raise ValueError(f"unknown window consumption format {payload['format_version']!r}")
        consumption = cls(
            window_id=payload["window_id"],
            opening_hash=payload["opening_hash"],
            subject=payload["subject"],
            report_hashes=tuple(payload["report_hashes"]),
            round_index=payload["round_index"],
        )
        if consumption.payload() != dict(payload):
            raise ValueError("the window consumption does not round-trip")
        return consumption


def _check_consumption(
    consumption: WindowConsumption,
    openings: Mapping[str, WindowOpening],
    consumptions: Mapping[str, WindowConsumption],
) -> None:
    """The one rule set of a consumption, on write and on replay (``SealedWindowAlreadyConsumed``
    for a reuse, ``ValueError`` for a consumption not bound to its window's opening)."""
    opening = openings.get(consumption.window_id)
    if opening is None:
        raise ValueError(f"window {consumption.window_id!r} was never opened")
    if opening.opening_hash() != consumption.opening_hash or opening.subject != consumption.subject:
        raise ValueError(
            f"the consumption of window {consumption.window_id!r} is not bound to its opening "
            f"(opened for {opening.subject})"
        )
    if consumption.round_index <= opening.round_index:
        raise ValueError("a window's evidence is consumed in a round after its opening")
    known = consumptions.get(consumption.window_id)
    if known is not None:
        raise SealedWindowAlreadyConsumed(
            f"window {consumption.window_id!r} was already consumed in round {known.round_index}"
        )
    used = {h for c in consumptions.values() for h in c.report_hashes}
    reused = sorted(used.intersection(consumption.report_hashes))
    if reused:
        raise SealedWindowAlreadyConsumed(f"reports {reused} were consumed for another window")


def _check_budget(count: int, max_unsealings: int) -> None:
    if (
        isinstance(max_unsealings, bool)
        or not isinstance(max_unsealings, int)
        or max_unsealings < 1
    ):
        raise ValueError("max_unsealings must be a positive int")
    if count >= max_unsealings:
        raise OosBudgetExhausted(
            f"the global sealed OOS unsealing budget ({max_unsealings}) is used up: {count} "
            "unsealings and replacement window openings are recorded"
        )


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
        self._consumptions: dict[str, WindowConsumption] = {}

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

    def open_window(self, opening: WindowOpening, *, max_unsealings: int) -> None:
        """Record a replacement window's single opening, counted in the budget (module docs)."""
        known = self._openings.get(opening.window_id)
        if known is not None:
            raise SealedWindowAlreadyOpened(
                f"window {opening.window_id!r} was already opened for {known.subject}"
            )
        _check_budget(self.count(), max_unsealings)
        self._openings[opening.window_id] = opening

    def window_consumption(self, window_id: str) -> WindowConsumption | None:
        return self._consumptions.get(window_id)

    def window_consumptions(self) -> tuple[WindowConsumption, ...]:
        return tuple(self._consumptions.values())

    def consume_window(self, consumption: WindowConsumption) -> None:
        """Record a replacement window's single evidence consumption (module docs)."""
        _check_consumption(consumption, self._openings, self._consumptions)
        self._consumptions[consumption.window_id] = consumption

    def count(self) -> int:
        """Family unsealings plus replacement window openings: one global budget (C-S2)."""
        return len(self._records) + len(self._openings)

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
        self._consumptions: dict[str, WindowConsumption] = {}
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
            elif entry.type == REPLACEMENT_WINDOW_OPENED:  # format 3 (module docs)
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
            elif entry.type == REPLACEMENT_WINDOW_CONSUMED:  # format 3 (module docs)
                try:
                    consumption = WindowConsumption.from_payload(entry.payload)
                    _check_consumption(consumption, self._openings, self._consumptions)
                except (KeyError, TypeError, ValueError, SealedWindowAlreadyConsumed) as exc:
                    raise JournalCorrupted(
                        f"{path}:{entry.seq} is not a valid replacement window consumption: {exc}"
                    ) from exc
                self._consumptions[consumption.window_id] = consumption
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
        """Family unsealings plus replacement window openings: one global budget (C-S2)."""
        return len(self._records) + len(self._openings)

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

    def open_window(self, opening: WindowOpening, *, max_unsealings: int) -> None:
        """Journal a replacement window's single opening (module docs), inside the write gate;
        refused before writing when the global unsealing budget is used up (C-S2)."""
        with (
            gate_scope(self._gate, f"opening the replacement window {opening.window_id!r}"),
            self._lock,
        ):
            known = self._openings.get(opening.window_id)
            if known is not None:
                raise SealedWindowAlreadyOpened(
                    f"window {opening.window_id!r} was already opened for {known.subject}"
                )
            _check_budget(self.count(), max_unsealings)
            self._journal.append(REPLACEMENT_WINDOW_OPENED, opening.payload())
            self._openings[opening.window_id] = opening

    def window_consumption(self, window_id: str) -> WindowConsumption | None:
        return self._consumptions.get(window_id)

    def window_consumptions(self) -> tuple[WindowConsumption, ...]:
        return tuple(self._consumptions.values())

    def consume_window(self, consumption: WindowConsumption) -> None:
        """Journal a replacement window's single evidence consumption, inside the write gate."""
        with (
            gate_scope(self._gate, f"consuming the replacement window {consumption.window_id!r}"),
            self._lock,
        ):
            _check_consumption(consumption, self._openings, self._consumptions)
            self._journal.append(REPLACEMENT_WINDOW_CONSUMED, consumption.payload())
            self._consumptions[consumption.window_id] = consumption


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
