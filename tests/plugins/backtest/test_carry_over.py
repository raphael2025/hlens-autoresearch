"""ADR-0054: partial-fill carry-over (``next_bar_open_participation``) for ``BarBacktester``.

The pre-existing models must stay bit-identical: the golden values below were recorded at 50a43a4,
before the contract extension (the v1 ``result_hash`` values themselves are the 9c0b851 goldens in
``test_execution_model.py``). Parameters here are TEST ONLY values.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from functools import partial

import pytest

from core.contracts.strategy import (
    BacktestCostModel,
    BacktestInputError,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    Fill,
    FillRemainder,
    PriceBar,
    TargetPosition,
)
from plugins.backtest import (
    CARRY_OVER_VERSION,
    MONEY_QUANTUM,
    BarBacktester,
    ExecutionModel,
    ExecutionReport,
)
from tests.contract_suites import backtest as backtest_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.backtest import (
    BacktestSubject,
    CarryOverBacktestProviderContract,
)
from tests.contract_version_support import at_pre_bump, built_at_pre_bump
from tests.plugins.backtest.test_execution_model import (
    _ACTIVE_MODELS,
    _SUITE_BARS,
    _golden_requests,
)
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes

ZERO = backtest_suite.ZERO_COST
TOLERANCE = MONEY_QUANTUM * 1000


def _target(minute: int, weight: str, instrument: str = "BTCUSDT") -> TargetPosition:
    decided = T0 + minute * MINUTE
    return TargetPosition(
        decision_time=decided,
        instrument=instrument,
        target_weight=Decimal(weight),
        inputs_used=1,
        latest_input_available_time=decided,
    )


def _with_volume(bars: tuple[PriceBar, ...], *volumes: str) -> tuple[PriceBar, ...]:
    """Each bar gets the next volume (the last one repeats); ``"-"`` leaves the bar without."""
    out = []
    for index, bar in enumerate(bars):
        raw = volumes[min(index, len(volumes) - 1)]
        out.append(bar.model_copy(update={"volume": None if raw == "-" else Decimal(raw)}))
    return tuple(out)


def _flat(count: int, *volumes: str) -> tuple[PriceBar, ...]:
    return _with_volume(make_bars("BTCUSDT", (Decimal(100),) * count), *volumes)


def _request(
    bars: tuple[PriceBar, ...],
    targets: tuple[TargetPosition, ...],
    costs: BacktestCostModel = ZERO,
    equity: str = "10000",
) -> BacktestRequest:
    return BacktestRequest(
        cost_model=costs, initial_equity=Decimal(equity), bars=bars, targets=targets
    )


def _carry(rate: str = "0.5", **kwargs: object) -> BarBacktester:
    model = ExecutionModel(
        max_participation_rate=Decimal(rate),
        carry_over=True,
        **kwargs,  # type: ignore[arg-type]
    )
    return BarBacktester(execution=model)


def _run(
    backtester: BarBacktester, request: BacktestRequest
) -> tuple[BacktestResult, ExecutionReport]:
    result, report = backtester.run_with_report(request)
    assert BacktestResult.model_validate(result.model_dump()) == result
    assert BacktestResult.model_validate_json(result.model_dump_json()) == result
    result.check_answers(request, backtester.descriptor)
    assert report.result_hash == result.result_hash
    assert report.carried == result.remainders
    assert report.remainders == ()  # nothing is cancelled at its bar under carry-over
    return result, report


# ----------------------------------------------------------------------------------------
# 1. the pre-existing models are bit-identical (golden values recorded at 50a43a4)
# ----------------------------------------------------------------------------------------

#: ``request_hash`` of the golden requests (their bars carry no volume).
_GOLDEN_REQUESTS = {
    "alternating": "ee46085590937d9d0dec764a739d84f702b17a6b66472aa5140e1605f867a67c",
    "leverage_and_gaps": "5ea0cc766022e0083bb74288a665ab8ee9e25417e3d93042f5eb718a816364cf",
    "two_instruments": "d12234917f98191186fe2768a50bd44a51ce99f42a8bcbfd3d7a376764c38846",
}
_GOLDEN_FIRST_BAR = "1323628a47020bdd4378bc3aabccdf482a7b5e6eff9b008124d5a2a7b852dd96"
_GOLDEN_V1_DESCRIPTOR = "6f856ab5f3223e1f3e81439e844a337410406836f2958cbeb54dc2adad6ae3e1"
#: The same objects built now carry the 2.1.0 envelope (ADR-0052 M2: new objects are 2.1.0
#: and the envelope is part of every content hash); pinned next to the 2.0.0 evidence above.
_GOLDEN_REQUESTS_2_1_0 = {
    "alternating": "e07ea0fc9b221b0bb53dd4081eb4923ac71a6bae91561a0fe8666f13cc581382",
    "leverage_and_gaps": "5ef45bdf68dff98f9acad6a3a0b319f0f5abf3e2e2061694ad4339fdff769bb4",
    "two_instruments": "b334a7827898190dc5c247d527a92212f55149abdbc7a354976208e4944fc637",
}
_GOLDEN_FIRST_BAR_2_1_0 = "ac03375206ec6f7692fccb3e620f984a3f69a499ae43cb171079b70eea8d7442"
_GOLDEN_V1_DESCRIPTOR_2_1_0 = "281355118786ddde8fc956c101888be0d0c0ff7fcebc12f406c6c7d59199da58"
#: ``(provider_hash, result_hash)`` of the truncating variants at 2.1.0 (fingerprints unchanged).
_GOLDEN_TRUNCATING_2_1_0 = {
    "all_active": (
        "082ec9936fc9b046dd4fcc9604b9e4a324f42f9420cb6314045e989b8fda591c",
        "1ef8b2d4ddf9e521ca347fc9b9e1cef6819562a8236ddae6711a191d243075a4",
    ),
    "cap_and_impact": (
        "a116db0b1ead46de07a4bdc3578cecfc20bac43cf766e595b846df21d01aaa92",
        "b59c541578d301cd16bad5dfb8c7e7716e54ad3bdd1dc12e34440b09183d181c",
    ),
}
#: ``(fingerprint, provider_hash, result_hash)`` of the truncating variants on one fixed request.
_GOLDEN_TRUNCATING = {
    "all_active": (
        "e505d29838d422a10ec56edae175814b87c83d1f240372f9288cdbac00092e2f",
        "879e3a77a688dc066ac1544a3b0e9a6d575d35fa882460fc36fef5a6a4be1c38",
        "c34e581cff3b430d326d1970734a8b46590688b86d3d0dac3c3d3f861196fbf2",
    ),
    "cap_and_impact": (
        "a117ebaa91b52e70c6270bafe1676681b7bd4077553b91a5f990a21ea2b74627",
        "aefc348255533a5a11f5d7e22c9c2fc9a6a5c9bb2ad88e4345cbef165582b406",
        "367cf4fb5095d559f02e9e1a3b5035e110d2b63f5e3ba4efdc8afbdfae2e1c18",
    ),
}


@pytest.mark.parametrize("name", sorted(_GOLDEN_REQUESTS))
def test_request_hashes_are_unchanged_when_no_bar_carries_volume(name: str) -> None:
    # The pins were recorded at contract 2.0.0 and ADR-0054 is re-declared at 2.1.0 (ADR-0052
    # §4): the no-volume objects rebuilt as the 2.0.0 code built them keep the pins bit for bit.
    with built_at_pre_bump():
        request = at_pre_bump(_golden_requests()[name])
        result = BarBacktester().run(request)
    assert request.content_hash() == _GOLDEN_REQUESTS[name]
    assert request.bars[0].content_hash() == _GOLDEN_FIRST_BAR
    assert "volume" not in request.bars[0].model_dump(mode="json")
    assert result.schema_version == "2.0.0"
    current = _golden_requests()[name]
    assert current.content_hash() == _GOLDEN_REQUESTS_2_1_0[name]
    assert current.bars[0].content_hash() == _GOLDEN_FIRST_BAR_2_1_0
    assert result.remainders == ()
    assert "remainders" not in result.model_dump(mode="json")
    assert "remainders" not in result._hashed_fields()


def test_the_default_descriptor_is_unchanged() -> None:
    with built_at_pre_bump():  # pinned at 2.0.0 (see above)
        descriptor = BarBacktester().descriptor
    assert descriptor.execution_model == "next_bar_open"
    assert descriptor.content_hash() == _GOLDEN_V1_DESCRIPTOR
    assert BarBacktester().descriptor.content_hash() == _GOLDEN_V1_DESCRIPTOR_2_1_0


@pytest.mark.parametrize("name", sorted(_GOLDEN_TRUNCATING))
def test_the_truncating_variants_are_unchanged(name: str) -> None:
    model = _ACTIVE_MODELS[name]
    with built_at_pre_bump():  # pinned at 2.0.0 (see above)
        backtester = BarBacktester(execution=model)
        targets = tuple(_target(i, "1.5" if i % 3 else "-1") for i in range(12))
        result = backtester.run(at_pre_bump(_request(_SUITE_BARS, targets, COSTS)))
    assert (model.fingerprint, backtester.descriptor.content_hash(), result.result_hash) == (
        _GOLDEN_TRUNCATING[name]
    )
    assert backtester.descriptor.execution_model == "next_bar_open"
    assert result.remainders == ()
    now = BarBacktester(execution=model)
    fresh = tuple(_target(i, "1.5" if i % 3 else "-1") for i in range(12))  # 2.1.0 targets
    current = now.run(_request(_SUITE_BARS, fresh, COSTS))
    assert (now.descriptor.content_hash(), current.result_hash) == _GOLDEN_TRUNCATING_2_1_0[name]


def test_a_bar_volume_changes_the_bar_and_request_hashes() -> None:
    bars = make_bars("BTCUSDT", wave_closes(3))
    with_volume = _with_volume(bars, "10")
    assert with_volume[0].content_hash() != bars[0].content_hash()
    assert with_volume[0].model_dump(mode="json")["volume"] == "10"
    assert _request(with_volume, ()).content_hash() != _request(bars, ()).content_hash()
    zero = _with_volume(bars, "0")[0]
    assert zero.model_dump(mode="json")["volume"] == "0"  # zero is a value, not an omission


# ----------------------------------------------------------------------------------------
# 2. the contract suite
# ----------------------------------------------------------------------------------------

_CARRY_BARS = _with_volume(make_bars("BTCUSDT", wave_closes(12)), "40", "30", "60", "0", "50")


class TestCarryOverContract(CarryOverBacktestProviderContract):
    """Cap 0.5 on 30 - 60 units per bar against ~100 units wanted: binding, fills in the data."""

    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        return BacktestSubject(
            open=partial(_carry, "0.5"),
            bars=_CARRY_BARS,
            costs=COSTS,
            initial_equity=Decimal("10000"),
            tolerance=TOLERANCE,
        )


class TestCarryOverWithImpactAndFundingContract(CarryOverBacktestProviderContract):
    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        return BacktestSubject(
            open=partial(
                _carry,
                "0.5",
                impact_coefficient=Decimal("0.1"),
                short_borrow_rate=Decimal("0.0001"),
                cash_borrow_rate=Decimal("0.0002"),
            ),
            bars=_CARRY_BARS,
            costs=COSTS,
            initial_equity=Decimal("10000"),
            tolerance=TOLERANCE,
        )


def test_the_next_bar_open_checks_refuse_a_carry_over_provider() -> None:
    subject = BacktestSubject(_carry, _CARRY_BARS, COSTS, Decimal("10000"), TOLERANCE)
    with pytest.raises(ContractSuiteFailure, match="CarryOverBacktest"):
        backtest_suite.check_descriptor(subject)
    truncating = BacktestSubject(
        partial(BarBacktester, execution=_ACTIVE_MODELS["cap_and_impact"]),
        _SUITE_BARS,
        COSTS,
        Decimal("10000"),
        TOLERANCE,
    )
    with pytest.raises(ContractSuiteFailure, match="next_bar_open_participation"):
        backtest_suite.check_carry_over_descriptor(truncating)


# ----------------------------------------------------------------------------------------
# 3. identity
# ----------------------------------------------------------------------------------------


def test_the_carry_over_variant_declares_its_model_and_binds_its_parameters() -> None:
    carry = _carry("0.5")
    model = carry.execution
    assert model is not None
    assert carry.descriptor.execution_model == "next_bar_open_participation"
    assert carry.descriptor.version == f"{CARRY_OVER_VERSION}+exec.{model.fingerprint}"
    assert carry.descriptor.simulation_only is True
    other = _carry("0.4")
    volumes = {("BTCUSDT", T0): Decimal(1)}
    truncating = BarBacktester(
        execution=ExecutionModel(max_participation_rate=Decimal("0.5"), bar_volume=volumes)
    )
    with_side = _carry("0.5", bar_volume=volumes)
    hashes = {
        b.descriptor.content_hash() for b in (carry, other, truncating, with_side, BarBacktester())
    }
    assert len(hashes) == 5
    same = ExecutionModel(max_participation_rate=Decimal("0.50"), carry_over=True)
    assert same.fingerprint == model.fingerprint


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"carry_over": True, "short_borrow_rate": Decimal("0.1")}, ValueError),  # no cap
        ({"carry_over": 1, "max_participation_rate": Decimal("0.5")}, TypeError),
        ({"carry_over": "yes", "max_participation_rate": Decimal("0.5")}, TypeError),
    ],
)
def test_carry_over_needs_the_cap_and_an_explicit_bool(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        ExecutionModel(**kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------
# 4. carry-over semantics
# ----------------------------------------------------------------------------------------


def test_the_remainder_carries_over_and_the_fills_sum_to_the_target() -> None:
    bars = _flat(8, "40")  # 10000 at 100 = 100 units, 0.5 x 40 = 20 per bar
    result, _ = _run(_carry(), _request(bars, (_target(0, "1"),)))
    assert [(fill.fill_time, fill.quantity) for fill in result.fills] == [
        (T0 + i * MINUTE, Decimal(20)) for i in range(5)
    ]
    assert {fill.decision_time for fill in result.fills} == {T0}
    assert all(fill.reference_price == Decimal(100) for fill in result.fills)
    (record,) = result.remainders
    assert record == FillRemainder(
        instrument="BTCUSDT",
        decision_time=T0,
        requested_quantity=Decimal(100),
        filled_quantity=Decimal(100),
        remaining_quantity=Decimal(0),
        ended_by="filled",
        ended_at=T0 + 4 * MINUTE,
    )
    assert sum((fill.quantity for fill in result.fills), Decimal(0)) == record.requested_quantity
    assert result.unexecuted_targets == 0
    assert result.equity_curve[-1].gross_exposure == Decimal(10000)


def test_the_request_is_not_re_sized_while_it_carries() -> None:
    # prices double after the execution bar: the remaining quantity stays in units
    bars = _with_volume(make_bars("BTCUSDT", (Decimal(100),) + (Decimal(200),) * 7), "40")
    result, _ = _run(_carry(), _request(bars, (_target(0, "1"),)))
    (record,) = result.remainders
    assert record.requested_quantity == Decimal(100)
    assert sum((fill.quantity for fill in result.fills), Decimal(0)) == Decimal(100)


def test_zero_volume_fills_nothing_at_that_bar_and_the_remainder_waits() -> None:
    bars = _flat(8, "40", "0", "40")
    result, _ = _run(_carry(), _request(bars, (_target(0, "1"),)))
    assert [fill.fill_time for fill in result.fills] == [T0] + [
        T0 + i * MINUTE for i in range(2, 6)
    ]
    assert result.remainders[0].ended_at == T0 + 5 * MINUTE


def test_a_later_target_supersedes_the_remainder_and_re_targets_against_holdings() -> None:
    bars = _flat(8, "40")
    targets = (_target(0, "1"), _target(2, "-0.3"))
    result, _ = _run(_carry(), _request(bars, targets))
    old, new = result.remainders
    assert (old.ended_by, old.ended_at) == ("superseded", T0 + 2 * MINUTE)
    assert (old.filled_quantity, old.remaining_quantity) == (Decimal(40), Decimal(60))
    # the new target wants -30 units and holds +40: a change of -70, in slices of 20
    assert new.requested_quantity == Decimal(-70)
    assert (new.ended_by, new.ended_at) == ("filled", T0 + 5 * MINUTE)
    assert [fill.quantity for fill in result.fills] == [Decimal(20)] * 2 + [Decimal(-20)] * 3 + [
        Decimal(-10)
    ]
    assert [fill.decision_time for fill in result.fills] == [T0] * 2 + [T0 + 2 * MINUTE] * 4
    assert sum((fill.quantity for fill in result.fills), Decimal(0)) == Decimal(-30)


def test_a_target_superseded_before_its_execution_bar_is_unexecuted_and_unrecorded() -> None:
    bars = _flat(6, "40")
    early = _target(0, "1").model_copy(update={"decision_time": T0 + MINUTE / 3})
    late = _target(0, "-1").model_copy(update={"decision_time": T0 + MINUTE / 2})
    result, _ = _run(_carry(), _request(bars, (early, late)))
    assert [item.decision_time for item in result.remainders] == [late.decision_time]
    assert result.unexecuted_targets == 1
    assert all(fill.quantity < 0 for fill in result.fills)


def test_the_remainder_ends_at_the_end_of_data() -> None:
    bars = _flat(3, "40")
    result, _ = _run(_carry(), _request(bars, (_target(0, "1"),)))
    (record,) = result.remainders
    assert (record.ended_by, record.ended_at) == ("end_of_data", T0 + 2 * MINUTE)
    assert (record.filled_quantity, record.remaining_quantity) == (Decimal(60), Decimal(40))
    assert result.unexecuted_targets == 0


def test_a_target_with_nothing_to_trade_needs_no_volume_and_no_record() -> None:
    bars = _flat(4, "-")
    result, _ = _run(_carry(), _request(bars, (_target(0, "0"),)))
    assert result.fills == () and result.remainders == () and result.unexecuted_targets == 0


def test_a_target_that_never_fills_is_recorded_and_unexecuted() -> None:
    bars = _flat(3, "0")
    result, _ = _run(_carry(), _request(bars, (_target(0, "1"),)))
    (record,) = result.remainders
    assert (record.filled_quantity, record.ended_by) == (Decimal(0), "end_of_data")
    assert result.fills == () and result.unexecuted_targets == 1


def test_carry_over_runs_per_instrument() -> None:
    btc = _with_volume(make_bars("BTCUSDT", (Decimal(100),) * 6), "40")
    eth = _with_volume(make_bars("ETHUSDT", (Decimal(50),) * 6), "100")
    targets = (_target(0, "0.5"), _target(0, "0.5", "ETHUSDT"))
    result, _ = _run(_carry(), _request(btc + eth, targets))
    by_instrument = {item.instrument: item for item in result.remainders}
    assert by_instrument["BTCUSDT"].requested_quantity == Decimal(50)  # 20 per bar: 3 bars
    assert by_instrument["ETHUSDT"].requested_quantity == Decimal(100)  # 50 per bar: 2 bars
    assert by_instrument["BTCUSDT"].ended_at == T0 + 2 * MINUTE
    assert by_instrument["ETHUSDT"].ended_at == T0 + MINUTE


# ----------------------------------------------------------------------------------------
# 5. volume sources (ADR-0054 §4)
# ----------------------------------------------------------------------------------------


def test_a_missing_bar_volume_on_the_carry_path_is_refused() -> None:
    bars = _flat(6, "40", "40", "-")
    with pytest.raises(BacktestInputError, match=r"no PriceBar\.volume"):
        _carry().run(_request(bars, (_target(0, "1"),)))
    # the ExecutionModel side channel never stands in for a missing PriceBar.volume
    side = {(bar.instrument, bar.interval_start): Decimal(40) for bar in bars}
    with pytest.raises(BacktestInputError, match=r"no PriceBar\.volume"):
        _carry(bar_volume=side).run(_request(bars, (_target(0, "1"),)))
    # a target that fills before the gap needs nothing more
    result, _ = _run(_carry(), _request(bars, (_target(0, "0.2"),)))
    assert result.remainders[0].ended_by == "filled"


def test_a_side_channel_volume_that_disagrees_with_the_bar_is_refused() -> None:
    bars = _flat(6, "40")
    agree = {(bar.instrument, bar.interval_start): Decimal("40.0") for bar in bars}
    disagree = {**agree, (bars[1].instrument, bars[1].interval_start): Decimal(41)}
    request = _request(bars, (_target(0, "1"),))
    result, _ = _run(_carry(bar_volume=agree), request)
    assert len(result.fills) == 5
    with pytest.raises(BacktestInputError, match="disagrees"):
        _carry(bar_volume=disagree).run(request)
    # the truncating variant keeps its side channel, and refuses the same disagreement
    truncating = BarBacktester(
        execution=ExecutionModel(max_participation_rate=Decimal("0.5"), bar_volume=disagree)
    )
    with pytest.raises(BacktestInputError, match="disagrees"):
        truncating.run(_request(bars, tuple(_target(i, "1") for i in range(3))))
    unchanged = BarBacktester(
        execution=ExecutionModel(max_participation_rate=Decimal("0.5"), bar_volume=agree)
    ).run(_request(bars, (_target(0, "1"),)))
    assert unchanged.remainders == () and len(unchanged.fills) == 1


# ----------------------------------------------------------------------------------------
# 6. no future bar is used
# ----------------------------------------------------------------------------------------


def test_no_later_price_or_volume_changes_anything_before_t() -> None:
    bars = _with_volume(make_bars("BTCUSDT", wave_closes(10)), "30", "50")
    targets = (_target(0, "1.5"), _target(4, "-1"), _target(7, "0.5"))
    model = partial(
        _carry,
        "0.5",
        impact_coefficient=Decimal("0.1"),
        short_borrow_rate=Decimal("0.0001"),
        cash_borrow_rate=Decimal("0.0002"),
    )
    cut = 5
    later = bars[:cut] + tuple(
        bar.model_copy(
            update={
                **{name: getattr(bar, name) * 3 for name in ("open", "high", "low", "close")},
                "volume": Decimal(7) * (bar.volume or 0),
            }
        )
        for bar in bars[cut:]
    )
    base, base_report = _run(model(), _request(bars, targets, COSTS))
    moved, moved_report = _run(model(), _request(later, targets, COSTS))
    before = bars[cut].interval_start
    assert any(fill.fill_time >= before for fill in base.fills)  # the future does matter later
    assert [f for f in moved.fills if f.fill_time < before] == [
        f for f in base.fills if f.fill_time < before
    ]
    assert [r for r in moved.remainders if r.ended_at < before] == [
        r for r in base.remainders if r.ended_at < before
    ]
    assert [p for p in moved.equity_curve if p.time <= before] == [
        p for p in base.equity_curve if p.time <= before
    ]
    assert [c for c in moved_report.funding if c.time < before] == [
        c for c in base_report.funding if c.time < before
    ]
    assert moved.result_hash != base.result_hash


# ----------------------------------------------------------------------------------------
# 7. check_answers refuses forged carry-over results
# ----------------------------------------------------------------------------------------


def _genuine() -> tuple[BarBacktester, BacktestRequest, BacktestResult]:
    bars = _flat(8, "40")
    backtester = _carry()
    request = _request(bars, (_target(0, "1"), _target(3, "-0.2")))
    result, _ = _run(backtester, request)
    return backtester, request, result


def _forged(
    request: BacktestRequest,
    descriptor: BacktestProviderDescriptor,
    genuine: BacktestResult,
    *,
    fills: tuple[Fill, ...] | None = None,
    remainders: tuple[FillRemainder, ...] | None = None,
    unexecuted: int | None = None,
) -> BacktestResult:
    return BacktestResult.build(
        request,
        descriptor,
        fills=sorted(
            genuine.fills if fills is None else fills,
            key=lambda item: (item.fill_time, item.instrument),
        ),
        equity_curve=genuine.equity_curve,
        unexecuted_targets=genuine.unexecuted_targets if unexecuted is None else unexecuted,
        remainders=genuine.remainders if remainders is None else remainders,
    )


def _moved(fill: Fill, minutes: int) -> Fill:
    return fill.model_copy(update={"fill_time": fill.fill_time + minutes * MINUTE})


def _replace_fill(result: BacktestResult, index: int, fill: Fill) -> tuple[Fill, ...]:
    return tuple(fill if i == index else item for i, item in enumerate(result.fills))


def _replace_record(
    result: BacktestResult, index: int, **update: object
) -> tuple[FillRemainder, ...]:
    return tuple(
        item.model_copy(update=update) if i == index else item
        for i, item in enumerate(result.remainders)
    )


_Forgery = Callable[[BacktestResult], dict[str, object]]

_FORGERIES: dict[str, tuple[_Forgery, str]] = {
    # the old target's third fill at minute 3 is the next target's execution bar
    "fill_at_the_next_targets_execution_bar": (
        lambda r: {"fills": _replace_fill(r, 2, _moved(r.fills[2], 1))},
        "结转窗口",
    ),
    "fill_of_an_unknown_target": (
        lambda r: {
            "fills": _replace_fill(
                r, 0, r.fills[0].model_copy(update={"decision_time": T0 - MINUTE})
            )
        },
        "没有对应的目标",
    ),
    "fill_between_bars": (
        lambda r: {
            "fills": _replace_fill(
                r,
                1,
                r.fills[1].model_copy(update={"fill_time": T0 + MINUTE + timedelta(seconds=1)}),
            )
        },
        "结转窗口",
    ),
    "two_fills_at_one_bar": (
        lambda r: {"fills": _replace_fill(r, 1, _moved(r.fills[1], -1))},
        "同一根 bar",
    ),
    "reverse_fill": (
        lambda r: {
            "fills": _replace_fill(
                r, 1, r.fills[1].model_copy(update={"quantity": -r.fills[1].quantity})
            ),
            "remainders": _replace_record(
                r,
                0,
                filled_quantity=Decimal(20),
                remaining_quantity=Decimal(80),
            ),
        },
        "反向",
    ),
    "overfill": (
        lambda r: {
            "fills": _replace_fill(r, 1, r.fills[1].model_copy(update={"quantity": Decimal(100)}))
        },
        "成交之和不符",
    ),
    "wrong_reference_price": (
        lambda r: {
            "fills": _replace_fill(
                r, 1, r.fills[1].model_copy(update={"reference_price": Decimal(101)})
            )
        },
        "参考价",
    ),
    "missing_record": (
        lambda r: {"remainders": r.remainders[1:]},
        "缺少结转记录",
    ),
    "record_without_target": (
        lambda r: {
            "remainders": (
                *r.remainders,
                r.remainders[0].model_copy(
                    update={"decision_time": T0 + 7 * MINUTE, "ended_at": T0 + 7 * MINUTE}
                ),
            )
        },
        "没有对应的",
    ),
    "filled_quantity_mismatch": (
        lambda r: {
            "remainders": _replace_record(
                r, 0, filled_quantity=Decimal(20), remaining_quantity=Decimal(80)
            )
        },
        "成交之和不符",
    ),
    "superseded_at_the_wrong_bar": (
        lambda r: {"remainders": _replace_record(r, 0, ended_at=T0 + 4 * MINUTE)},
        "superseded",
    ),
    "claimed_end_of_data_while_superseded": (
        lambda r: {"remainders": _replace_record(r, 0, ended_by="end_of_data")},
        "end_of_data",
    ),
    "filled_before_its_last_fill": (
        lambda r: {"remainders": _replace_record(r, 1, ended_at=T0 + 4 * MINUTE)},
        "filled",
    ),
    "too_many_unexecuted": (lambda r: {"unexecuted": 1}, "未执行目标数"),
}


def test_the_genuine_result_is_what_the_forgeries_start_from() -> None:
    _, _, result = _genuine()
    # 100 units wanted: 20 at minutes 0 - 2, then superseded at 3 by a change of -80 (-20 target)
    assert [fill.quantity for fill in result.fills] == [Decimal(20)] * 3 + [Decimal(-20)] * 4
    assert [(r.ended_by, r.ended_at) for r in result.remainders] == [
        ("superseded", T0 + 3 * MINUTE),
        ("filled", T0 + 6 * MINUTE),
    ]


@pytest.mark.parametrize("name", sorted(_FORGERIES))
def test_check_answers_refuses_forged_carry_over_results(name: str) -> None:
    backtester, request, genuine = _genuine()
    forge, message = _FORGERIES[name]
    forged = _forged(request, backtester.descriptor, genuine, **forge(genuine))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message):
        forged.check_answers(request, backtester.descriptor)


def test_forged_records_that_break_their_own_invariants_do_not_even_construct() -> None:
    _, _, genuine = _genuine()
    record = genuine.remainders[0]
    for update in (
        {"filled_quantity": Decimal(120), "remaining_quantity": Decimal(-20)},  # overfill
        {"filled_quantity": Decimal(-20), "remaining_quantity": Decimal(80)},  # reverse
        {"remaining_quantity": Decimal(0)},  # superseded with nothing left
    ):
        with pytest.raises(ValueError):
            record.model_copy(update=update)


def test_a_next_bar_open_descriptor_refuses_carry_over_records() -> None:
    _, request, genuine = _genuine()
    v1 = BarBacktester().descriptor
    forged = _forged(request, v1, genuine)
    with pytest.raises(ValueError, match="next_bar_open_participation"):
        forged.check_answers(request, v1)
    # and a carry-over result is never mistaken for the v1 model's
    without_records = _forged(request, v1, genuine, remainders=())
    with pytest.raises(ValueError, match="不在执行模型规定的 bar"):
        without_records.check_answers(request, v1)
