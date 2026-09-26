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

Persistence (code completion, 2026-09-26): ``StateDiagnostics.to_payload()`` is a deterministic
JSON-ready form (``Decimal`` as its exact text, times as ISO-8601 UTC, per-state maps with sorted
keys; ``state_space`` and ``runs`` keep their semantic order) and ``diagnostics_hash`` its content
hash. ``StateDiagnostics.from_payload`` rebuilds the report and refuses a payload that is not
exactly the canonical form of what it rebuilds (or whose hash differs from ``expected_hash``).
The report describes exactly the series it was given; it holds no window of its own, so a later
change to evaluations after the series' last time cannot alter an already computed report.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Final

from core.contracts.state import StateResult
from core.domain.base import content_hash

__all__ = [
    "PAYLOAD_KIND",
    "PAYLOAD_SCHEMA_VERSION",
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
#: ``kind`` and SemVer of the ``StateDiagnostics.to_payload`` layout (breaking change = major).
PAYLOAD_KIND: Final = "state_diagnostics"
PAYLOAD_SCHEMA_VERSION: Final = "1.0.0"

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

    def to_payload(self) -> dict[str, object]:
        """Deterministic JSON-ready form (module docs); times must be timezone-aware."""

        def per_state[V](values: Mapping[str, V], encode: _Encoder[V]) -> dict[str, object]:
            return {key: encode(values[key]) for key in sorted(values)}

        return {
            "kind": PAYLOAD_KIND,
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            "probability_places": PROBABILITY_PLACES,
            "state_space": list(self.state_space),
            "evaluations": self.evaluations,
            "not_computable": self.not_computable,
            "counts": per_state(self.counts, _same),
            "shares": per_state(self.shares, _decimal_text),
            "runs": [
                {
                    "state": run.state,
                    "start": _utc_text(run.start),
                    "end": _utc_text(run.end),
                    "steps": run.steps,
                }
                for run in self.runs
            ],
            "mean_steps": per_state(self.mean_steps, _decimal_text),
            "max_steps": per_state(self.max_steps, _same),
            "transitions": {
                a: per_state(self.transitions[a], _same) for a in sorted(self.transitions)
            },
            "transition_probabilities": {
                a: per_state(self.transition_probabilities[a], _decimal_text)
                for a in sorted(self.transition_probabilities)
            },
            "min_run": self.min_run,
            "short_run_share": _decimal_text(self.short_run_share),
            "switch_rate": _decimal_text(self.switch_rate),
        }

    @property
    def diagnostics_hash(self) -> str:
        """Content hash of ``to_payload()``."""
        return content_hash(self.to_payload())

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object], *, expected_hash: str | None = None
    ) -> StateDiagnostics:
        """Rebuild a report from ``to_payload()`` output; ``ValueError`` on anything else.

        The payload must be exactly the canonical form of the rebuilt report (same keys, same
        encodings), and its hash must equal ``expected_hash`` when one is given.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("a state diagnostics payload must be a mapping")
        if set(payload) != _PAYLOAD_KEYS:
            raise ValueError(
                f"payload keys differ: missing {sorted(_PAYLOAD_KEYS - set(payload))}, "
                f"unexpected {sorted(set(payload) - _PAYLOAD_KEYS)}"
            )
        if payload["kind"] != PAYLOAD_KIND or payload["schema_version"] != PAYLOAD_SCHEMA_VERSION:
            raise ValueError(
                f"not a {PAYLOAD_KIND}@{PAYLOAD_SCHEMA_VERSION} payload: "
                f"{payload['kind']!r}@{payload['schema_version']!r}"
            )
        if payload["probability_places"] != PROBABILITY_PLACES:
            raise ValueError(f"probability_places must be {PROBABILITY_PLACES}")
        space = payload["state_space"]
        if not isinstance(space, list) or not all(isinstance(label, str) for label in space):
            raise ValueError("state_space must be a list of labels")
        runs = payload["runs"]
        if not isinstance(runs, list):
            raise ValueError("runs must be a list")
        report = cls(
            state_space=tuple(space),
            evaluations=_int(payload["evaluations"], "evaluations"),
            not_computable=_int(payload["not_computable"], "not_computable"),
            counts=_per_state(payload["counts"], space, "counts", _int),
            shares=_per_state(payload["shares"], space, "shares", _optional_decimal),
            runs=tuple(_run(item) for item in runs),
            mean_steps=_per_state(payload["mean_steps"], space, "mean_steps", _optional_decimal),
            max_steps=_per_state(payload["max_steps"], space, "max_steps", _int),
            transitions=_per_state(
                payload["transitions"],
                space,
                "transitions",
                lambda row, where: _per_state(row, space, where, _int),
            ),
            transition_probabilities=_per_state(
                payload["transition_probabilities"],
                space,
                "transition_probabilities",
                lambda row, where: _per_state(row, space, where, _optional_decimal),
            ),
            min_run=_int(payload["min_run"], "min_run"),
            short_run_share=_optional_decimal(payload["short_run_share"], "short_run_share"),
            switch_rate=_optional_decimal(payload["switch_rate"], "switch_rate"),
        )
        if report.to_payload() != dict(payload):
            raise ValueError("the payload is not the canonical form of the report it encodes")
        if expected_hash is not None and report.diagnostics_hash != expected_hash:
            raise ValueError(
                f"diagnostics hash {report.diagnostics_hash} differs from {expected_hash}"
            )
        return report


type _Encoder[V] = Callable[[V], object]
type _Decoder[V] = Callable[[object, str], V]

_PAYLOAD_KEYS: Final = frozenset(
    {
        "kind",
        "schema_version",
        "probability_places",
        "state_space",
        "evaluations",
        "not_computable",
        "counts",
        "shares",
        "runs",
        "mean_steps",
        "max_steps",
        "transitions",
        "transition_probabilities",
        "min_run",
        "short_run_share",
        "switch_rate",
    }
)
_RUN_KEYS: Final = frozenset({"state", "start", "end", "steps"})


def _same[V](value: V) -> V:
    return value


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{value!r} is not timezone-aware: diagnostics times must be UTC")
    return value.astimezone(UTC).isoformat()


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an int")
    return value


def _optional_decimal(value: object, where: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a decimal string or null")
    try:
        number = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError(f"{where}: {value!r} is not a decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{where} must be finite")
    return number


def _utc_time(value: object, where: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{where} must be UTC")
    return parsed


def _per_state[V](
    value: object, space: Sequence[str], where: str, decode: _Decoder[V]
) -> dict[str, V]:
    if not isinstance(value, Mapping) or set(value) != set(space):
        raise ValueError(f"{where} must map exactly the state space")
    return {label: decode(value[label], f"{where}[{label}]") for label in space}


def _run(value: object) -> StateRun:
    if not isinstance(value, Mapping) or set(value) != _RUN_KEYS:
        raise ValueError(f"a run must have exactly the keys {sorted(_RUN_KEYS)}")
    state = value["state"]
    if not isinstance(state, str):
        raise ValueError("a run's state must be a label")
    return StateRun(
        state=state,
        start=_utc_time(value["start"], "run start"),
        end=_utc_time(value["end"], "run end"),
        steps=_int(value["steps"], "run steps"),
    )


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
