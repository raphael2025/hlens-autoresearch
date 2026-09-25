"""Outcome materialization: a checked, label-only table (Phase 4, ADR-0037).

``materialize`` runs one ``OutcomeProvider`` over one request, checks the answers against the
request (``OutcomeResult.check_answers``) and returns an ``OutcomeTable``. The table only hands out
labels, and only those already **known** at a given time (``available_time <= t``): a label is
never visible before its window has closed and its last bar is available.

The table deliberately has no way to produce a ``FeatureObservation`` or any other input DTO:
Outcomes are labels, never inputs (Constitution C-L2). ``rows()`` is a JSON-ready export for a
later Iceberg ``outcome`` table (Decimal values as strings); persistence is not part of this batch.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from core.contracts.outcome import (
    OutcomeLabel,
    OutcomeProvider,
    OutcomeRequest,
    OutcomeResult,
)
from core.domain.base import Ref

__all__ = ["OutcomeTable", "materialize"]


@dataclass(frozen=True)
class OutcomeTable:
    """The labels of one ``OutcomeResult``, in event order, keyed by ``event_key``."""

    outcome: Ref
    label_spec_hash: str
    provider: str
    result_hash: str
    labels: tuple[OutcomeLabel, ...]

    def __post_init__(self) -> None:
        keys = [label.event_key for label in self.labels]
        if len(set(keys)) != len(keys):
            raise ValueError("an outcome table holds one label per event_key")

    def __iter__(self) -> Iterator[OutcomeLabel]:
        return iter(self.labels)

    def __len__(self) -> int:
        return len(self.labels)

    def get(self, event_key: str) -> OutcomeLabel | None:
        return next((label for label in self.labels if label.event_key == event_key), None)

    def computable(self) -> tuple[OutcomeLabel, ...]:
        """Labels with a value (the ``None`` ones are explicit gaps, never filled)."""
        return tuple(label for label in self.labels if label.value is not None)

    def known_as_of(self, t: datetime) -> tuple[OutcomeLabel, ...]:
        """Computable labels whose ``available_time <= t`` (a label is unknowable before)."""
        if t.tzinfo is None:
            raise ValueError("t must be timezone-aware UTC")
        return tuple(
            label
            for label in self.labels
            if label.available_time is not None and label.available_time <= t
        )

    def rows(self) -> list[dict[str, object]]:
        """JSON-ready rows (one per event) for a later ``outcome`` table export."""
        head = {
            "outcome": str(self.outcome),
            "label_spec_hash": self.label_spec_hash,
            "provider": self.provider,
            "result_hash": self.result_hash,
        }
        return [{**head, **label.model_dump(mode="json")} for label in self.labels]


def materialize(provider: OutcomeProvider, request: OutcomeRequest) -> OutcomeTable:
    """Compute, check against the request and materialize one outcome result."""
    result = provider.compute(request)
    if type(result) is not OutcomeResult:
        raise TypeError("an OutcomeProvider must return an OutcomeResult")
    result.check_answers(request, provider.descriptor)
    return OutcomeTable(
        outcome=request.label_spec.outcome,
        label_spec_hash=request.label_spec.content_hash(),
        provider=result.provider,
        result_hash=result.result_hash,
        labels=result.labels,
    )
