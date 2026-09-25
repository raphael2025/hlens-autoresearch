"""Sealed OOS: a fixed-date window that each hypothesis family may unseal exactly once.

Constitution C-S1 ~ C-S3 and 07-validation.md §3: the window starts at the Profile's
``data_split.sealed_oos_boundary`` (UTC midnight) and lasts ``sealed_oos_length``; it does not roll
with the run time. Before an unsealing is recorded, the vault refuses to hand out any sample of the
window (``SealedOosLocked``). ``unseal`` records an ``OosUnsealing`` in an append-only ledger and
refuses a second unsealing of the same family (``OosAlreadyUnsealed``); the record is never
removed. The ledger also gives the global unsealing count (C-S2: counted in a global budget).

``InMemoryUnsealingLedger`` is the framework ledger; a durable Control Plane ledger implements the
same ``UnsealingLedger`` Protocol later (not in this batch).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.contracts.profile_selection import OosUnsealing
from core.contracts.validation_profile import ValidationProfile
from research.validation.splits import LabeledSpan, midnight_utc

__all__ = [
    "InMemoryUnsealingLedger",
    "OosAlreadyUnsealed",
    "SealedOosLocked",
    "SealedOosVault",
    "SealedWindow",
    "UnsealingLedger",
]


class OosAlreadyUnsealed(Exception):
    """The family has already used its one unsealing."""


class SealedOosLocked(Exception):
    """Sealed OOS data was requested without a recorded unsealing."""


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


class InMemoryUnsealingLedger:
    """Append-only: a family is recorded once and never removed."""

    def __init__(self) -> None:
        self._records: dict[str, OosUnsealing] = {}

    def get(self, family_id: str) -> OosUnsealing | None:
        return self._records.get(family_id)

    def record(self, family_id: str, unsealing: OosUnsealing) -> None:
        if family_id in self._records:
            raise OosAlreadyUnsealed(f"family {family_id!r} has already been unsealed")
        self._records[family_id] = unsealing

    def count(self) -> int:
        return len(self._records)


class SealedOosVault:
    def __init__(self, profile: ValidationProfile, ledger: UnsealingLedger) -> None:
        self.window = SealedWindow.from_profile(profile)
        self._ledger = ledger

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
        unsealing = OosUnsealing(unsealed_at=at, approved_by=approved_by, family_unseal_count=1)
        self._ledger.record(family_id, unsealing)
        return unsealing

    def sealed_view(self, family_id: str, spans: Sequence[LabeledSpan]) -> tuple[LabeledSpan, ...]:
        """Spans fully inside the window; only after the family's unsealing was recorded."""
        if self._ledger.get(family_id) is None:
            raise SealedOosLocked(f"family {family_id!r} has not unsealed the OOS window")
        return tuple(span for span in spans if self.window.contains(span))
