"""State stability diagnostics (Phase 2; research only): distribution, durations, transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.contracts.state import StateProviderDescriptor, StateRequest, StateResult, StateValue
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from research.states.diagnostics import (
    StateDiagnostics,
    diagnose,
    render_markdown,
    runs_of,
    transition_counts,
)
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


# --------------------------------------------------------------------------- payload / hash


def _wire(report: StateDiagnostics) -> dict[str, object]:
    """``to_payload`` through real JSON text, as a stored report would come back."""
    loaded: dict[str, object] = json.loads(json.dumps(report.to_payload()))
    return loaded


def test_payload_round_trips_exactly_through_json() -> None:
    report = diagnose(SERIES, SPACE, min_run=2)
    payload = report.to_payload()
    assert payload["shares"] == {"a": "0.428571", "b": "0.571429"}
    assert payload["counts"] == {"a": 3, "b": 4}
    assert payload["runs"][0] == {  # type: ignore[index]
        "state": "a",
        "start": at(0).isoformat(),
        "end": at(1).isoformat(),
        "steps": 2,
    }
    assert str(payload["runs"][0]["start"]).endswith("+00:00")  # type: ignore[index]
    back = StateDiagnostics.from_payload(_wire(report), expected_hash=report.diagnostics_hash)
    assert back == report
    assert back.diagnostics_hash == report.diagnostics_hash


def test_payload_and_hash_are_deterministic_and_order_independent() -> None:
    report = diagnose(SERIES, SPACE, min_run=2)
    again = diagnose(SERIES, SPACE, min_run=2)
    assert json.dumps(report.to_payload()) == json.dumps(again.to_payload())
    assert report.diagnostics_hash == again.diagnostics_hash
    # the per-state maps are emitted with sorted keys whatever order the dataclass holds
    shuffled = StateDiagnostics(
        **{
            **report.__dict__,
            "counts": dict(reversed(list(report.counts.items()))),
            "shares": dict(reversed(list(report.shares.items()))),
        }
    )
    assert json.dumps(shuffled.to_payload()) == json.dumps(report.to_payload())
    # a different parameter is a different report
    assert diagnose(SERIES, SPACE, min_run=3).diagnostics_hash != report.diagnostics_hash
    # the declared state-space order is semantic (it orders the Markdown table) and is kept
    assert diagnose(SERIES, ("b", "a"), min_run=2).diagnostics_hash != report.diagnostics_hash


def test_non_utc_times_are_normalized_and_naive_times_refused() -> None:
    plus_eight = timezone(timedelta(hours=8))
    shifted = tuple((t.astimezone(plus_eight), label) for t, label in SERIES)
    report = diagnose(SERIES, SPACE, min_run=2)
    assert diagnose(shifted, SPACE, min_run=2).to_payload() == report.to_payload()
    naive = tuple((t.replace(tzinfo=None), label) for t, label in SERIES)
    with pytest.raises(ValueError, match="timezone-aware"):
        diagnose(naive, SPACE, min_run=2).to_payload()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("shares", {"a": "0.428571", "b": "0.571430"}),  # tampered value
        ("counts", {"a": 3}),  # a state missing
        ("min_run", True),  # a bool is not an int
        ("schema_version", "2.0.0"),
        ("short_run_share", 0.6),  # a float is not an exact decimal
    ],
)
def test_malformed_or_tampered_payloads_are_refused(key: str, value: object) -> None:
    report = diagnose(SERIES, SPACE, min_run=2)
    payload = {**_wire(report), key: value}
    with pytest.raises(ValueError):
        StateDiagnostics.from_payload(payload, expected_hash=report.diagnostics_hash)


def test_a_tampered_payload_fails_the_expected_hash_and_non_canonical_text_is_refused() -> None:
    report = diagnose(SERIES, SPACE, min_run=2)
    tampered = {**_wire(report), "switch_rate": "0.700000"}
    assert StateDiagnostics.from_payload(tampered).switch_rate == Decimal("0.700000")
    with pytest.raises(ValueError, match="differs"):
        StateDiagnostics.from_payload(tampered, expected_hash=report.diagnostics_hash)
    payload = _wire(report)
    runs = [dict(run) for run in payload["runs"]]  # type: ignore[attr-defined]
    runs[0]["start"] = at(0).strftime("%Y-%m-%dT%H:%M:%SZ")  # same instant, not canonical text
    with pytest.raises(ValueError, match="canonical"):
        StateDiagnostics.from_payload({**payload, "runs": runs})
    with pytest.raises(ValueError, match="keys"):
        StateDiagnostics.from_payload({**payload, "extra": 1})


def test_a_later_perturbation_does_not_change_an_earlier_report() -> None:
    """The report holds no window of its own: it describes exactly the series it is given."""
    cut = at(4)
    past = tuple(item for item in SERIES if item[0] <= cut)
    before = diagnose(past, SPACE, min_run=2)
    later = tuple((t, None if t > cut else label) for t, label in SERIES) + (
        (at(20), "a"),
        (datetime(2030, 1, 1, tzinfo=UTC), "b"),
    )
    after = diagnose(tuple(item for item in later if item[0] <= cut), SPACE, min_run=2)
    assert after.diagnostics_hash == before.diagnostics_hash
    # and the perturbation does change a report that covers it (the hash is not constant)
    assert diagnose(later, SPACE, min_run=2).diagnostics_hash != (
        diagnose(SERIES, SPACE, min_run=2).diagnostics_hash
    )
