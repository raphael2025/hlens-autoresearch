"""Purged and embargoed time-series splits (Constitution C-L5, C-S1, C-S4; ADR-0037 §3).

A sample is a ``LabeledSpan``: ``start`` is the event (decision) time and ``end`` is the time its
label becomes known (``OutcomeLabel.available_time``). Two samples whose spans overlap share
information through the label window.

- **purging**: a training sample whose span overlaps the test span
  ``[min test start, max test end]`` is removed from training;
- **embargo**: a training sample that starts within ``embargo`` after the end of the test span is
  removed as well (serial correlation right after the test period);
- samples whose span reaches the sealed OOS window never enter a research split.

``walk_forward_folds`` takes its windows (train / test / step), the embargo and the research and
sealed boundaries from the Profile; ``purged_k_fold`` takes an explicit fold count and embargo
(no defaults).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from core.contracts.validation_profile import ValidationProfile

__all__ = [
    "Fold",
    "LabeledSpan",
    "midnight_utc",
    "purge_and_embargo",
    "purged_k_fold",
    "research_spans",
    "walk_forward_folds",
]


def midnight_utc(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


@dataclass(frozen=True)
class LabeledSpan:
    key: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("span times must be timezone-aware UTC")
        if self.end < self.start:
            raise ValueError("a span cannot end before it starts")


@dataclass(frozen=True)
class Fold:
    index: int
    train: tuple[str, ...]
    test: tuple[str, ...]
    purged: tuple[str, ...]
    embargoed: tuple[str, ...]


def purge_and_embargo(
    train: Sequence[LabeledSpan], test: Sequence[LabeledSpan], embargo: timedelta
) -> tuple[tuple[LabeledSpan, ...], tuple[LabeledSpan, ...], tuple[LabeledSpan, ...]]:
    """``(kept, purged, embargoed)`` training samples for one test set."""
    if embargo < timedelta(0):
        raise ValueError("embargo must be >= 0")
    if not test:
        return tuple(train), (), ()
    test_start = min(span.start for span in test)
    test_end = max(span.end for span in test)
    kept: list[LabeledSpan] = []
    purged: list[LabeledSpan] = []
    embargoed: list[LabeledSpan] = []
    for span in train:
        if span.start <= test_end and span.end >= test_start:
            purged.append(span)
        elif test_end < span.start < test_end + embargo:
            embargoed.append(span)
        else:
            kept.append(span)
    return tuple(kept), tuple(purged), tuple(embargoed)


def research_spans(
    spans: Sequence[LabeledSpan], profile: ValidationProfile
) -> tuple[LabeledSpan, ...]:
    """Spans inside the research window whose label is known before the sealed OOS boundary."""
    start = midnight_utc(profile.data_split.research_window_start)
    boundary = midnight_utc(profile.data_split.sealed_oos_boundary)
    return tuple(span for span in spans if span.start >= start and span.end < boundary)


def _fold(
    index: int,
    train: Sequence[LabeledSpan],
    test: Sequence[LabeledSpan],
    embargo: timedelta,
) -> Fold:
    kept, purged, embargoed = purge_and_embargo(train, test, embargo)
    return Fold(
        index=index,
        train=tuple(span.key for span in kept),
        test=tuple(span.key for span in test),
        purged=tuple(span.key for span in purged),
        embargoed=tuple(span.key for span in embargoed),
    )


def walk_forward_folds(spans: Sequence[LabeledSpan], profile: ValidationProfile) -> list[Fold]:
    """Anchored-step walk-forward over the research window, purged and embargoed."""
    split = profile.data_split
    wf = split.walk_forward
    usable = sorted(research_spans(spans, profile), key=lambda span: (span.start, span.key))
    boundary = midnight_utc(split.sealed_oos_boundary)
    folds: list[Fold] = []
    train_start = midnight_utc(split.research_window_start)
    while True:
        train_end = train_start + wf.train_window
        test_end = train_end + wf.test_window
        if test_end > boundary:
            break
        train = [span for span in usable if train_start <= span.start < train_end]
        test = [span for span in usable if train_end <= span.start < test_end]
        if test:
            folds.append(_fold(len(folds), train, test, split.embargo))
        train_start += wf.step
    return folds


def purged_k_fold(spans: Sequence[LabeledSpan], n_folds: int, embargo: timedelta) -> list[Fold]:
    """Contiguous k-fold in time order; training uses both sides of the test fold."""
    if n_folds < 2:
        raise ValueError("n_folds must be >= 2")
    ordered = sorted(spans, key=lambda span: (span.start, span.key))
    if len(ordered) < n_folds:
        raise ValueError("fewer samples than folds")
    size, extra = divmod(len(ordered), n_folds)
    folds: list[Fold] = []
    begin = 0
    for index in range(n_folds):
        end = begin + size + (1 if index < extra else 0)
        test = ordered[begin:end]
        train = ordered[:begin] + ordered[end:]
        folds.append(_fold(index, train, test, embargo))
        begin = end
    return folds
