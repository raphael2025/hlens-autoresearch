"""ADR-0105 §1 (D-P11-WINDOW): a metric that depends on the Profile's research window, asked for a
recent window outside it, is refused as ``metric_undefined`` with a message that says why and
cites the ADR. The refusal itself (code, which windows) is unchanged by the amendment."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research.operations.authority import (
    METRIC_UNDEFINED,
    MONITORING_METRICS,
    AuthorityRefused,
    MonitoringMetricDefinition,
    RuledMetric,
    _check_window_scope,
)
from research.operations.degradation import ObservationWindow
from tests.factories import validation_profile

PROFILE = validation_profile()  # research window [2020-01-01, 2024-01-01)
DEPENDENT = MONITORING_METRICS["positive_window_fraction"][0]
INDEPENDENT = MONITORING_METRICS["breakeven_cost_multiple"][0]
assert DEPENDENT.requires_research_window and not INDEPENDENT.requires_research_window


def _window(start: tuple[int, int, int], end: tuple[int, int, int]) -> ObservationWindow:
    return ObservationWindow(
        start=datetime(*start, tzinfo=UTC), end=datetime(*end, tzinfo=UTC), label="TEST ONLY"
    )


def _ruled(*definitions: MonitoringMetricDefinition) -> list[RuledMetric]:
    return [RuledMetric(definition=d, gate_id=d.baseline_gate_id) for d in definitions]


@pytest.mark.parametrize(
    "window",
    [
        _window((2026, 2, 1), (2026, 3, 1)),  # a recent window, after the sealed boundary
        _window((2023, 12, 1), (2024, 2, 1)),  # straddles the sealed OOS boundary
        _window((2019, 12, 1), (2020, 2, 1)),  # starts before the research window
    ],
)
def test_a_research_window_metric_on_a_window_outside_it_is_refused_with_the_reason(
    window: ObservationWindow,
) -> None:
    with pytest.raises(AuthorityRefused) as refused:
        _check_window_scope(_ruled(DEPENDENT), PROFILE, window)
    assert refused.value.code == METRIC_UNDEFINED
    message = str(refused.value)
    assert "ADR-0105" in message and "D-P11-WINDOW" in message
    assert "positive_window_fraction" in message  # the metric is named
    assert "2020-01-01T00:00:00Z" in message and "2024-01-01T00:00:00Z" in message
    assert "depends on the Profile's fixed research window" in message
    assert "degradation thresholds" in message


def test_the_refusal_semantics_are_unchanged() -> None:
    inside = _window((2021, 1, 1), (2021, 7, 1))
    _check_window_scope(_ruled(DEPENDENT), PROFILE, inside)  # inside the research window: fine
    edge = _window((2020, 1, 1), (2024, 1, 1))  # exactly the research window (end exclusive)
    _check_window_scope(_ruled(DEPENDENT), PROFILE, edge)
    recent = _window((2026, 2, 1), (2026, 3, 1))
    _check_window_scope(_ruled(INDEPENDENT), PROFILE, recent)  # not research-window dependent
    with pytest.raises(AuthorityRefused):  # one dependent metric is enough to refuse
        _check_window_scope(_ruled(INDEPENDENT, DEPENDENT), PROFILE, recent)
