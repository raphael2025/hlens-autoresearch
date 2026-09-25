"""Event runner (Phase 3; ADR-0036 §3): truncation, point-in-time consistency, materialization."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from core.contracts.event import EventResult, UnsupportedEvent
from core.contracts.feature import FeatureObservation, FeatureRequest
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import EventSpec
from infrastructure.event.inputs import (
    feature_value_lineage,
    inputs_from_feature_run,
    inputs_from_state_series,
)
from infrastructure.event.runner import (
    EventRunnerError,
    FutureConfirmationError,
    default_checkpoints,
    run_events,
)
from infrastructure.event.table import EVENT_TABLE_COLUMNS, event_table
from infrastructure.feature.runner import run_feature
from plugins.events import (
    EventCoOccurrenceProvider,
    EventSequenceProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
    VolatilityBreakoutProvider,
)
from plugins.features import BarLogReturnProvider
from tests.fake_events import (
    END,
    LAG,
    MINUTE,
    REGIME,
    REGIME_INPUTS,
    T0,
    X_INPUTS,
    BackdatedProvider,
    ConfirmedTopProvider,
    X,
    request,
)

CROSS = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)
CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)


@pytest.mark.parametrize(
    ("provider", "spec"),
    [
        (FeatureThresholdCrossProvider((CROSS,)), CROSS),
        (
            VolatilityBreakoutProvider(
                (VolatilityBreakoutProvider.spec(X, 2, Decimal("1.5"), observable_lag=LAG),)
            ),
            VolatilityBreakoutProvider.spec(X, 2, Decimal("1.5"), observable_lag=LAG),
        ),
        (StateSwitchProvider((SWITCH,)), SWITCH),
    ],
)
def test_runner_equals_a_compliant_direct_answer(provider: object, spec: EventSpec) -> None:
    full = request(spec, inputs=X_INPUTS + REGIME_INPUTS)
    run = run_events(provider, spec, full)  # type: ignore[arg-type]
    direct = provider.detect(full)  # type: ignore[attr-defined]
    assert isinstance(direct, EventResult)
    assert run == direct and run.events


def test_default_checkpoints_are_every_visibility_change() -> None:
    grid = default_checkpoints(request(CROSS, inputs=X_INPUTS), CROSS)
    assert grid[-1] == END
    assert set(grid[:-1]) == {item.available_time + LAG for item in X_INPUTS}


def test_future_confirmation_fails_closed() -> None:
    with pytest.raises(FutureConfirmationError, match="back-dated"):
        run_events(ConfirmedTopProvider((CROSS,)), CROSS, request(CROSS, inputs=X_INPUTS))


def test_a_coarse_grid_is_weaker_and_says_so() -> None:
    # Only as_of: the back-dated tops are invisible to the runner (documented trade-off).
    run_events(
        ConfirmedTopProvider((CROSS,)), CROSS, request(CROSS, inputs=X_INPUTS), checkpoints=()
    )


def test_event_time_that_is_not_observable_fails_closed() -> None:
    with pytest.raises(EventRunnerError, match="not compliant"):
        run_events(BackdatedProvider((CROSS,)), CROSS, request(CROSS, inputs=X_INPUTS))


def test_spec_request_mismatch_and_unsupported() -> None:
    provider = FeatureThresholdCrossProvider((CROSS,))
    with pytest.raises(EventRunnerError, match="not for"):
        run_events(provider, CROSS, request(CROSS_UP, inputs=X_INPUTS))
    with pytest.raises(UnsupportedEvent):
        run_events(provider, CROSS_UP, request(CROSS_UP, inputs=X_INPUTS))
    with pytest.raises(EventRunnerError, match="ascending"):
        run_events(provider, CROSS, request(CROSS, inputs=X_INPUTS), checkpoints=(END, T0))


def test_interactions_run_on_upstream_runs() -> None:
    ups = run_events(
        FeatureThresholdCrossProvider((CROSS_UP,)), CROSS_UP, request(CROSS_UP, inputs=X_INPUTS)
    )
    switches = run_events(
        StateSwitchProvider((SWITCH,)), SWITCH, request(SWITCH, inputs=REGIME_INPUTS)
    )
    upstream = ups.events + switches.events
    for provider_cls, window in (
        (EventSequenceProvider, 3 * MINUTE),
        (EventCoOccurrenceProvider, MINUTE),
    ):
        spec = provider_cls.spec(CROSS_UP, SWITCH, window, observable_lag=LAG)
        result = run_events(provider_cls((spec,)), spec, request(spec, upstream_events=upstream))
        assert result.events
        known = {item.event_id for item in upstream}
        assert all(set(item.upstream_event_ids) <= known for item in result.events)


def test_event_table_rows() -> None:
    result = run_events(
        FeatureThresholdCrossProvider((CROSS,)), CROSS, request(CROSS, inputs=X_INPUTS)
    )
    rows = event_table(result)
    assert EVENT_TABLE_COLUMNS[0] == "event_id" and len(EVENT_TABLE_COLUMNS) == 9
    assert [row.event_id for row in rows] == [item.event_id for item in result.events]
    assert all(
        row.result_hash == result.result_hash and row.event == str(CROSS.ref) for row in rows
    )
    assert rows[0].attributes_json.startswith('{"direction":')


def test_inputs_from_feature_run_and_state_series() -> None:
    spec = BarLogReturnProvider.spec(available_lag=MINUTE)
    lineage = SelectedRevisionLineage(
        canonical_table="canonical.bars_1m",
        canonical_revision_id="crev",
        raw_table="raw.binance_spot_klines_1m",
        raw_revision_id="raw",
        source_table="raw.binance_spot_archives",
        source_revision_id="archive",
    )
    bars = tuple(
        FeatureObservation(
            observation_key=f"bar:{minute}",
            event_time=T0 + minute * MINUTE,
            event_end_time=T0 + (minute + 1) * MINUTE,
            available_time=T0 + (minute + 1) * MINUTE,
            knowledge_time=T0 + (minute + 1) * MINUTE,
            values=FrozenMapping({"close": Decimal(100 + minute), "volume": Decimal(1)}),
            lineage=lineage,
        )
        for minute in range(5)
    )
    feature_request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=content_hash({"manifest": "test"}),
        knowledge_cutoff=T0 + timedelta(days=1),
        evaluation_times=tuple(T0 + minute * MINUTE for minute in range(8)),
        observations=bars,
    )
    result = run_feature(BarLogReturnProvider((spec,)), spec, feature_request)
    points = inputs_from_feature_run(feature_request, result)
    assert [item.evaluation_time for item in points] == list(feature_request.evaluation_times)
    assert all(item.available_time == item.evaluation_time for item in points)
    assert all(item.source == spec.ref for item in points)
    assert [item.source_lineage_hash for item in points] == [
        feature_value_lineage(feature_request, result, value) for value in result.values
    ]
    assert points[0].value is None  # not computable stays None
    other = feature_request.model_copy(update={"evaluation_times": (T0,)})
    with pytest.raises(ValueError, match="does not answer"):
        inputs_from_feature_run(other, result)
    with pytest.raises(ValueError, match="not a state"):
        inputs_from_state_series(Ref(kind=Kind.FEATURE, name="x", version="1.0.0"), ())
