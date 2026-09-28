"""Composite strategies (ADR-0088 decision 2): contract suite, hand-computed conditioned /
ensemble / negated targets, missing and unknown states, insufficient history, no look-ahead, and
the construction refusals (self-reference, cycles, unresolved parts, uncovered signals, ensemble
members with different risk policy / applicable instruments, spot negation)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.feature import ObservationScalar
from core.contracts.strategy import (
    SignalObservation,
    StrategyInputError,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import (
    ConditionedStrategy,
    EnsembleStrategy,
    Instrument,
    InstrumentType,
    NegatedStrategy,
    StrategySpec,
)
from research.strategies.composite import CompositeStrategyProvider, ResolvedStrategy
from tests.contract_suites.strategy import StrategyProviderContract, StrategySubject
from tests.strategy_fixtures import MINUTE, T0

CUTOFF = T0 + timedelta(days=1)
SPEC_TIME = datetime(2026, 9, 28, tzinfo=UTC)
SIG_A = Ref(kind=Kind.FEATURE, name="echo_a", version="1.0.0")
SIG_B = Ref(kind=Kind.FEATURE, name="echo_b", version="1.0.0")
SIG_C = Ref(kind=Kind.FEATURE, name="echo_c", version="1.0.0")
REGIME = Ref(kind=Kind.STATE, name="regime", version="1.0.0")
BTC = "BTCUSDT"
ETH = "ETHUSDT"


def _plain(name: str, signal: Ref, **extra: Any) -> StrategySpec:
    return StrategySpec(
        name=name, version="1.0.0", created_at=SPEC_TIME, signals=(signal,), **extra
    )


def _composite(
    name: str,
    signals: tuple[Ref, ...],
    composition: ConditionedStrategy | EnsembleStrategy | NegatedStrategy,
) -> StrategySpec:
    return StrategySpec(
        name=name,
        version="1.0.0",
        created_at=SPEC_TIME,
        signals=signals,
        composition=composition,
    )


class EchoProvider:
    """Test double for an injected base strategy (not the logic under test): the target is the
    latest visible ``Decimal`` value of the spec's single signal for the instrument, else flat."""

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec
        self._descriptor = StrategyProviderDescriptor(
            name="test_echo",
            version="0.1.0",
            deterministic=True,
            supported_strategies=FrozenMapping({str(spec.ref): spec.content_hash()}),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        if not self._descriptor.supports(request.strategy, request.spec_hash):
            raise UnsupportedStrategy("not this echo spec")
        (signal,) = self.spec.signals
        positions: list[TargetPosition] = []
        for decision_time in request.decision_times:
            visible = request.visible_at(decision_time)
            for instrument in request.instruments:
                series = [
                    item
                    for item in visible
                    if item.instrument == instrument
                    and item.signal == signal
                    and isinstance(item.value, Decimal)
                ]
                if not series:
                    positions.append(
                        TargetPosition(
                            decision_time=decision_time,
                            instrument=instrument,
                            target_weight=Decimal(0),
                            inputs_used=0,
                        )
                    )
                    continue
                latest = max(series, key=lambda item: (item.event_time, item.available_time))
                assert isinstance(latest.value, Decimal)
                positions.append(
                    TargetPosition(
                        decision_time=decision_time,
                        instrument=instrument,
                        target_weight=latest.value,
                        inputs_used=1,
                        latest_input_available_time=latest.available_time,
                    )
                )
        return StrategyResult.build(request, self._descriptor, positions)


BASE_A = _plain("echo_a", SIG_A)
BASE_B = _plain("echo_b", SIG_B)
BASE_C = _plain("echo_c", SIG_C)
COND = _composite(
    "cond_a",
    (SIG_A, REGIME),
    ConditionedStrategy(base=BASE_A.ref, state=REGIME, state_value="up"),
)
ENS = _composite("ens_ab", (SIG_A, SIG_B), EnsembleStrategy(members=(BASE_A.ref, BASE_B.ref)))
ENS3 = _composite(
    "ens_abc",
    (SIG_A, SIG_B, SIG_C),
    EnsembleStrategy(members=(BASE_A.ref, BASE_B.ref, BASE_C.ref)),
)
NEG = _composite("neg_a", (SIG_A,), NegatedStrategy(base=BASE_A.ref))


def _table(*specs: StrategySpec) -> dict[str, ResolvedStrategy]:
    return {str(spec.ref): ResolvedStrategy(spec, EchoProvider(spec)) for spec in specs}


TABLE = _table(BASE_A, BASE_B, BASE_C)


def _provider(
    *specs: StrategySpec,
    table: dict[str, ResolvedStrategy] | None = None,
    instrument_type: InstrumentType = InstrumentType.PERPETUAL,
) -> CompositeStrategyProvider:
    return CompositeStrategyProvider(
        specs, resolution=TABLE if table is None else table, instrument_type=instrument_type
    )


def _obs(
    signal: Ref, instrument: str, minute: int, value: ObservationScalar | None
) -> SignalObservation:
    at = T0 + minute * MINUTE
    return SignalObservation(
        signal=signal,
        instrument=instrument,
        event_time=at,
        available_time=at,
        knowledge_time=at,
        value=value,
    )


def _request(
    spec: StrategySpec,
    signals: Sequence[SignalObservation],
    minutes: Sequence[int],
    *,
    instruments: tuple[str, ...] = (BTC,),
    params: dict[str, ObservationScalar] | None = None,
) -> StrategyRequest:
    return StrategyRequest(
        strategy=spec.ref,
        spec_hash=spec.content_hash(),
        params=FrozenMapping(params or {}),
        instruments=instruments,
        knowledge_cutoff=CUTOFF,
        decision_times=tuple(T0 + minute * MINUTE for minute in minutes),
        signals=tuple(signals),
    )


def _run(provider: CompositeStrategyProvider, request: StrategyRequest) -> StrategyResult:
    result = provider.target_positions(request)
    result.check_answers(request, provider.descriptor)
    return result


def _cells(result: StrategyResult) -> list[tuple[Decimal, int, datetime | None]]:
    return [
        (item.target_weight, item.inputs_used, item.latest_input_available_time)
        for item in result.positions
    ]


# ---------------------------------------------------------------------------------------------
# contract suite (conditioned and ensemble)
# ---------------------------------------------------------------------------------------------

WAVE = tuple(
    _obs(signal, instrument, minute, Decimal(minute % 5) / Decimal(4) - Decimal("0.5"))
    for minute in range(12)
    for instrument in (BTC, ETH)
    for signal in (SIG_A, SIG_B)
) + tuple(
    _obs(REGIME, instrument, minute, "up" if (minute // 3) % 2 == 0 else "down")
    for minute in range(12)
    for instrument in (BTC, ETH)
)


def _perturb(item: SignalObservation) -> SignalObservation:
    if isinstance(item.value, Decimal):
        return item.model_copy(update={"value": item.value + Decimal(7)})
    if isinstance(item.value, str):
        return item.model_copy(update={"value": "down" if item.value == "up" else "up"})
    return item


class TestConditionedContract(StrategyProviderContract):
    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        signals = tuple(item for item in WAVE if item.signal != SIG_B)
        return StrategySubject(
            open=lambda: _provider(COND),
            request=_request(COND, signals, range(0, 12, 2), instruments=(BTC, ETH)),
            unsupported=_request(
                COND, signals, range(0, 12, 2), instruments=(BTC, ETH), params={"lookback": 5}
            ),
            perturb=_perturb,
        )


class TestEnsembleContract(StrategyProviderContract):
    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        signals = tuple(item for item in WAVE if item.signal != REGIME)
        return StrategySubject(
            open=lambda: _provider(ENS),
            request=_request(ENS, signals, range(0, 12, 2), instruments=(BTC, ETH)),
            unsupported=_request(
                ENS, signals, range(0, 12, 2), instruments=(BTC, ETH), params={"lookback": 5}
            ),
            perturb=_perturb,
        )


# ---------------------------------------------------------------------------------------------
# conditioned
# ---------------------------------------------------------------------------------------------


def test_conditioned_hand_computed_gate() -> None:
    signals = (
        _obs(SIG_A, BTC, 0, Decimal("0.5")),
        _obs(SIG_A, BTC, 2, Decimal("0.25")),
        _obs(REGIME, BTC, 0, "up"),
        _obs(REGIME, BTC, 1, "down"),
        _obs(REGIME, BTC, 3, "up"),
        _obs(REGIME, BTC, 4, None),
    )
    result = _run(_provider(COND), _request(COND, signals, range(5)))
    assert _cells(result) == [
        # t0: state "up" -> base 0.5; the gate and the base observation are both inputs.
        (Decimal("0.5"), 2, T0),
        # t1: state "down" -> flat; the gate observation decided it.
        (Decimal(0), 1, T0 + MINUTE),
        # t2: still "down" although the base moved to 0.25.
        (Decimal(0), 1, T0 + MINUTE),
        # t3: "up" again -> base 0.25; latest input is the gate at t3.
        (Decimal("0.25"), 2, T0 + 3 * MINUTE),
        # t4: explicit None (unknown state) -> flat without inputs.
        (Decimal(0), 0, None),
    ]


def test_conditioned_missing_state_is_flat() -> None:
    signals = (_obs(SIG_A, BTC, 0, Decimal("0.5")), _obs(SIG_A, ETH, 0, Decimal("0.7")))
    result = _run(_provider(COND), _request(COND, signals, (0, 1), instruments=(BTC, ETH)))
    assert all(item.target_weight == 0 and item.inputs_used == 0 for item in result.positions)


def test_conditioned_state_is_per_instrument() -> None:
    signals = (
        _obs(SIG_A, BTC, 0, Decimal("0.5")),
        _obs(SIG_A, ETH, 0, Decimal("0.7")),
        _obs(REGIME, BTC, 0, "down"),
        _obs(REGIME, ETH, 0, "up"),
    )
    result = _run(_provider(COND), _request(COND, signals, (0,), instruments=(BTC, ETH)))
    assert [item.target_weight for item in result.positions] == [Decimal(0), Decimal("0.7")]


def test_conditioned_refuses_a_non_text_state_value() -> None:
    signals = (_obs(SIG_A, BTC, 0, Decimal("0.5")), _obs(REGIME, BTC, 0, Decimal(1)))
    with pytest.raises(StrategyInputError, match="state labels"):
        _provider(COND).target_positions(_request(COND, signals, (0,)))


# ---------------------------------------------------------------------------------------------
# ensemble
# ---------------------------------------------------------------------------------------------


def test_ensemble_hand_computed_equal_weight_mean() -> None:
    signals = (
        _obs(SIG_A, BTC, 0, Decimal("0.5")),
        _obs(SIG_B, BTC, 0, Decimal("0.2")),
        _obs(SIG_A, BTC, 1, Decimal(1)),
    )
    result = _run(_provider(ENS), _request(ENS, signals, (0, 1)))
    assert _cells(result) == [
        # (0.5 + 0.2) / 2
        (Decimal("0.35"), 2, T0),
        # (1 + 0.2) / 2: B's latest visible value is still the one at t0.
        (Decimal("0.6"), 2, T0 + MINUTE),
    ]


def test_ensemble_member_without_information_contributes_zero() -> None:
    signals = (_obs(SIG_A, BTC, 0, Decimal(1)),)
    result = _run(_provider(ENS), _request(ENS, signals, (0,)))
    assert _cells(result) == [(Decimal("0.5"), 1, T0)]


def test_ensemble_mean_is_rounded_half_even_at_18_places() -> None:
    signals = (
        _obs(SIG_A, BTC, 0, Decimal(1)),
        _obs(SIG_B, BTC, 0, Decimal(1)),
        _obs(SIG_C, BTC, 0, Decimal(0)),
    )
    result = _run(_provider(ENS3), _request(ENS3, signals, (0,)))
    assert result.positions[0].target_weight == Decimal("0.666666666666666667")
    assert result.positions[0].inputs_used == 3


def test_ensemble_refuses_members_with_different_risk_policy() -> None:
    risky = _plain("echo_c_risk", SIG_C, risk_policy=Ref(kind=Kind.RISK, name="r", version="1.0.0"))
    spec = _composite("ens_ac", (SIG_A, SIG_C), EnsembleStrategy(members=(BASE_A.ref, risky.ref)))
    with pytest.raises(ValueError, match="risk_policy"):
        _provider(spec, table=_table(BASE_A, risky))


def test_ensemble_refuses_members_with_different_applicable_instruments() -> None:
    btc = Instrument(
        venue="binance",
        symbol="BTCUSDT",
        instrument_type=InstrumentType.SPOT,
        base="BTC",
        quote="USDT",
    )
    scoped = _plain("echo_c_btc", SIG_C, applicable_instruments=(btc,))
    spec = _composite("ens_ac", (SIG_A, SIG_C), EnsembleStrategy(members=(BASE_A.ref, scoped.ref)))
    with pytest.raises(ValueError, match="applicable_instruments"):
        _provider(spec, table=_table(BASE_A, scoped))


# ---------------------------------------------------------------------------------------------
# negated
# ---------------------------------------------------------------------------------------------


def test_negated_hand_computed_sign_flip() -> None:
    signals = (_obs(SIG_A, BTC, 0, Decimal("0.5")), _obs(SIG_A, BTC, 1, Decimal("-0.25")))
    result = _run(_provider(NEG), _request(NEG, signals, (0, 1, 2)))
    assert _cells(result) == [
        (Decimal("-0.5"), 1, T0),
        (Decimal("0.25"), 1, T0 + MINUTE),
        (Decimal("0.25"), 1, T0 + MINUTE),
    ]


def test_negated_refuses_a_short_target_in_a_spot_context() -> None:
    signals = (_obs(SIG_A, BTC, 0, Decimal("0.5")),)
    provider = _provider(NEG, instrument_type=InstrumentType.SPOT)
    with pytest.raises(UnsupportedStrategy, match="ST-4"):
        provider.target_positions(_request(NEG, signals, (0,)))


def test_negated_in_spot_passes_flat_and_long_targets() -> None:
    signals = (_obs(SIG_A, BTC, 0, Decimal(0)), _obs(SIG_A, BTC, 1, Decimal("-0.4")))
    provider = _provider(NEG, instrument_type=InstrumentType.SPOT)
    result = _run(provider, _request(NEG, signals, (0, 1)))
    assert [item.target_weight for item in result.positions] == [Decimal(0), Decimal("0.4")]
    assert not result.positions[0].target_weight.is_signed()


# ---------------------------------------------------------------------------------------------
# history, look-ahead, nesting
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [COND, ENS, NEG], ids=lambda spec: spec.name)
def test_insufficient_history_is_flat_without_inputs(spec: StrategySpec) -> None:
    signals = (_obs(SIG_A, BTC, 5, Decimal("0.5")), _obs(REGIME, BTC, 5, "up"))
    wanted = {signal.target_identity() for signal in spec.signals}
    own = tuple(item for item in signals if item.signal.target_identity() in wanted)
    result = _run(_provider(spec), _request(spec, own, (0, 1, 2)))
    assert _cells(result) == [(Decimal(0), 0, None)] * 3


@pytest.mark.parametrize("spec", [COND, ENS, NEG], ids=lambda spec: spec.name)
def test_future_perturbation_does_not_move_past_targets(spec: StrategySpec) -> None:
    wanted = {signal.target_identity() for signal in spec.signals}
    signals = tuple(item for item in WAVE if item.signal.target_identity() in wanted)
    cut = T0 + 5 * MINUTE
    moved = tuple(_perturb(item) if item.available_time > cut else item for item in signals)
    minutes = range(12)
    base = _run(_provider(spec), _request(spec, signals, minutes, instruments=(BTC, ETH)))
    other = _run(_provider(spec), _request(spec, moved, minutes, instruments=(BTC, ETH)))
    early = [item for item in base.positions if item.decision_time <= cut]
    assert early == [item for item in other.positions if item.decision_time <= cut]
    assert base.positions != other.positions  # the perturbation is visible later


def test_nested_composite_through_an_injected_composite_provider() -> None:
    inner = _provider(COND)
    table = {
        **_table(BASE_B),
        str(COND.ref): ResolvedStrategy(COND, inner),
    }
    outer = _composite(
        "ens_cond_b", (SIG_A, REGIME, SIG_B), EnsembleStrategy(members=(COND.ref, BASE_B.ref))
    )
    signals = (
        _obs(SIG_A, BTC, 0, Decimal("0.5")),
        _obs(REGIME, BTC, 0, "up"),
        _obs(SIG_B, BTC, 0, Decimal("0.1")),
    )
    result = _run(_provider(outer, table=table), _request(outer, signals, (0,)))
    # (0.5 [gated in] + 0.1) / 2; inputs: gate + A + B.
    assert _cells(result) == [(Decimal("0.3"), 3, T0)]


# ---------------------------------------------------------------------------------------------
# construction and request refusals
# ---------------------------------------------------------------------------------------------


def test_self_reference_is_refused_by_the_contract_and_the_provider() -> None:
    own = Ref(kind=Kind.STRATEGY, name="neg_self", version="1.0.0")
    with pytest.raises(ValidationError):
        _composite("neg_self", (SIG_A,), NegatedStrategy(base=own))
    forged = StrategySpec.model_construct(
        name="neg_self",
        version="1.0.0",
        created_at=SPEC_TIME,
        signals=(SIG_A,),
        composition=NegatedStrategy(base=own),
    )
    with pytest.raises(ValueError, match="cycle"):
        _provider(forged)


def test_reference_cycle_is_refused() -> None:
    x_ref = Ref(kind=Kind.STRATEGY, name="neg_x", version="1.0.0")
    y_spec = _composite("neg_y", (SIG_A,), NegatedStrategy(base=x_ref))
    x_spec = _composite("neg_x", (SIG_A,), NegatedStrategy(base=y_spec.ref))
    table = {str(y_spec.ref): ResolvedStrategy(y_spec, EchoProvider(y_spec))}
    with pytest.raises(ValueError, match="cycle"):
        _provider(x_spec, table=table)


def test_unresolved_reference_is_refused() -> None:
    with pytest.raises(ValueError, match="resolution table"):
        _provider(ENS, table=_table(BASE_A))


def test_signals_must_cover_the_referenced_strategies() -> None:
    spec = _composite(
        "cond_uncovered",
        (SIG_B, REGIME),
        ConditionedStrategy(base=BASE_A.ref, state=REGIME, state_value="up"),
    )
    with pytest.raises(ValueError, match="do not cover"):
        _provider(spec)


def test_a_composite_has_no_parameters_of_its_own() -> None:
    spec = StrategySpec(
        name="neg_params",
        version="1.0.0",
        created_at=SPEC_TIME,
        signals=(SIG_A,),
        params=FrozenMapping({"lookback": 5}),
        param_search_space=FrozenMapping({"lookback": (5,)}),
        composition=NegatedStrategy(base=BASE_A.ref),
    )
    with pytest.raises(ValueError, match="no parameters"):
        _provider(spec)


def test_a_plain_spec_is_not_served() -> None:
    with pytest.raises(ValueError, match="no composition"):
        _provider(BASE_A)


def test_resolution_key_must_name_its_spec() -> None:
    table = {str(BASE_B.ref): ResolvedStrategy(BASE_A, EchoProvider(BASE_A))}
    with pytest.raises(ValueError, match="does not name"):
        _provider(NEG, table=table)


def test_resolution_provider_must_support_the_spec_hash() -> None:
    table = {str(BASE_A.ref): ResolvedStrategy(BASE_A, EchoProvider(BASE_B))}
    with pytest.raises(ValueError, match="does not support"):
        _provider(NEG, table=table)


def test_request_refusals() -> None:
    provider = _provider(NEG)
    signals = (_obs(SIG_A, BTC, 0, Decimal("0.5")),)
    with pytest.raises(UnsupportedStrategy):
        provider.target_positions(_request(NEG, signals, (0,), params={"lookback": 5}))
    with pytest.raises(StrategyInputError, match="does not consume"):
        provider.target_positions(_request(NEG, (*signals, _obs(SIG_B, BTC, 0, Decimal(1))), (0,)))
    with pytest.raises(UnsupportedStrategy):
        provider.target_positions(_request(COND, signals, (0,)))


def test_instrument_type_and_specs_are_explicit() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CompositeStrategyProvider((), resolution=TABLE, instrument_type=InstrumentType.SPOT)
    with pytest.raises(ValueError, match="InstrumentType"):
        CompositeStrategyProvider(
            (NEG,),
            resolution=TABLE,
            instrument_type="spot",  # type: ignore[arg-type]
        )
