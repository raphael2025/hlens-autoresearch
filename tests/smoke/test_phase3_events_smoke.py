"""Phase 3 smoke: synthetic bars -> features -> events -> interactions -> statistics (ADR-0036).

FRAMEWORK_IMPLEMENTED / NOT_VALIDATED: this proves the pipeline is wired, point-in-time,
deterministic and traceable on a seeded synthetic market; it says nothing about any real-market
effect.

The state series is a stand-in built here (the sign of the log return, a causal label per minute)
in the local Phase 3 shape — Phase 2's StateProvider replaces it when wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.contracts.event import EventInputPoint, EventProvider, EventRequest, EventResult
from core.contracts.feature import FeatureObservation, FeatureRequest, FeatureResult
from core.contracts.synthetic import SyntheticBar, SyntheticMarketSpec
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import EventSpec, FeatureSpec
from infrastructure.event.inputs import (
    StateSeriesPoint,
    feature_value_lineage,
    inputs_from_feature_run,
    inputs_from_state_series,
)
from infrastructure.event.runner import run_events
from infrastructure.feature.runner import run_feature
from plugins.events import (
    EventCoOccurrenceProvider,
    EventSequenceProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
    VolatilityBreakoutProvider,
)
from plugins.features import BarLogReturnProvider, BarRealizedVolatilityProvider
from plugins.synthetic import RandomWalkMarket
from research.events.stats import co_occurrence, event_frequency, lead_lag, overlap_diagnostics

START = datetime(2024, 1, 1, tzinfo=UTC)
MINUTES = 150
MINUTE = timedelta(minutes=1)
CUT = START + 90 * MINUTE
LINEAGE = SelectedRevisionLineage(
    # Synthetic bars stand in for PIT-selected Canonical bars (labels only; no catalog involved).
    canonical_table="canonical.synthetic_bars_1m",
    canonical_revision_id="synthetic",
    raw_table="raw.synthetic",
    raw_revision_id="synthetic",
    source_table="raw.synthetic_source",
    source_revision_id="seed-11",
)
LOG_RETURN = BarLogReturnProvider.spec()
REALIZED_VOL = BarRealizedVolatilityProvider.spec(5)
DIRECTION = Ref(kind=Kind.STATE, name="return_direction", version="1.0.0")

CROSS = FeatureThresholdCrossProvider.spec(LOG_RETURN.ref, Decimal(0), "up")
BREAKOUT = VolatilityBreakoutProvider.spec(REALIZED_VOL.ref, 10, Decimal("1.2"))
SWITCH = StateSwitchProvider.spec(DIRECTION)
SEQUENCE = EventSequenceProvider.spec(BREAKOUT, SWITCH, 5 * MINUTE)
CO_OCCUR = EventCoOccurrenceProvider.spec(CROSS, BREAKOUT, 2 * MINUTE)


def _bars(*, shock_after: datetime | None = None) -> tuple[SyntheticBar, ...]:
    market = RandomWalkMarket().generate(
        SyntheticMarketSpec(
            name="phase3_smoke",
            version="1.0.0",
            symbol="SYN-USDT",
            start=START,
            minutes=MINUTES,
            seed=11,
            initial_price=Decimal(100),
            volatility=Decimal("0.002"),
        )
    )
    if shock_after is None:
        return market.bars
    return tuple(
        bar.model_copy(update={"close": bar.close * 3})
        if bar.interval_start >= shock_after
        else bar
        for bar in market.bars
    )


def _observation(bar: SyntheticBar) -> FeatureObservation:
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": "SYN-USDT",
        "close": bar.close,
        "volume": bar.volume,
    }
    return FeatureObservation(
        observation_key=f"bar:{bar.interval_start.isoformat()}",
        event_time=bar.interval_start,
        event_end_time=bar.interval_end,
        available_time=bar.interval_end,
        knowledge_time=bar.interval_end,
        values=FrozenMapping(values),
        lineage=LINEAGE,
    )


def _observations(bars: tuple[SyntheticBar, ...]) -> tuple[FeatureObservation, ...]:
    return tuple(_observation(bar) for bar in bars)


def _feature(
    provider: BarLogReturnProvider | BarRealizedVolatilityProvider,
    spec: FeatureSpec,
    observations: tuple[FeatureObservation, ...],
) -> tuple[FeatureRequest, FeatureResult]:
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=content_hash({"synthetic": "phase3_smoke"}),
        knowledge_cutoff=START + (MINUTES + 1) * MINUTE,
        evaluation_times=tuple(START + minute * MINUTE for minute in range(1, MINUTES + 1)),
        observations=observations,
    )
    return request, run_feature(provider, spec, request)


def _direction(points: tuple[EventInputPoint, ...]) -> tuple[EventInputPoint, ...]:
    """Stand-in state series: the sign of each log return (causal, per point)."""
    series = []
    for item in points:
        value = item.value
        label = None
        if isinstance(value, Decimal):
            label = "up" if value > 0 else "down" if value < 0 else "flat"
        series.append(
            StateSeriesPoint(
                evaluation_time=item.evaluation_time,
                available_time=item.available_time,
                label=label,
                lineage_hash=content_hash(
                    {"stand_in": "return_direction", "from": item.source_lineage_hash}
                ),
            )
        )
    return inputs_from_state_series(DIRECTION, series)


@dataclass(frozen=True)
class Pipeline:
    inputs: tuple[EventInputPoint, ...]
    #: Every causal lineage hash the feature runs and the state stand-in can produce.
    lineages: frozenset[str]
    results: dict[str, EventResult]


def _run(spec: EventSpec, provider: EventProvider, **fields: Any) -> EventResult:
    request = EventRequest(
        event=spec.ref,
        spec_hash=spec.content_hash(),
        as_of=START + (MINUTES + 5) * MINUTE,
        **fields,
    )
    return run_events(provider, spec, request)


def _pipeline(*, shock_after: datetime | None = None) -> Pipeline:
    observations = _observations(_bars(shock_after=shock_after))
    returns_request, returns = _feature(
        BarLogReturnProvider((LOG_RETURN,)), LOG_RETURN, observations
    )
    vol_request, vol = _feature(
        BarRealizedVolatilityProvider((REALIZED_VOL,)), REALIZED_VOL, observations
    )
    return_points = inputs_from_feature_run(returns_request, returns)
    feature_lineages = {
        feature_value_lineage(feature_request, result, value)
        for feature_request, result in ((returns_request, returns), (vol_request, vol))
        for value in result.values
    }
    inputs = return_points + inputs_from_feature_run(vol_request, vol) + _direction(return_points)
    results: dict[str, EventResult] = {}
    for spec, provider in (
        (CROSS, FeatureThresholdCrossProvider((CROSS,))),
        (BREAKOUT, VolatilityBreakoutProvider((BREAKOUT,))),
        (SWITCH, StateSwitchProvider((SWITCH,))),
    ):
        results[spec.name] = _run(spec, provider, inputs=inputs)
    results[SEQUENCE.name] = _run(
        SEQUENCE,
        EventSequenceProvider((SEQUENCE,)),
        upstream_events=results[BREAKOUT.name].events + results[SWITCH.name].events,
    )
    results[CO_OCCUR.name] = _run(
        CO_OCCUR,
        EventCoOccurrenceProvider((CO_OCCUR,)),
        upstream_events=results[CROSS.name].events + results[BREAKOUT.name].events,
    )
    return Pipeline(
        inputs=inputs,
        lineages=frozenset(feature_lineages)
        | {content_hash({"stand_in": "return_direction", "from": x}) for x in feature_lineages},
        results=results,
    )


def test_phase3_pipeline_is_point_in_time_deterministic_and_traceable() -> None:
    base = _pipeline()
    for name, result in base.results.items():
        assert result.events, f"{name} produced no events on the smoke market"

    # Determinism: the whole pipeline again gives the same result hashes.
    again = _pipeline()
    assert {k: v.result_hash for k, v in again.results.items()} == {
        k: v.result_hash for k, v in base.results.items()
    }

    # No look-ahead: perturbing the market after CUT leaves every event up to CUT unchanged.
    shocked = _pipeline(shock_after=CUT)
    changed_later = False
    for name, result in base.results.items():
        assert shocked.results[name].restricted_to(CUT) == result.restricted_to(CUT), name
        changed_later |= shocked.results[name].events != result.events
    assert changed_later, "the perturbation must change something after CUT (teeth)"

    # Traceability: interactions -> upstream events -> input points -> feature / state runs.
    point_ids = {item.point_id: item for item in base.inputs}
    upstream = {
        item.event_id: item
        for name in (CROSS.name, BREAKOUT.name, SWITCH.name)
        for item in base.results[name].events
    }
    for name in (SEQUENCE.name, CO_OCCUR.name):
        for item in base.results[name].events:
            assert item.upstream_event_ids and set(item.upstream_event_ids) <= upstream.keys()
            for event_id in item.upstream_event_ids:
                for point_id in upstream[event_id].input_ids:
                    source = point_ids[point_id]
                    assert source.source_lineage_hash in base.lineages

    # Statistics run on the tables (descriptive only).
    end = START + (MINUTES + 5) * MINUTE
    cross, breakout = base.results[CROSS.name].events, base.results[BREAKOUT.name].events
    assert event_frequency(cross, start=START, end=end, bucket=30 * MINUTE).count == len(cross)
    assert co_occurrence(cross, breakout, window=2 * MINUTE, start=START, end=end).n_a == len(cross)
    assert lead_lag(cross, breakout, max_lag=10 * MINUTE, bin=MINUTE).pairs >= 0
    assert overlap_diagnostics(cross, horizon=5 * MINUTE).n == len(cross)
