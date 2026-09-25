"""State stability diagnostics (Phase 2; research only): distribution, durations, transitions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.contracts.state import StateProviderDescriptor, StateRequest, StateResult, StateValue
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from research.states.diagnostics import diagnose, render_markdown, runs_of, transition_counts
from tests.fake_states import at

SPACE = ("a", "b")
#: a a b None b b a b  → runs: a×2, b×1 | b×2, a×1, b×1
LABELS = ("a", "a", "b", None, "b", "b", "a", "b")
SERIES = tuple((at(i), label) for i, label in enumerate(LABELS))


def test_runs_break_at_none_and_at_label_changes() -> None:
    runs = runs_of(SERIES)
    assert [(run.state, run.steps) for run in runs] == [
        ("a", 2),
        ("b", 1),
        ("b", 2),
        ("a", 1),
        ("b", 1),
    ]
    assert runs[0].wall_time == at(1) - at(0)


def test_transitions_skip_pairs_with_none() -> None:
    counts = transition_counts(SERIES, SPACE)
    # pairs: aa, ab, (b,None) skip, (None,b) skip, bb, ba, ab
    assert counts == {"a": {"a": 1, "b": 2}, "b": {"a": 1, "b": 1}}


def test_report_distribution_durations_and_flicker() -> None:
    report = diagnose(SERIES, SPACE, min_run=2)
    assert (report.evaluations, report.not_computable) == (8, 1)
    assert report.counts == {"a": 3, "b": 4}
    assert report.shares["a"] == Decimal("0.428571")
    assert report.mean_steps == {"a": Decimal("1.500000"), "b": Decimal("1.333333")}
    assert report.max_steps == {"a": 2, "b": 2}
    assert report.transition_probabilities["a"] == {
        "a": Decimal("0.333333"),
        "b": Decimal("0.666667"),
    }
    assert report.short_run_share == Decimal("0.600000")  # 3 of 5 runs shorter than 2 steps
    assert report.switch_rate == Decimal("0.600000")  # 3 changes in 5 computable pairs
    text = render_markdown(report)
    assert "| a | 3 |" in text and "flicker" in text


def test_report_from_a_state_result_and_empty_rows() -> None:
    descriptor = StateProviderDescriptor(
        name="fixture",
        version="1.0.0",
        deterministic=True,
        supported_states=FrozenMapping({"state:s@1.0.0": content_hash({"s": 1})}),
    )
    request = StateRequest(
        state=Ref(kind=Kind.STATE, name="s", version="1.0.0"),
        spec_hash=content_hash({"s": 1}),
        evaluation_times=(at(0), at(1)),
        inputs=(),
    )
    result = StateResult.build(
        request,
        descriptor,
        [StateValue(evaluation_time=at(i), state=None, inputs_used=0) for i in range(2)],
    )
    report = diagnose(result, SPACE, min_run=1)
    assert report.not_computable == 2
    assert report.shares == {"a": None, "b": None}
    assert report.transition_probabilities["a"] == {"a": None, "b": None}
    assert report.switch_rate is None and report.short_run_share is None


@pytest.mark.parametrize(
    "series",
    [
        ((at(1), "a"), (at(0), "a")),  # out of order
        ((at(0), "c"),),  # label outside the state space
    ],
)
def test_malformed_series_are_refused(series: tuple[tuple[object, str], ...]) -> None:
    with pytest.raises(ValueError):
        diagnose(series, SPACE, min_run=1)  # type: ignore[arg-type]


def test_min_run_is_a_required_positive_parameter() -> None:
    with pytest.raises(ValueError):
        diagnose(SERIES, SPACE, min_run=0)
