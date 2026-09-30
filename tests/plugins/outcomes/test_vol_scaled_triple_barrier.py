"""Phase 4 / ADR-0088 decision 5: ``VolScaledTripleBarrierOutcome`` passes the provider-agnostic
suite (same as ``triple_barrier``), plus exact values, same-bar double touch, a gap, missing /
non-positive volatility, and that a later event's volatility cannot change an earlier label.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

import pytest

from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeInputError,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeRequest,
)
from core.domain.base import Kind, Ref
from core.domain.specs import OutcomeSpec
from plugins.outcomes import TripleBarrierOutcome, VolScaledTripleBarrierOutcome
from tests.contract_suites.outcome import OutcomeProviderContract, OutcomeSubject
from tests.plugins.outcomes.test_outcome_providers import BARS, EVENTS, MANIFEST, MINUTE, T0

VOL_FEATURE = Ref(kind=Kind.FEATURE, name="realized_vol", version="1.0.0")


def _outcome_spec(name: str = "vst_10m", horizon: timedelta = 10 * MINUTE) -> OutcomeSpec:
    return OutcomeSpec(
        name=name, version="1.0.0", created_at=T0, horizon=horizon, label_definition="test label"
    )


#: Same scale (0.0015) as the plain triple-barrier fixture (`BARRIER` in test_outcome_providers.py)
#: over the same `BARS`, so this reuses the known-good touch/no-touch shape of that fixture.
VOL_SCALED = OutcomeLabelSpec.bind(
    _outcome_spec(),
    OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER,
    volatility_feature=VOL_FEATURE,
    barrier_multiplier=Decimal("2"),
)
VOL_SCALED_OTHER = OutcomeLabelSpec.bind(
    _outcome_spec("vst_5m", 5 * MINUTE),
    OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER,
    volatility_feature=VOL_FEATURE,
    barrier_multiplier=Decimal("2"),
)

#: `barrier_multiplier * VOLATILITY == 0.0015`, matching `BARRIER.upper_barrier == lower_barrier`.
VOLATILITY = Decimal("0.00075")
VOLATILITY_BY_EVENT = {event.event_key: VOLATILITY for event in EVENTS}


def _request(
    spec: OutcomeLabelSpec = VOL_SCALED, events: tuple[OutcomeEvent, ...] = EVENTS
) -> OutcomeRequest:
    return OutcomeRequest(
        label_spec=spec,
        manifest_content_hash=MANIFEST,
        price_cutoff=BARS[-1].available_time,
        events=events,
        bars=BARS,
    )


def _provider(
    volatility: Mapping[str, Decimal | None] | None = None,
) -> VolScaledTripleBarrierOutcome:
    return VolScaledTripleBarrierOutcome(
        (VOL_SCALED,), volatility=VOLATILITY_BY_EVENT if volatility is None else volatility
    )


class TestVolScaledTripleBarrierContract(OutcomeProviderContract):
    @pytest.fixture
    def outcome_subject(self) -> OutcomeSubject:
        return OutcomeSubject(
            open=lambda: _provider(),
            label_spec=VOL_SCALED,
            other_spec=VOL_SCALED_OTHER,
            bars=BARS,
            events=EVENTS,
            manifest_content_hash=MANIFEST,
        )


def test_vol_scaled_touches_and_vertical_exit() -> None:
    result = _provider().compute(_request())
    barriers = {label.event_key: label.barrier for label in result.labels}
    assert set(barriers.values()) - {None} <= {-1, 0, 1}
    assert any(label.barrier in (-1, 1) for label in result.labels)
    for label in result.labels:
        if label.barrier in (-1, 1):
            assert label.exit_time is not None and label.entry_time is not None
            assert label.exit_time <= label.entry_time + VOL_SCALED.horizon


def test_vol_scaled_value_matches_the_fixed_fraction_equivalent() -> None:
    """`barrier_multiplier * volatility == 0.0015` must reproduce the same result as the plain
    `triple_barrier` fixture with fixed `upper_barrier == lower_barrier == 0.0015`, since the
    scanning logic and prices are identical."""
    from tests.plugins.outcomes.test_outcome_providers import BARRIER

    fixed_request = OutcomeRequest(
        label_spec=BARRIER,
        manifest_content_hash=MANIFEST,
        price_cutoff=BARS[-1].available_time,
        events=EVENTS,
        bars=BARS,
    )
    fixed = TripleBarrierOutcome((BARRIER,)).compute(fixed_request).labels
    scaled = _provider().compute(_request()).labels
    for fixed_label, scaled_label in zip(fixed, scaled, strict=True):
        assert fixed_label.event_key == scaled_label.event_key
        assert fixed_label.value == scaled_label.value
        assert fixed_label.barrier == scaled_label.barrier
        assert fixed_label.entry_price == scaled_label.entry_price
        assert fixed_label.exit_price == scaled_label.exit_price


def _one_bar_request(
    bar: OutcomePriceBar, *, multiplier: Decimal = Decimal("2")
) -> tuple[OutcomeRequest, OutcomeLabelSpec]:
    spec = OutcomeLabelSpec.bind(
        _outcome_spec("vst_1m", MINUTE),
        OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER,
        volatility_feature=VOL_FEATURE,
        barrier_multiplier=multiplier,
    )
    request = OutcomeRequest(
        label_spec=spec,
        manifest_content_hash=MANIFEST,
        price_cutoff=bar.available_time,
        events=(OutcomeEvent(event_key="e", event_time=bar.interval_start),),
        bars=(bar,),
    )
    return request, spec


def test_both_barriers_in_one_bar_resolve_to_the_lower() -> None:
    start = T0
    wide = OutcomePriceBar(
        interval_start=start,
        interval_end=start + MINUTE,
        available_time=start + MINUTE,
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
    )
    # multiplier(2) * volatility(0.005) == 0.01: barriers at 101 (touched by high) and 99 (touched
    # by low) — both sides of this bar are reached, so the double touch must resolve to -1.
    request, spec = _one_bar_request(wide, multiplier=Decimal("2"))
    provider = VolScaledTripleBarrierOutcome((spec,), volatility={"e": Decimal("0.005")})
    label = provider.compute(request).labels[0]
    assert label.barrier == -1
    assert label.exit_price == Decimal("99.00")


def test_a_gap_through_the_lower_barrier_exits_at_the_open() -> None:
    start = T0
    entry_bar = OutcomePriceBar(  # no touch: barriers are entry(100) * (1 +/- 0.01) = [99, 101]
        interval_start=start,
        interval_end=start + MINUTE,
        available_time=start + MINUTE,
        open=Decimal(100),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal(100),
    )
    gapped = OutcomePriceBar(  # opens below the 99 lower barrier: a gap down between the bars
        interval_start=start + MINUTE,
        interval_end=start + 2 * MINUTE,
        available_time=start + 2 * MINUTE,
        open=Decimal("90"),
        high=Decimal("90.5"),
        low=Decimal("89"),
        close=Decimal("89.5"),
    )
    spec = OutcomeLabelSpec.bind(
        _outcome_spec("vst_2m", 2 * MINUTE),
        OutcomeMethod.VOL_SCALED_TRIPLE_BARRIER,
        volatility_feature=VOL_FEATURE,
        barrier_multiplier=Decimal("2"),
    )
    request = OutcomeRequest(
        label_spec=spec,
        manifest_content_hash=MANIFEST,
        price_cutoff=gapped.available_time,
        events=(OutcomeEvent(event_key="e", event_time=start),),
        bars=(entry_bar, gapped),
    )
    provider = VolScaledTripleBarrierOutcome((spec,), volatility={"e": Decimal("0.005")})
    label = provider.compute(request).labels[0]
    assert label.barrier == -1
    assert label.entry_price == Decimal(100)
    assert label.exit_price == Decimal("90")  # the gap open, not the (unreached) 99 barrier price


def test_missing_volatility_is_an_explicit_none_label() -> None:
    label = _provider(volatility={}).compute(_request(events=(EVENTS[0],))).labels[0]
    assert label.value is None
    assert label.entry_time is None and label.exit_time is None


def test_volatility_mapped_to_none_is_an_explicit_none_label() -> None:
    volatility: dict[str, Decimal | None] = {EVENTS[0].event_key: None}
    label = _provider(volatility=volatility).compute(_request(events=(EVENTS[0],))).labels[0]
    assert label.value is None


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-0.001")])
def test_non_positive_volatility_fails_closed(bad: Decimal) -> None:
    provider = _provider(volatility={EVENTS[0].event_key: bad})
    with pytest.raises(OutcomeInputError, match="volatility"):
        provider.compute(_request(events=(EVENTS[0],)))


def test_a_scale_that_collapses_the_lower_barrier_fails_closed() -> None:
    # multiplier(2) * volatility(0.6) == 1.2 >= 1: the lower barrier would be <= 0.
    request, spec = _one_bar_request(BARS[0], multiplier=Decimal("2"))
    provider = VolScaledTripleBarrierOutcome((spec,), volatility={"e": Decimal("0.6")})
    with pytest.raises(OutcomeInputError, match="barrier"):
        provider.compute(request)


def test_a_later_events_volatility_does_not_change_an_earlier_label() -> None:
    """Perturbing the mapping entry of a later event must not change an earlier event's label
    (each event's barrier depends only on its own entry-time volatility)."""
    early, late = EVENTS[0], EVENTS[1]
    request = _request(events=(early, late))
    base = _provider().compute(request).labels
    perturbed_volatility = dict(VOLATILITY_BY_EVENT)
    perturbed_volatility[late.event_key] = VOLATILITY * 5
    perturbed = _provider(volatility=perturbed_volatility).compute(request).labels
    by_key_base = {label.event_key: label for label in base}
    by_key_perturbed = {label.event_key: label for label in perturbed}
    assert by_key_base[early.event_key] == by_key_perturbed[early.event_key]
