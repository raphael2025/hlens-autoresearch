"""Phase 4: the first OutcomeProviders pass the provider-agnostic suite (ADR-0037).

Also: exact label values on a hand-made price path, and single-fault variants that the suite
must reject (peeking past the horizon, filling a partial window).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabel,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeProvider,
    OutcomeRequest,
)
from core.domain.specs import OutcomeSpec
from plugins.outcomes import ForwardReturnOutcome, TripleBarrierOutcome
from plugins.outcomes._window import label_window, quantize_return, unknown
from tests.contract_suites import outcome as outcome_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.outcome import OutcomeProviderContract, OutcomeSubject

T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
MANIFEST = "9" * 64
#: Per-minute moves in basis points; repeated to 60 bars.
PATTERN = (5, -3, 8, -12, 4, 6, -9, 15, -2, 1, -20, 7)


def _bars(count: int = 60) -> tuple[OutcomePriceBar, ...]:
    bars: list[OutcomePriceBar] = []
    close = Decimal(100)
    for index in range(count):
        opened = close
        close = (opened * (1 + Decimal(PATTERN[index % len(PATTERN)]) / 10_000)).quantize(
            Decimal("1e-8")
        )
        start = T0 + index * MINUTE
        bars.append(
            OutcomePriceBar(
                interval_start=start,
                interval_end=start + MINUTE,
                available_time=start + MINUTE,
                open=opened,
                high=max(opened, close) * Decimal("1.0002"),
                low=min(opened, close) * Decimal("0.9998"),
                close=close,
            )
        )
    return tuple(bars)


BARS = _bars()
EVENTS = tuple(
    OutcomeEvent(event_key=f"e{minute}", event_time=T0 + minute * MINUTE)
    for minute in (3, 17, 30, 55, 59)
) + (OutcomeEvent(event_key="mid-bar", event_time=T0 + 40 * MINUTE + timedelta(seconds=20)),)


def outcome_spec(name: str = "fwd_10m", horizon: timedelta = 10 * MINUTE) -> OutcomeSpec:
    return OutcomeSpec(
        name=name,
        version="1.0.0",
        created_at=T0,
        horizon=horizon,
        label_definition="test label",
    )


FORWARD = OutcomeLabelSpec.bind(outcome_spec(), OutcomeMethod.FORWARD_RETURN)
FORWARD_OTHER = OutcomeLabelSpec.bind(
    outcome_spec("fwd_5m", 5 * MINUTE), OutcomeMethod.FORWARD_RETURN
)
BARRIER = OutcomeLabelSpec.bind(
    outcome_spec("tb_10m"),
    OutcomeMethod.TRIPLE_BARRIER,
    upper_barrier=Decimal("0.0015"),
    lower_barrier=Decimal("0.0015"),
)
BARRIER_OTHER = OutcomeLabelSpec.bind(
    outcome_spec("tb_10m"),
    OutcomeMethod.TRIPLE_BARRIER,
    upper_barrier=Decimal("0.05"),
    lower_barrier=Decimal("0.05"),
)


def subject(
    make: Callable[[], OutcomeProvider], spec: OutcomeLabelSpec, other: OutcomeLabelSpec
) -> OutcomeSubject:
    return OutcomeSubject(
        open=make,
        label_spec=spec,
        other_spec=other,
        bars=BARS,
        events=EVENTS,
        manifest_content_hash=MANIFEST,
    )


class TestForwardReturnContract(OutcomeProviderContract):
    @pytest.fixture
    def outcome_subject(self) -> OutcomeSubject:
        return subject(lambda: ForwardReturnOutcome((FORWARD,)), FORWARD, FORWARD_OTHER)


class TestTripleBarrierContract(OutcomeProviderContract):
    @pytest.fixture
    def outcome_subject(self) -> OutcomeSubject:
        return subject(lambda: TripleBarrierOutcome((BARRIER,)), BARRIER, BARRIER_OTHER)


def _request(spec: OutcomeLabelSpec, events: tuple[OutcomeEvent, ...] = EVENTS) -> OutcomeRequest:
    return OutcomeRequest(
        label_spec=spec,
        manifest_content_hash=MANIFEST,
        price_cutoff=BARS[-1].available_time,
        events=events,
        bars=BARS,
    )


def test_forward_return_values_are_exact() -> None:
    result = ForwardReturnOutcome((FORWARD,)).compute(_request(FORWARD))
    by_key = {label.event_key: label for label in result.labels}
    e3 = by_key["e3"]
    assert e3.entry_time == T0 + 3 * MINUTE
    assert e3.exit_time == T0 + 13 * MINUTE
    assert e3.entry_price == BARS[3].open and e3.exit_price == BARS[12].close
    assert e3.value == quantize_return(BARS[12].close, BARS[3].open)
    assert e3.available_time == BARS[12].available_time
    assert by_key["e55"].value is None  # the window runs past the data: never filled
    mid = by_key["mid-bar"]
    assert mid.entry_time == T0 + 41 * MINUTE  # next bar after a mid-bar event


def test_triple_barrier_touches_and_vertical_exit() -> None:
    result = TripleBarrierOutcome((BARRIER,)).compute(_request(BARRIER))
    barriers = {label.event_key: label.barrier for label in result.labels}
    assert set(barriers.values()) - {None} <= {-1, 0, 1}
    assert any(label.barrier in (-1, 1) for label in result.labels)
    for label in result.labels:
        if label.barrier in (-1, 1):
            assert label.exit_time is not None and label.entry_time is not None
            assert label.exit_time <= label.entry_time + BARRIER.horizon


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
    spec = OutcomeLabelSpec.bind(
        outcome_spec("tb_1m", MINUTE),
        OutcomeMethod.TRIPLE_BARRIER,
        upper_barrier=Decimal("0.005"),
        lower_barrier=Decimal("0.005"),
    )
    request = OutcomeRequest(
        label_spec=spec,
        manifest_content_hash=MANIFEST,
        price_cutoff=wide.available_time,
        events=(OutcomeEvent(event_key="e", event_time=start),),
        bars=(wide,),
    )
    label = TripleBarrierOutcome((spec,)).compute(request).labels[0]
    assert label.barrier == -1
    assert label.exit_price == Decimal("99.500")


def test_a_gap_inside_the_window_is_not_filled() -> None:
    gapped = BARS[:6] + BARS[7:]
    request = OutcomeRequest(
        label_spec=FORWARD,
        manifest_content_hash=MANIFEST,
        price_cutoff=BARS[-1].available_time,
        events=(EVENTS[0],),
        bars=gapped,
    )
    assert ForwardReturnOutcome((FORWARD,)).compute(request).labels[0].value is None


# ======================================================================================
# Single-fault variants: the suite must reject each of them
# ======================================================================================


class PeeksPastHorizon(ForwardReturnOutcome):
    """Exits one bar after the horizon."""

    def _label(self, request: OutcomeRequest, event: OutcomeEvent) -> OutcomeLabel:
        spec = request.label_spec
        window = label_window(request.bars, event.event_time, spec.horizon + MINUTE)
        if window is None or not window.complete:
            return unknown(event)
        first, last = window.bars[0], window.bars[-1]
        return OutcomeLabel(
            event_key=event.event_key,
            event_time=event.event_time,
            value=quantize_return(last.close, first.open),
            entry_time=window.entry_time,
            exit_time=last.interval_end,
            entry_price=first.open,
            exit_price=last.close,
            available_time=last.available_time,
        )


class FillsPartialWindow(ForwardReturnOutcome):
    """Uses whatever part of the window exists instead of ``None``."""

    def _label(self, request: OutcomeRequest, event: OutcomeEvent) -> OutcomeLabel:
        window = label_window(request.bars, event.event_time, request.label_spec.horizon)
        if window is None or not window.bars:
            return unknown(event)
        first, last = window.bars[0], window.bars[-1]
        return OutcomeLabel(
            event_key=event.event_key,
            event_time=event.event_time,
            value=quantize_return(last.close, first.open),
            entry_time=window.entry_time,
            exit_time=last.interval_end,
            entry_price=first.open,
            exit_price=last.close,
            available_time=last.available_time,
        )


@pytest.mark.parametrize(
    ("variant", "check"),
    [
        (PeeksPastHorizon, outcome_suite.check_answers_match_the_request),
        (FillsPartialWindow, outcome_suite.check_answers_match_the_request),
        (FillsPartialWindow, outcome_suite.check_missing_data_is_explicit_none),
    ],
    ids=["peeks-past-horizon", "fills-partial-window", "fills-cut-window"],
)
def test_the_suite_kills_faulty_variants(
    variant: type[ForwardReturnOutcome], check: outcome_suite.OutcomeCheck
) -> None:
    faulty = subject(lambda: variant((FORWARD,)), FORWARD, FORWARD_OTHER)
    outcome_suite.check_descriptor_declares_the_spec(faulty)  # still compliant at the base
    with pytest.raises(ContractSuiteFailure):
        check(faulty)
