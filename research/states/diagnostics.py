"""State stability diagnostics (Phase 2; research only, never production — H5).

Given one state series (``StateResult`` or ``(evaluation_time, label | None)`` pairs in time order),
report what the roadmap asks for: the state distribution, run durations, the transition matrix and a
label-flicker metric. The functions describe a series; they hold no threshold that decides whether a
state is "good" — any such cut belongs to a ValidationProfile, and the flicker ``min_run`` is a
parameter of the report, stated by the caller.

- a *run* is a maximal stretch of consecutive equal non-``None`` labels; ``None`` (not computable)
  ends a run and is counted separately, never folded into a state;
- durations are in evaluation steps (number of consecutive evaluation times), plus wall time
  (first to last evaluation time of the run) when times are given;
- transitions are counted between consecutive computable labels only (a ``None`` between two labels
  breaks the chain), including self-transitions; row probabilities are exact ``Decimal`` fractions
  quantized to ``PROBABILITY_PLACES`` places;
- flicker: the share of runs shorter than ``min_run`` steps, and the switch rate (label changes per
  computable adjacent pair).

Pure and deterministic: no clock, no randomness, exact arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Final

from core.contracts.state import StateResult

__all__ = [
    "PROBABILITY_PLACES",
    "StateDiagnostics",
    "StateRun",
    "diagnose",
    "labels_of",
    "render_markdown",
    "runs_of",
    "transition_counts",
]

#: Decimal places of reported shares / probabilities.
PROBABILITY_PLACES: Final = 6
_QUANTUM: Final = Decimal(1).scaleb(-PROBABILITY_PLACES)

type Labelled = tuple[datetime, str | None]


@dataclass(frozen=True)
class StateRun:
    """One maximal run of a label."""

    state: str
    start: datetime
    end: datetime
    steps: int

    @property
    def wall_time(self) -> timedelta:
        return self.end - self.start


@dataclass(frozen=True)
class StateDiagnostics:
    """The stability report of one state series (see module docs)."""

    state_space: tuple[str, ...]
    evaluations: int
    not_computable: int
    counts: dict[str, int]
    shares: dict[str, Decimal | None]
    runs: tuple[StateRun, ...]
    mean_steps: dict[str, Decimal | None]
    max_steps: dict[str, int]
    transitions: dict[str, dict[str, int]]
    transition_probabilities: dict[str, dict[str, Decimal | None]]
    min_run: int
    short_run_share: Decimal | None
    switch_rate: Decimal | None


def _share(part: int, whole: int) -> Decimal | None:
    if whole == 0:
        return None
    return (Decimal(part) / Decimal(whole)).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def labels_of(result: StateResult) -> tuple[Labelled, ...]:
    """The ``(evaluation_time, label)`` series of a ``StateResult``."""
    return tuple((item.evaluation_time, item.state) for item in result.values)


def _checked(series: Iterable[Labelled], state_space: Sequence[str]) -> tuple[Labelled, ...]:
    items = tuple(series)
    if any(later[0] <= earlier[0] for earlier, later in pairwise(items)):
        raise ValueError("the series must be in strictly ascending time order")
    known = set(state_space)
    stray = sorted({label for _, label in items if label is not None and label not in known})
    if stray:
        raise ValueError(f"labels outside the state space: {stray}")
    return items


def runs_of(series: Iterable[Labelled]) -> tuple[StateRun, ...]:
    """Maximal runs of equal non-``None`` labels (``None`` ends a run)."""
    runs: list[StateRun] = []
    current: StateRun | None = None
    for at, label in series:
        if label is None:
            if current is not None:
                runs.append(current)
            current = None
        elif current is not None and current.state == label:
            current = StateRun(label, current.start, at, current.steps + 1)
        else:
            if current is not None:
                runs.append(current)
            current = StateRun(label, at, at, 1)
    if current is not None:
        runs.append(current)
    return tuple(runs)


def transition_counts(
    series: Iterable[Labelled], state_space: Sequence[str]
) -> dict[str, dict[str, int]]:
    """``counts[from][to]`` over consecutive computable labels (self-transitions included)."""
    counts = {a: dict.fromkeys(state_space, 0) for a in state_space}
    for (_, a), (_, b) in pairwise(series):
        if a is not None and b is not None:
            counts[a][b] += 1
    return counts


def diagnose(
    series: Iterable[Labelled] | StateResult, state_space: Sequence[str], *, min_run: int
) -> StateDiagnostics:
    """The stability report of ``series``; ``min_run`` (steps) is the caller's flicker parameter."""
    if isinstance(min_run, bool) or not isinstance(min_run, int) or min_run < 1:
        raise ValueError("min_run must be a positive int")
    space = tuple(state_space)
    if len(set(space)) != len(space) or not space:
        raise ValueError("state_space must be non-empty and unique")
    items = _checked(labels_of(series) if isinstance(series, StateResult) else series, space)
    computable = [label for _, label in items if label is not None]
    counts = {label: computable.count(label) for label in space}
    runs = runs_of(items)
    transitions = transition_counts(items, space)
    by_state = {label: [run.steps for run in runs if run.state == label] for label in space}
    pairs = [(a, b) for (_, a), (_, b) in pairwise(items) if a is not None and b is not None]
    return StateDiagnostics(
        state_space=space,
        evaluations=len(items),
        not_computable=len(items) - len(computable),
        counts=counts,
        shares={label: _share(n, len(computable)) for label, n in counts.items()},
        runs=runs,
        mean_steps={
            label: _share(sum(steps), len(steps)) if steps else None
            for label, steps in by_state.items()
        },
        max_steps={label: max(steps, default=0) for label, steps in by_state.items()},
        transitions=transitions,
        transition_probabilities={
            a: {b: _share(n, sum(row.values())) for b, n in row.items()}
            for a, row in transitions.items()
        },
        min_run=min_run,
        short_run_share=_share(sum(1 for run in runs if run.steps < min_run), len(runs)),
        switch_rate=_share(sum(1 for a, b in pairs if a != b), len(pairs)),
    )


def _cell(value: object) -> str:
    return "—" if value is None else str(value)


def render_markdown(report: StateDiagnostics) -> str:
    """A human-readable Markdown report (distribution, durations, transitions, flicker)."""
    lines = [
        f"evaluations: {report.evaluations}; not computable: {report.not_computable}",
        "",
        "| state | count | share | runs | mean steps | max steps |",
        "|---|---|---|---|---|---|",
    ]
    for label in report.state_space:
        runs = sum(1 for run in report.runs if run.state == label)
        lines.append(
            f"| {label} | {report.counts[label]} | {_cell(report.shares[label])} | {runs} | "
            f"{_cell(report.mean_steps[label])} | {report.max_steps[label]} |"
        )
    lines += ["", "| from \\ to | " + " | ".join(report.state_space) + " |"]
    lines.append("|---" * (len(report.state_space) + 1) + "|")
    for a in report.state_space:
        row = report.transition_probabilities[a]
        lines.append(f"| {a} | " + " | ".join(_cell(row[b]) for b in report.state_space) + " |")
    lines += [
        "",
        f"flicker: runs shorter than {report.min_run} steps = {_cell(report.short_run_share)}; "
        f"switch rate = {_cell(report.switch_rate)}",
    ]
    return "\n".join(lines) + "\n"
