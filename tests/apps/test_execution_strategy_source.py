"""Phase 5 -> Phase 13 wiring (``apps.execution.strategy_source``): a ``StrategyProvider``
(+ optional ``RiskProvider``) as the execution service's ``TargetPositionSource``.

Smoke level: the adapter only ever sees signals visible at ``as_of`` (no future leak), fails closed
when the provider's answer does not pass ``StrategyResult.check_answers``, a ``RiskProvider``
visibly constrains the mapped quantity, and end to end through ``ExecutionService`` in SIMULATED
mode (``RiskLimits.from_risk_policy`` for second-line risk): LIVE is still refused and the kill
switch halts order flow.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.execution import (
    EquityPriceSizer,
    ExecutionReport,
    ExecutionService,
    KillSwitch,
    LinearCostModel,
    LiveExecutionRefused,
    Monitor,
    RejectionSource,
    RiskLimits,
    SecondLineRisk,
    SimulatedVenue,
    StrategyProviderTargetSource,
    StrategySourceRefused,
    TargetPosition,
    TargetPositions,
    instrument_key,
)
from core.contracts.strategy import SignalObservation
from core.domain.base import FrozenMapping
from core.domain.execution import ExecutionMode
from core.domain.specs import Instrument, InstrumentType, RiskPolicy
from core.lifecycle.strategy import LifecycleHistory, LifecycleState, LifecycleTransition
from infrastructure.event_bus import InMemoryEventBus
from tests import factories
from tests.fake_strategy import (
    SIGNAL_REF,
    FakeCapRisk,
    FakeSignStrategy,
    RequestShiftingStrategy,
    fake_cap_risk_policy,
    fake_strategy_spec,
)

T0 = datetime(2026, 2, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
AS_OF = T0 + 10 * MINUTE

BTC = Instrument(
    venue="sim", symbol="BTCUSDT", instrument_type=InstrumentType.SPOT, base="BTC", quote="USDT"
)
ETH = Instrument(
    venue="sim", symbol="ETHUSDT", instrument_type=InstrumentType.SPOT, base="ETH", quote="USDT"
)
KB, KE = instrument_key(BTC), instrument_key(ETH)
INSTRUMENTS = {KB: BTC, KE: ETH}

SPEC = fake_strategy_spec()
#: Equity 1000 at price 100: a weight w sizes to 10 * w units (never the weight itself).
EQUITY, PRICE = Decimal(1000), Decimal(100)
SIZER = EquityPriceSizer(equity=EQUITY, price=lambda _instrument, _as_of: PRICE)


def _signal(instrument: str, minutes_before: int, value: Decimal) -> SignalObservation:
    at = AS_OF - minutes_before * MINUTE
    return SignalObservation(
        signal=SIGNAL_REF,
        instrument=instrument,
        event_time=at,
        available_time=at,
        knowledge_time=at,
        value=value,
    )


#: BTC trends up, ETH trends down; one signal per instrument is in the future (available after
#: AS_OF) with the opposite sign, to prove the adapter never lets it leak in.
BASE_SIGNALS: tuple[SignalObservation, ...] = (
    _signal(KB, 3, Decimal("0.01")),
    _signal(KB, 2, Decimal("0.02")),
    _signal(KB, 1, Decimal("0.03")),
    _signal(KE, 3, Decimal("-0.01")),
    _signal(KE, 2, Decimal("-0.02")),
    _signal(KE, 1, Decimal("-0.03")),
    _signal(KB, -5, Decimal("-9")),  # available 5 minutes after AS_OF
    _signal(KE, -5, Decimal("9")),  # available 5 minutes after AS_OF
)


def _signals(
    feed: tuple[SignalObservation, ...],
) -> Callable[[str, datetime], tuple[SignalObservation, ...]]:
    return lambda _deployment_id, _as_of: feed


def _source(**overrides: object) -> StrategyProviderTargetSource:
    fields: dict[str, object] = {
        "strategy": FakeSignStrategy((SPEC,)),
        "strategy_spec": SPEC,
        "instruments": INSTRUMENTS,
        "signals": _signals(BASE_SIGNALS),
        "sizer": SIZER,
    }
    fields.update(overrides)
    return StrategyProviderTargetSource(**fields)  # type: ignore[arg-type]


def _quantity(targets: TargetPositions, instrument: Instrument) -> Decimal:
    for target in targets.targets:
        if target.instrument == instrument:
            return target.quantity
    raise AssertionError(f"no target for {instrument}")


# -- the adapter alone --------------------------------------------------------------------------


def test_maps_strategy_positions_to_target_positions() -> None:
    targets = _source().target_positions("dep-1", AS_OF)
    assert targets.deployment_id == "dep-1" and targets.as_of == AS_OF
    assert _quantity(targets, BTC) > 0  # BTC's visible trailing signal is positive
    assert _quantity(targets, ETH) < 0  # ETH's visible trailing signal is negative


def _negated(item: SignalObservation) -> SignalObservation:
    value = item.value
    assert isinstance(value, Decimal)
    return item.model_copy(update={"value": -value})


def test_future_signals_never_leak_into_the_target() -> None:
    base = _source().target_positions("dep-1", AS_OF)
    # Flip the *future* signals' sign; a leak would flip the resulting positions too.
    mutated = tuple(
        _negated(item) if item.available_time > AS_OF else item for item in BASE_SIGNALS
    )
    again = _source(signals=_signals(mutated)).target_positions("dep-1", AS_OF)
    assert again == base


def test_fails_closed_when_the_answer_does_not_pass_check_answers() -> None:
    broken = _source(strategy=RequestShiftingStrategy(FakeSignStrategy((SPEC,))))
    with pytest.raises(StrategySourceRefused, match="check_answers"):
        broken.target_positions("dep-1", AS_OF)


def test_risk_provider_visibly_constrains_the_mapped_quantity() -> None:
    cap_policy = fake_cap_risk_policy(cap="0.1")
    spec = fake_strategy_spec(name="fake_sign_capped", risk_policy=cap_policy.ref)
    unconstrained = _source(strategy=FakeSignStrategy((spec,)), strategy_spec=spec)
    constrained = _source(
        strategy=FakeSignStrategy((spec,)),
        strategy_spec=spec,
        risk=FakeCapRisk((cap_policy,)),
        risk_policy=cap_policy,
    )
    loose = unconstrained.target_positions("dep-1", AS_OF)
    capped = constrained.target_positions("dep-1", AS_OF)
    cap_units = Decimal("0.1") * EQUITY / PRICE
    assert abs(_quantity(loose, BTC)) > cap_units  # teeth: unconstrained exceeds the cap
    assert abs(_quantity(capped, BTC)) == cap_units
    assert abs(_quantity(capped, ETH)) == cap_units


# -- end to end through ExecutionService, SIMULATED mode -----------------------------------------


COSTS = LinearCostModel(fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.0005"))
SECOND_LINE_POLICY = RiskPolicy(
    name="second_line",
    version="1.0.0",
    created_at=T0,
    rules=("second_line",),
    params=FrozenMapping({"max_gross_exposure": "5000", "max_leverage": "2", "max_loss": "1000"}),
)


def _lifecycle(subject: object) -> LifecycleHistory:
    path = [
        LifecycleState.IDEA,
        LifecycleState.CANDIDATE,
        LifecycleState.VALIDATION,
        LifecycleState.OOS,
        LifecycleState.PAPER,
    ]
    history = LifecycleHistory(subject=subject)  # type: ignore[arg-type]
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False)):
        history = history.append(
            LifecycleTransition(
                subject=subject,  # type: ignore[arg-type]
                from_state=a,
                to_state=b,
                reason="test",
                evidence=("report:test",),
                triggered_by="test",
                approved_by=(
                    "raphael" if (a, b) == (LifecycleState.OOS, LifecycleState.PAPER) else None
                ),
                occurred_at=T0 + timedelta(minutes=i),
            )
        )
    return history


class _Rig:
    def __init__(self) -> None:
        limits = RiskLimits.from_risk_policy(SECOND_LINE_POLICY, capital=Decimal(10_000))
        self.bus = InMemoryEventBus()
        self.kill_switch = KillSwitch()
        self.risk = SecondLineRisk(limits)
        self.monitor = Monitor(capital=limits.capital, max_drawdown=Decimal(1_000_000))
        self.venue = SimulatedVenue(venue_id="sim-1", cost_model=COSTS)
        self.service = ExecutionService(
            mode=ExecutionMode.SIMULATED,
            venue=self.venue,
            kill_switch=self.kill_switch,
            risk=self.risk,
            monitor=self.monitor,
            bus=self.bus,
            clock=lambda: AS_OF,
        )
        self.artifact = factories.strategy_artifact(applicable_instruments=(BTC, ETH))
        self.deployment = factories.deployment_record(
            equivalence=factories.equivalence_check(artifact_id=self.artifact.artifact_id)
        )
        self.service.admit(self.deployment, self.artifact, _lifecycle(self.artifact.strategy_spec))
        self.source = _source()

    def run(self) -> ExecutionReport:
        return self.service.run_once(
            self.source, self.deployment.deployment_id, {KB: Decimal(100), KE: Decimal(50)}
        )


def test_end_to_end_admit_and_run_once_in_simulated_mode() -> None:
    rig = _Rig()
    report = rig.run()
    assert report.orders and report.fills and not report.rejections
    keys = {instrument_key(o.instrument) for o in report.orders}
    assert keys == {KB, KE}


def test_live_mode_is_still_refused() -> None:
    rig = _Rig()
    with pytest.raises(LiveExecutionRefused, match="SIMULATED only"):
        ExecutionService(
            mode=ExecutionMode.LIVE,
            venue=rig.venue,
            kill_switch=KillSwitch(),
            risk=rig.risk,
            monitor=rig.monitor,
            bus=rig.bus,
            clock=lambda: AS_OF,
        )


def test_kill_switch_halts_order_flow() -> None:
    rig = _Rig()
    first = rig.run()
    assert first.fills

    rig.service.trip_kill_switch(reason="test halt", tripped_by="test")
    # A different target (flatten) so the delta is non-zero and an order is actually attempted.
    flatten = TargetPositions(
        deployment_id=rig.deployment.deployment_id,
        as_of=AS_OF,
        targets=(
            TargetPosition(instrument=BTC, quantity=Decimal(0)),
            TargetPosition(instrument=ETH, quantity=Decimal(0)),
        ),
    )
    second = rig.service.submit_targets(flatten, {KB: Decimal(100), KE: Decimal(50)})
    assert not second.fills
    assert second.rejections and all(
        r.source is RejectionSource.KILL_SWITCH for r in second.rejections
    )


def test_weights_are_sized_by_equity_and_price_never_used_as_quantities() -> None:
    targets = _source().target_positions("dep-1", AS_OF)
    doubled = _source(
        sizer=EquityPriceSizer(equity=EQUITY * 2, price=lambda _i, _t: PRICE)
    ).target_positions("dep-1", AS_OF)
    for instrument in (BTC, ETH):
        assert _quantity(doubled, instrument) == 2 * _quantity(targets, instrument)
        assert _quantity(targets, instrument) != 0


@pytest.mark.parametrize("price", [None, Decimal(0), Decimal(-1), Decimal("NaN")])
def test_a_missing_or_non_positive_price_refuses_to_size(price: Decimal | None) -> None:
    source = _source(sizer=EquityPriceSizer(equity=EQUITY, price=lambda _i, _t: price))
    with pytest.raises(StrategySourceRefused, match="price"):
        source.target_positions("dep-1", AS_OF)


def test_the_sizer_refuses_a_non_positive_equity() -> None:
    with pytest.raises(ValueError, match="equity"):
        EquityPriceSizer(equity=Decimal(0), price=lambda _i, _t: PRICE)
