"""Opt-in execution realism for ``BarBacktester``: participation cap, square-root impact, funding.

The default constructor must stay byte-identical to v1 (golden ``result_hash`` values recorded
from the v1 backtester at 9c0b851 before the variant existed); every new behaviour is switched on
only by an explicit ``ExecutionModel`` parameter. Parameters here are TEST ONLY values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from functools import partial

import pytest

from core.contracts.strategy import (
    BacktestCostModel,
    BacktestInputError,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    TargetPosition,
)
from plugins.backtest import (
    EXECUTION_VERSION,
    MONEY_QUANTUM,
    BarBacktester,
    ExecutionModel,
    ExecutionReport,
)
from tests.contract_suites import backtest as backtest_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.backtest import BacktestProviderContract, BacktestSubject
from tests.contract_version_support import at_pre_bump, built_at_pre_bump
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes

ZERO = backtest_suite.ZERO_COST
TOLERANCE = MONEY_QUANTUM * 1000


def _target(
    minute: int, weight: str, instrument: str = "BTCUSDT", offset: timedelta = timedelta(0)
) -> TargetPosition:
    decided = T0 + minute * MINUTE + offset
    return TargetPosition(
        decision_time=decided,
        instrument=instrument,
        target_weight=Decimal(weight),
        inputs_used=1,
        latest_input_available_time=decided,
    )


def _volumes(bars: tuple[PriceBar, ...], volume: str) -> dict[tuple[str, datetime], Decimal]:
    return {(bar.instrument, bar.interval_start): Decimal(volume) for bar in bars}


def _flat(count: int, price: str = "100") -> tuple[PriceBar, ...]:
    return make_bars("BTCUSDT", (Decimal(price),) * count)


def _request(
    bars: tuple[PriceBar, ...],
    targets: tuple[TargetPosition, ...],
    costs: BacktestCostModel = ZERO,
    equity: str = "10000",
) -> BacktestRequest:
    return BacktestRequest(
        cost_model=costs, initial_equity=Decimal(equity), bars=bars, targets=targets
    )


def _run(
    backtester: BarBacktester, request: BacktestRequest
) -> tuple[BacktestResult, ExecutionReport]:
    result, report = backtester.run_with_report(request)
    BacktestResult.model_validate(result.model_dump())
    result.check_answers(request, backtester.descriptor)
    assert report.result_hash == result.result_hash
    return result, report


# ----------------------------------------------------------------------------------------
# 1. the default constructor is byte-identical to v1
# ----------------------------------------------------------------------------------------

_BTC = make_bars("BTCUSDT", wave_closes(12))
_ETH = make_bars("ETHUSDT", wave_closes(12, phase=10))


def _golden_requests() -> dict[str, BacktestRequest]:
    third, half = MINUTE / 3, MINUTE / 2
    return {
        "alternating": _request(
            _BTC, tuple(_target(i, "1" if i % 2 == 0 else "-1") for i in range(12)), COSTS
        ),
        "two_instruments": _request(
            _BTC + _ETH,
            (
                _target(0, "0.5"),
                _target(0, "0.5", "ETHUSDT"),
                _target(5, "-0.25"),
                _target(7, "0.75", "ETHUSDT"),
            ),
            COSTS,
            "1000",
        ),
        "leverage_and_gaps": _request(
            _BTC,
            (
                _target(0, "2", offset=third),
                _target(0, "-1.5", offset=half),
                _target(4, "-1.5"),
                _target(9, "0"),
                _target(30, "1"),
            ),
            COSTS,
            "1000",
        ),
    }


#: ``result_hash`` of each request under the v1 backtester, recorded before the variant existed.
_GOLDEN = {
    "alternating": "fada3325c90eff57fb8c16cb2c566e75daab6b3243c841e162b0039b2f97cdd9",
    "two_instruments": "766b48ff30d19bebd225126fe1d2753f007065dbcd7c351c6431653003ba7dba",
    "leverage_and_gaps": "ab8bd3072cdf780ada18cd2ec67eecb7c9cb10b54dded4414c6ad3986175ca92",
}


@pytest.mark.parametrize("name", sorted(_GOLDEN))
@pytest.mark.parametrize(
    "factory", [BarBacktester, partial(BarBacktester, execution=None)], ids=["bare", "none"]
)
def test_the_default_backtester_is_byte_identical_to_v1(
    name: str, factory: Callable[[], BarBacktester]
) -> None:
    # Recorded at contract 2.0.0: rebuilt exactly as the 2.0.0 code built it (every envelope
    # 2.0.0, ADR-0052 §4), the v1 result is still byte-identical to the pin.
    with built_at_pre_bump():
        backtester = factory()
        request = at_pre_bump(_golden_requests()[name])
        assert backtester.descriptor.version == "1.0.0"
        result, report = _run(backtester, request)
        assert result.result_hash == _GOLDEN[name]
        assert backtester.run(request) == result
    assert report.execution_fingerprint is None
    assert (report.fills, report.remainders, report.funding) == ((), (), ())
    assert report.total_impact == 0 and report.total_funding == 0


# ----------------------------------------------------------------------------------------
# 2. the backtest contract suite for the variant
# ----------------------------------------------------------------------------------------

_SUITE_BARS = make_bars("BTCUSDT", wave_closes(12))


class TestExecutionVariantContract(BacktestProviderContract):
    """The full suite, cap engaged but not binding, short borrow on: the closed forms still hold
    (the buy-and-hold / round-trip benchmarks hold no short and never exceed the cap)."""

    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        model = ExecutionModel(
            max_participation_rate=Decimal("0.5"),
            bar_volume=_volumes(_SUITE_BARS, "1000000"),
            short_borrow_rate=Decimal("0.0001"),
        )
        return BacktestSubject(
            open=partial(BarBacktester, execution=model),
            bars=_SUITE_BARS,
            costs=COSTS,
            initial_equity=Decimal("10000"),
            tolerance=TOLERANCE,
        )


#: Checks whose statement does not depend on the v1 cost semantics.
_AGNOSTIC = (
    backtest_suite.check_descriptor,
    backtest_suite.check_determinism,
    backtest_suite.check_zero_positions_zero_pnl,
    backtest_suite.check_no_look_ahead,
)

_ACTIVE_MODELS = {
    # binding cap (100 units wanted, 0.5 x 60 = 30 per bar) + impact + both funding legs
    "all_active": ExecutionModel(
        max_participation_rate=Decimal("0.5"),
        impact_coefficient=Decimal("0.1"),
        short_borrow_rate=Decimal("0.0001"),
        cash_borrow_rate=Decimal("0.0002"),
        bar_volume=_volumes(_SUITE_BARS, "60"),
    ),
    "cap_and_impact": ExecutionModel(
        max_participation_rate=Decimal("0.5"),
        impact_coefficient=Decimal("0.1"),
        bar_volume=_volumes(_SUITE_BARS, "60"),
    ),
}


def _subject(model: ExecutionModel) -> BacktestSubject:
    return BacktestSubject(
        open=partial(BarBacktester, execution=model),
        bars=_SUITE_BARS,
        costs=COSTS,
        initial_equity=Decimal("10000"),
        tolerance=TOLERANCE,
    )


@pytest.mark.parametrize("check", _AGNOSTIC, ids=lambda check: check.__name__)
@pytest.mark.parametrize("model", sorted(_ACTIVE_MODELS))
def test_active_variants_pass_the_semantics_agnostic_checks(
    model: str, check: backtest_suite.BacktestCheck
) -> None:
    check(_subject(_ACTIVE_MODELS[model]))


def test_impact_and_a_binding_cap_keep_the_round_trip_identity() -> None:
    # flat prices: the round trip still loses exactly fees + slippage (impact is in slippage)
    backtest_suite.check_round_trip_on_flat_prices_costs_only(
        _subject(_ACTIVE_MODELS["cap_and_impact"])
    )


def test_the_v1_closed_forms_do_not_hold_once_impact_or_the_cap_binds() -> None:
    # evidence that the variant really changes execution: the v1 benchmarks now fail
    subject = _subject(_ACTIVE_MODELS["cap_and_impact"])
    with pytest.raises(ContractSuiteFailure):
        backtest_suite.check_buy_and_hold_with_costs(subject)
    with pytest.raises(ContractSuiteFailure):
        backtest_suite.check_buy_and_hold_is_price_ratio(subject)


# ----------------------------------------------------------------------------------------
# 3. identity: every parameter and every volume is bound into provider_hash
# ----------------------------------------------------------------------------------------


def test_every_parameter_and_volume_is_bound_into_the_provider_hash() -> None:
    bars = _flat(3)
    half, ten = Decimal("0.5"), _volumes(bars, "10")
    variants = [
        ExecutionModel(max_participation_rate=half, bar_volume=ten),
        ExecutionModel(max_participation_rate=Decimal("0.4"), bar_volume=ten),
        ExecutionModel(max_participation_rate=half, bar_volume=_volumes(bars, "11")),
        ExecutionModel(
            max_participation_rate=half, bar_volume=ten, impact_coefficient=Decimal("0.1")
        ),
        ExecutionModel(
            max_participation_rate=half, bar_volume=ten, short_borrow_rate=Decimal("0.001")
        ),
        ExecutionModel(
            max_participation_rate=half, bar_volume=ten, cash_borrow_rate=Decimal("0.001")
        ),
    ]
    descriptors = [BarBacktester(execution=model).descriptor for model in variants]
    hashes = {d.content_hash() for d in descriptors} | {BarBacktester().descriptor.content_hash()}
    assert len(hashes) == len(variants) + 1
    for model, descriptor in zip(variants, descriptors, strict=True):
        assert descriptor.version == f"{EXECUTION_VERSION}+exec.{model.fingerprint}"
        assert descriptor.execution_model == "next_bar_open"
        assert descriptor.simulation_only is True
    # equal parameters written differently are one model
    same = ExecutionModel(max_participation_rate=Decimal("0.50"), bar_volume=_volumes(bars, "10.0"))
    assert same.fingerprint == variants[0].fingerprint


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({}, ValueError),  # nothing switched on: use BarBacktester()
        ({"max_participation_rate": Decimal("0.5")}, ValueError),  # cap without volume
        ({"impact_coefficient": Decimal("0.1")}, ValueError),  # impact without volume
        ({"short_borrow_rate": Decimal("0.1"), "bar_volume": {}}, ValueError),  # unused volume
        ({"max_participation_rate": 0.5, "bar_volume": {}}, TypeError),  # float
        ({"short_borrow_rate": 1}, TypeError),  # int
        ({"max_participation_rate": Decimal(0), "bar_volume": {}}, ValueError),
        ({"max_participation_rate": Decimal("1.1"), "bar_volume": {}}, ValueError),
        ({"impact_coefficient": Decimal("-0.1"), "bar_volume": {}}, ValueError),
        ({"short_borrow_rate": Decimal(1)}, ValueError),
        ({"cash_borrow_rate": Decimal(-1)}, ValueError),
        ({"cash_borrow_rate": Decimal("NaN")}, ValueError),
        (
            {"max_participation_rate": Decimal("0.5"), "bar_volume": {("BTCUSDT", T0): -1}},
            TypeError,
        ),
        (
            {
                "max_participation_rate": Decimal("0.5"),
                "bar_volume": {("BTCUSDT", T0): Decimal(-1)},
            },
            ValueError,
        ),
        (
            {
                "max_participation_rate": Decimal("0.5"),
                "bar_volume": {("BTCUSDT", T0.replace(tzinfo=None)): Decimal(1)},
            },
            ValueError,
        ),
    ],
)
def test_the_execution_model_rejects_implicit_or_invalid_parameters(
    kwargs: Mapping[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        ExecutionModel(**kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------
# 4. participation cap
# ----------------------------------------------------------------------------------------


def _capped(
    volume: str = "40", rate: str = "0.5", bars: int = 8
) -> tuple[BarBacktester, tuple[PriceBar, ...]]:
    flat = _flat(bars)
    model = ExecutionModel(max_participation_rate=Decimal(rate), bar_volume=_volumes(flat, volume))
    return BarBacktester(execution=model), flat


def test_a_reissued_target_is_filled_in_capped_slices_that_sum_to_it() -> None:
    backtester, bars = _capped()  # 10000 at 100 = 100 units wanted, 0.5 x 40 = 20 per bar
    targets = tuple(_target(i, "1") for i in range(len(bars)))
    result, report = _run(backtester, _request(bars, targets))
    assert [fill.quantity for fill in result.fills] == [Decimal(20)] * 5
    assert sum((fill.quantity for fill in result.fills), Decimal(0)) == Decimal(100)
    assert [fill.fill_time for fill in result.fills] == [T0 + i * MINUTE for i in range(5)]
    assert [r.cancelled_quantity for r in report.remainders] == [
        Decimal(80),
        Decimal(60),
        Decimal(40),
        Decimal(20),
    ]
    assert all(r.filled_quantity == 20 and r.bar_volume == 40 for r in report.remainders)
    assert result.unexecuted_targets == 0  # the last three targets had nothing left to trade
    assert result.equity_curve[-1].gross_exposure == Decimal(10000)


def test_a_single_target_is_cut_at_its_bar_and_the_remainder_is_reported_not_carried() -> None:
    # the frozen contract pins a target's fill to its execution bar (check_answers)
    backtester, bars = _capped()
    result, report = _run(backtester, _request(bars, (_target(0, "1"),)))
    assert [(fill.fill_time, fill.quantity) for fill in result.fills] == [(T0, Decimal(20))]
    (remainder,) = report.remainders
    assert (remainder.desired_quantity, remainder.cancelled_quantity) == (Decimal(100), Decimal(80))
    assert remainder.decision_time == T0 and remainder.bar_time == T0


def test_a_changed_target_re_targets_against_the_actual_holdings() -> None:
    backtester, bars = _capped()
    targets = tuple(_target(i, "1") for i in range(3)) + tuple(
        _target(i, "-0.3") for i in range(3, 8)
    )
    result, _ = _run(backtester, _request(bars, targets))
    # 3 x 20 bought, then 60 -> -30: sells of 20 until the short target is reached
    assert [fill.quantity for fill in result.fills] == [Decimal(20)] * 3 + [Decimal(-20)] * 4 + [
        Decimal(-10)
    ]
    assert sum((fill.quantity for fill in result.fills), Decimal(0)) == Decimal(-30)


def test_zero_volume_fills_nothing_and_counts_the_target_unexecuted() -> None:
    backtester, bars = _capped(volume="0")
    result, report = _run(backtester, _request(bars, (_target(0, "1"),)))
    assert result.fills == () and result.unexecuted_targets == 1
    assert report.remainders[0].filled_quantity == 0
    assert result.final_equity == Decimal(10000)


def test_a_missing_volume_is_refused_not_filled_in() -> None:
    bars = _flat(4)
    volumes = _volumes(bars[:2], "40")
    backtester = BarBacktester(
        execution=ExecutionModel(max_participation_rate=Decimal("0.5"), bar_volume=volumes)
    )
    with pytest.raises(BacktestInputError, match="no bar volume"):
        backtester.run(_request(bars, (_target(3, "1"),)))
    # a bar without a trade needs no volume
    result, _ = _run(backtester, _request(bars, (_target(0, "0.1"), _target(3, "0.1"))))
    assert len(result.fills) == 1


# ----------------------------------------------------------------------------------------
# 5. square-root impact (the G4 capacity check's law)
# ----------------------------------------------------------------------------------------


def _impact_backtester(bars: tuple[PriceBar, ...], coefficient: str = "0.2") -> BarBacktester:
    return BarBacktester(
        execution=ExecutionModel(
            impact_coefficient=Decimal(coefficient), bar_volume=_volumes(bars, "1000")
        )
    )


def _first_fill_price(
    backtester: BarBacktester, bars: tuple[PriceBar, ...], weight: str
) -> Decimal:
    result, _ = _run(backtester, _request(bars, (_target(0, weight),), COSTS))
    return result.fills[0].fill_price


def test_impact_raises_buys_and_lowers_sells_monotonically_in_size() -> None:
    bars = _flat(3)
    backtester = _impact_backtester(bars)
    sizes = ["0.1", "0.25", "0.5", "1", "2"]
    buys = [_first_fill_price(backtester, bars, size) for size in sizes]
    sells = [_first_fill_price(backtester, bars, f"-{size}") for size in sizes]
    slip = COSTS.slippage_rate
    assert buys[0] > Decimal(100) * (1 + slip)
    assert sells[0] < Decimal(100) * (1 - slip)
    assert all(a < b for a, b in zip(buys, buys[1:], strict=False))
    assert all(a > b for a, b in zip(sells, sells[1:], strict=False))


def test_impact_is_coefficient_times_sqrt_participation_and_part_of_slippage() -> None:
    bars = _flat(3)
    result, report = _run(_impact_backtester(bars), _request(bars, (_target(0, "1"),), COSTS))
    fill, detail = result.fills[0], report.fills[0]
    with localcontext() as ctx:
        ctx.prec = 50
        participation = Decimal(100) / Decimal(1000)  # 100 units against 1000
        impact = Decimal("0.2") * participation.sqrt()
        expected_price = Decimal(100) * (1 + COSTS.slippage_rate + impact)
        expected_impact = (Decimal(100) * Decimal(100) * impact).quantize(MONEY_QUANTUM)
        expected_slippage = (abs(fill.quantity) * (fill.fill_price - 100)).quantize(MONEY_QUANTUM)
    assert detail.participation == participation
    assert fill.fill_price == expected_price
    assert detail.impact_cost == expected_impact
    assert report.total_impact == detail.impact_cost
    assert fill.slippage_cost == expected_slippage
    assert fill.slippage_cost > detail.impact_cost > 0


def test_zero_impact_coefficient_prices_like_v1() -> None:
    bars = _flat(3)
    request = _request(bars, (_target(0, "1"), _target(1, "-1")), COSTS)
    variant, _ = _run(_impact_backtester(bars, "0"), request)
    v1 = BarBacktester().run(request)
    assert [f.fill_price for f in variant.fills] == [f.fill_price for f in v1.fills]
    assert variant.final_equity == v1.final_equity
    assert variant.provider_hash != v1.provider_hash  # still a distinguishable provider


def test_an_impact_that_would_make_a_sell_price_non_positive_is_refused() -> None:
    bars = _flat(3)
    backtester = BarBacktester(
        execution=ExecutionModel(impact_coefficient=Decimal(5), bar_volume=_volumes(bars, "1"))
    )
    with pytest.raises(BacktestInputError, match="zero or below"):
        backtester.run(_request(bars, (_target(0, "-1"),)))


def test_impact_against_zero_volume_without_a_cap_is_refused() -> None:
    bars = _flat(3)
    backtester = BarBacktester(
        execution=ExecutionModel(impact_coefficient=Decimal("0.1"), bar_volume=_volumes(bars, "0"))
    )
    with pytest.raises(BacktestInputError, match="zero bar volume"):
        backtester.run(_request(bars, (_target(0, "1"),)))


# ----------------------------------------------------------------------------------------
# 6. funding
# ----------------------------------------------------------------------------------------


def _funded(short: Decimal | None = None, cash: Decimal | None = None) -> BarBacktester:
    return BarBacktester(execution=ExecutionModel(short_borrow_rate=short, cash_borrow_rate=cash))


def test_short_borrow_reduces_equity_for_shorts_by_rate_times_short_notional() -> None:
    bars = _flat(6)
    request = _request(bars, (_target(0, "-1"),))
    rate = Decimal("0.001")
    result, report = _run(_funded(short=rate), request)
    baseline = BarBacktester().run(request)
    assert baseline.final_equity == Decimal(10000)
    # short 100 units at a flat 100: 10000 short notional, charged on each of the 6 bar steps
    assert [charge.cost for charge in report.funding] == [Decimal(10)] * 6
    assert result.final_equity == Decimal(10000) - 6 * Decimal(10)
    assert report.total_funding == baseline.final_equity - result.final_equity
    equities = [point.equity for point in result.equity_curve]
    assert all(a > b for a, b in zip(equities, equities[1:], strict=False))
    # funding is not a fill cost
    assert result.total_fees == 0 and result.total_slippage == 0


def test_short_borrow_does_not_charge_a_long_book() -> None:
    bars = _flat(4)
    result, report = _run(_funded(short=Decimal("0.001")), _request(bars, (_target(0, "1"),)))
    assert report.funding == () and result.final_equity == Decimal(10000)


def test_cash_borrow_charges_leverage_on_the_negative_cash() -> None:
    bars = _flat(4)
    result, report = _run(_funded(cash=Decimal("0.001")), _request(bars, (_target(0, "2"),)))
    # 200 units at 100 on 10000: cash -10000 at the first step, then a little more each step
    first = report.funding[0]
    assert (first.borrowed_cash, first.cost) == (Decimal(10000), Decimal(10))
    assert [charge.borrowed_cash for charge in report.funding] == sorted(
        charge.borrowed_cash for charge in report.funding
    )
    assert result.final_equity == Decimal(10000) - report.total_funding
    assert result.final_equity < Decimal(10000)


# ----------------------------------------------------------------------------------------
# 7. no future bar is used
# ----------------------------------------------------------------------------------------


def test_no_future_price_or_volume_changes_anything_up_to_t() -> None:
    bars = make_bars("BTCUSDT", wave_closes(10))
    targets = tuple(_target(i, "1.5" if i % 3 else "-1") for i in range(10))
    cut = 5

    def model(volumes: Mapping[tuple[str, datetime], Decimal]) -> BarBacktester:
        return BarBacktester(
            execution=ExecutionModel(
                max_participation_rate=Decimal("0.5"),
                impact_coefficient=Decimal("0.1"),
                short_borrow_rate=Decimal("0.0001"),
                cash_borrow_rate=Decimal("0.0002"),
                bar_volume=volumes,
            )
        )

    volumes = _volumes(bars, "150")
    later_volumes = {
        key: (value if key[1] < bars[cut].interval_start else value * 7)
        for key, value in volumes.items()
    }
    later_prices = bars[:cut] + tuple(
        bar.model_copy(
            update={name: getattr(bar, name) * 3 for name in ("open", "high", "low", "close")}
        )
        for bar in bars[cut:]
    )
    base, base_report = _run(model(volumes), _request(bars, targets, COSTS))
    runs = [
        _run(model(later_volumes), _request(bars, targets, COSTS)),
        _run(model(volumes), _request(later_prices, targets, COSTS)),
    ]
    horizon = bars[cut - 1].interval_end
    before = bars[cut].interval_start
    assert base_report.remainders  # the cap binds, so volumes matter
    for moved, moved_report in runs:
        assert [p for p in moved.equity_curve if p.time <= horizon] == [
            p for p in base.equity_curve if p.time <= horizon
        ]
        assert [f for f in moved.fills if f.fill_time < before] == [
            f for f in base.fills if f.fill_time < before
        ]
        assert [r for r in moved_report.remainders if r.bar_time < before] == [
            r for r in base_report.remainders if r.bar_time < before
        ]
        assert [c for c in moved_report.funding if c.time < before] == [
            c for c in base_report.funding if c.time < before
        ]
        assert moved.result_hash != base.result_hash
    assert all(fill.fill_time >= fill.decision_time for fill in base.fills)
