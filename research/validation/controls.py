"""Negative controls for leakage detection (Constitution C-L6; ADR-0037 §3).

A ``SignalStudy`` maps the traded events to sides (``-1`` short, ``0`` flat, ``1`` long). It also
receives the vector of label values aligned with the events — **a compliant study ignores it**;
it is exposed so the controls can catch a study that (wrongly) depends on the outcome, directly
or through a leaky pipeline.

Label blinding (Phase 8 fix, ADR-0041): every side the pipeline **uses** (G0 determinism, G2 / G3
statistics, G5) is computed from ``blind_labels`` — a constant vector carrying no information —
so a study that reads its label argument cannot feed the outcome into the statistics. The real
labels reach ``sides`` only in the leak-detection gate ``G1.label_blind_sides`` (sides with the real
labels must equal the blinded sides) and in the two negative controls below. The first call a
study ever receives is a blinded one, so a study that memoizes its first answer memoizes nothing.
Honest limit: a study whose sides were computed **upstream** from label data (e.g. a
``FixedSides`` filled with ``sign(label)``) never reads the argument and cannot be detected here;
the defence is provenance — the strategy adapter builds ``FixedSides`` only from contract-checked
``TargetPosition`` rows (``latest_input_available_time <= decision_time``, signal kinds only).

``FittableStudy`` is the optional extension for a study with fitted parameters: the pipeline fits
it per walk-forward fold on the **purged and embargoed** training labels only and evaluates it on
that fold's test events (``pipeline.run_in_sample``).

Each control rebuilds the label vector with the alignment destroyed, re-runs the study on it and
tests the timing statistic ``side_i * (y_i - mean(y))`` (the covariance of side and outcome, so a
constant side has no effect) with the same HAC test as G3:

- **shuffle**: a seeded random permutation of the labels;
- **shift**: a seeded circular shift by at least ``lag + 1`` positions (keeps the autocorrelation
  of each series, breaks their alignment).

The effect must disappear: the gate compares the control's p-value with the Profile threshold.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from core.domain.base import Ref
from research.validation.stats import hac_t_test

__all__ = [
    "ControlResult",
    "FittableStudy",
    "FixedSides",
    "SignalStudy",
    "blind_labels",
    "shift_control",
    "shuffle_control",
    "timing_p_value",
]


class SignalStudy(Protocol):
    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        """The declared signal inputs (Feature / State / Event refs; never an Outcome)."""
        ...

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        """One side per event; must not depend on ``label_values``."""
        ...


@runtime_checkable
class FittableStudy(Protocol):
    """A study with fitted parameters: ``fit`` sees only one fold's purged training labels."""

    @property
    def signal_refs(self) -> tuple[Ref, ...]: ...

    def sides(
        self, event_keys: Sequence[str], label_values: Sequence[Decimal]
    ) -> tuple[int, ...]: ...

    def fit(self, train_keys: Sequence[str], train_values: Sequence[Decimal]) -> SignalStudy:
        """A study fitted on the given training events (the fold's test events are not given)."""
        ...


def blind_labels(n: int) -> tuple[Decimal, ...]:
    """A label vector of ``n`` zeros: it carries no information about the outcome."""
    return tuple(Decimal(0) for _ in range(n))


@dataclass(frozen=True)
class FixedSides:
    """A study whose sides were computed upstream from its signals (keyed by event)."""

    refs: tuple[Ref, ...]
    by_event: dict[str, int]

    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return self.refs

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        return tuple(self.by_event.get(key, 0) for key in event_keys)


def _check_sides(sides: Sequence[int], n: int) -> None:
    if len(sides) != n or any(side not in (-1, 0, 1) for side in sides):
        raise ValueError("a study must return one side in {-1, 0, 1} per event")


def timing_p_value(sides: Sequence[int], values: Sequence[Decimal], lag: int) -> float:
    """Two-sided HAC p-value of ``side * (y - mean(y))``; 1.0 when there is nothing to test."""
    _check_sides(sides, len(values))
    ys = [float(value) for value in values]
    if len(ys) < 2:
        return 1.0
    mean = sum(ys) / len(ys)
    series = [side * (y - mean) for side, y in zip(sides, ys, strict=True)]
    if all(item == 0.0 for item in series):
        return 1.0
    return hac_t_test(series, lag).p_two_sided


@dataclass(frozen=True)
class ControlResult:
    name: str
    p_value: float
    applicable: bool


def shuffle_control(
    study: SignalStudy,
    event_keys: Sequence[str],
    values: Sequence[Decimal],
    lag: int,
    seed: int,
) -> ControlResult:
    rng = random.Random(seed)
    permuted = list(values)
    rng.shuffle(permuted)
    sides = study.sides(tuple(event_keys), tuple(permuted))
    return ControlResult("shuffle", timing_p_value(sides, permuted, lag), applicable=True)


def shift_control(
    study: SignalStudy,
    event_keys: Sequence[str],
    values: Sequence[Decimal],
    lag: int,
    seed: int,
) -> ControlResult:
    n = len(values)
    low, high = lag + 1, n - lag - 1
    if high < low:
        return ControlResult("shift", 1.0, applicable=False)
    offset = random.Random(seed).randint(low, high)
    shifted = list(values[offset:]) + list(values[:offset])
    sides = study.sides(tuple(event_keys), tuple(shifted))
    return ControlResult("shift", timing_p_value(sides, shifted, lag), applicable=True)
