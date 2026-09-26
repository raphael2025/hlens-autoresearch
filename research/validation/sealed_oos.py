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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from core.contracts.profile_selection import OosUnsealing
from core.contracts.validation_profile import ValidationProfile
from research.persistence import AppendOnlyJournal, JournalCorrupted
from research.validation.gates import sourced_parameter
from research.validation.splits import LabeledSpan, midnight_utc

__all__ = [
    "DurableUnsealingLedger",
    "InMemoryUnsealingLedger",
    "OosAlreadyUnsealed",
    "OosBudgetExhausted",
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

    def get(self, family_id: str) -> OosUnsealing | None:
        return self._records.get(family_id)

    def record(self, family_id: str, unsealing: OosUnsealing) -> None:
        if family_id in self._records:
            raise OosAlreadyUnsealed(f"family {family_id!r} has already been unsealed")
        self._records[family_id] = unsealing

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
        self._records: dict[str, OosUnsealing] = {}
        self._evaluated: set[str] = set()
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
            else:
                raise JournalCorrupted(f"{path}: unknown record type {entry.type!r}")

    @property
    def path(self) -> Path:
        return self._journal.path

    @property
    def journal(self) -> AppendOnlyJournal:
        """The backing journal (read-only use: its entries and head, for cross-file checks)."""
        return self._journal

    def get(self, family_id: str) -> OosUnsealing | None:
        return self._records.get(family_id)

    def record(self, family_id: str, unsealing: OosUnsealing) -> None:
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
        if family_id not in self._records:
            raise SealedOosLocked(f"family {family_id!r} has not unsealed the OOS window")
        if family_id in self._evaluated:
            raise SealedOosAlreadyEvaluated(f"family {family_id!r} already evaluated sealed OOS")
        self._journal.append("mark_evaluated", {"family_id": family_id})
        self._evaluated.add(family_id)

    def is_evaluated(self, family_id: str) -> bool:
        return family_id in self._evaluated


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
