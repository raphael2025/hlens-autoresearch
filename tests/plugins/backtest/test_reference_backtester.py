"""Phase 14: the reference backtester (ADR-0106) — the contract suite plus a field-by-field
differential against ``BarBacktester`` over many signal / cost configurations."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

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
    MONEY_QUANTUM,
    REFERENCE_MONEY_QUANTUM,
    BarBacktester,
    ExecutionModel,
    ReferenceBacktester,
    ReferenceScopeError,
    reference,
)
from tests.contract_suites.backtest import BacktestProviderContract, BacktestSubject
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes


class TestReferenceBacktester(BacktestProviderContract):
    @pytest.fixture
    def backtest_subject(self) -> BacktestSubject:
        return BacktestSubject(
            open=ReferenceBacktester,
            bars=make_bars("BTCUSDT", wave_closes(12)),
            costs=COSTS,
            initial_equity=Decimal("10000"),
            tolerance=MONEY_QUANTUM * 1000,
        )


def _target(minute: object, weight: str, instrument: str = "BTCUSDT") -> TargetPosition:
    decided = T0 + minute * MINUTE  # type: ignore[operator]
    return TargetPosition(
        decision_time=decided,
        instrument=instrument,
        target_weight=Decimal(weight),
        inputs_used=1,
        latest_input_available_time=decided,
    )


def _request(
    bars: tuple[PriceBar, ...],
    targets: tuple[TargetPosition, ...],
    costs: BacktestCostModel = COSTS,
    equity: str = "10000",
) -> BacktestRequest:
    return BacktestRequest(
        cost_model=costs, initial_equity=Decimal(equity), bars=bars, targets=targets
    )


def _costs(fee: str, slippage: str) -> BacktestCostModel:
    return BacktestCostModel(
        name="diff", version="1.0.0", fee_rate=Decimal(fee), slippage_rate=Decimal(slippage)
    )


def _both(request: BacktestRequest) -> tuple[BacktestResult, BacktestResult]:
    return BarBacktester().run(request), ReferenceBacktester().run(request)


def _assert_same_economics(old: BacktestResult, new: BacktestResult) -> None:
    """Every field but the provider identity (and the hash that embeds it) is identical."""
    assert new.fills == old.fills
    assert new.equity_curve == old.equity_curve
    assert new.final_equity == old.final_equity
    assert new.total_fees == old.total_fees
    assert new.total_slippage == old.total_slippage
    assert new.unexecuted_targets == old.unexecuted_targets
    assert new.remainders == old.remainders == ()
    assert new.request_hash == old.request_hash
    assert new.initial_equity == old.initial_equity
    assert new.provider != old.provider and new.provider_hash != old.provider_hash
    assert new.result_hash != old.result_hash


SINGLE = tuple(make_bars("BTCUSDT", wave_closes(40)))
TWO = make_bars("BTCUSDT", wave_closes(30)) + make_bars("ETHUSDT", wave_closes(30, phase=10))


def _alternating(count: int, instrument: str = "BTCUSDT") -> tuple[TargetPosition, ...]:
    weights = ("1", "-1", "0.5", "0", "-0.25", "2")
    return tuple(_target(i, weights[i % len(weights)], instrument) for i in range(count))


@pytest.mark.parametrize(
    ("fee", "slippage"),
    [
        ("0", "0"),
        ("0.001", "0.0005"),
        ("0.0002", "0"),
        ("0", "0.003"),
        ("0.01", "0.02"),
        ("0.000123456789", "0.000098765432"),
    ],
)
@pytest.mark.parametrize("equity", ["10000", "1", "123456789.123456789"])
def test_single_instrument_signals_match_field_by_field(
    fee: str, slippage: str, equity: str
) -> None:
    request = _request(SINGLE, _alternating(36), _costs(fee, slippage), equity)
    old, new = _both(request)
    _assert_same_economics(old, new)
    assert new.fills, "the configuration must trade"


def test_two_instruments_with_interleaved_targets_match() -> None:
    targets = tuple(
        sorted(
            (*_alternating(25), *_alternating(25, "ETHUSDT")),
            key=lambda t: (t.decision_time, t.instrument),
        )
    )
    old, new = _both(_request(TWO, targets))
    _assert_same_economics(old, new)
    assert {fill.instrument for fill in new.fills} == {"BTCUSDT", "ETHUSDT"}


def test_first_trade_order_across_instruments_matches() -> None:
    """ETH trades before BTC: the exposure / mark-to-market sums accumulate in that order."""
    targets = (_target(0, "0.3", "ETHUSDT"), _target(1, "0.4"), _target(5, "-0.7", "ETHUSDT"))
    old, new = _both(_request(TWO, targets))
    _assert_same_economics(old, new)


def test_a_decision_inside_a_bar_executes_at_the_next_open() -> None:
    bars = make_bars("BTCUSDT", wave_closes(6))
    target = _target(0, "1").model_copy(
        update={"decision_time": T0 + MINUTE / 2, "latest_input_available_time": T0}
    )
    old, new = _both(_request(bars, (target,), equity="1000"))
    _assert_same_economics(old, new)
    assert [fill.fill_time for fill in new.fills] == [T0 + MINUTE]
    assert new.fills[0].reference_price == bars[1].open


def test_superseded_unexecuted_and_zero_trade_targets_are_counted_alike() -> None:
    bars = make_bars("BTCUSDT", wave_closes(5))
    targets = (
        _target(0, "1").model_copy(update={"decision_time": T0 + MINUTE / 3}),
        _target(0, "-1").model_copy(update={"decision_time": T0 + MINUTE / 2}),
        _target(2, "-1"),  # re-targets the held weight at a new equity: a small rebalance
        _target(3, "0"),
        _target(4, "0"),  # already flat: executes with no fill
        _target(30, "1"),  # after the last bar
    )
    old, new = _both(_request(bars, targets, equity="1000"))
    _assert_same_economics(old, new)
    assert new.unexecuted_targets == old.unexecuted_targets
    assert new.unexecuted_targets == 2  # the superseded one and the one after the last bar
    assert len(new.fills) == 3  # the zero-weight repeat executed without a fill
    assert new.fills[0].quantity < 0  # the superseding target, not the superseded one, executes


def test_a_wiped_out_book_is_only_flattened() -> None:
    """Leverage 5 into a falling market drives equity to zero or below: both flatten only."""
    falling = make_bars("BTCUSDT", tuple(Decimal(100 - 18 * i) for i in range(5)))
    targets = tuple(_target(i, "5" if i == 0 else "1") for i in range(5))
    old, new = _both(_request(falling, targets, _costs("0.001", "0.001"), "1000"))
    _assert_same_economics(old, new)


def test_a_bar_gap_in_one_instrument_keeps_its_last_mark() -> None:
    bars = make_bars("BTCUSDT", wave_closes(10)) + tuple(
        bar
        for bar in make_bars("ETHUSDT", wave_closes(10))
        if bar.interval_start != T0 + 4 * MINUTE
    )
    targets = (_target(0, "0.5"), _target(0, "0.5", "ETHUSDT"), _target(4, "-0.5", "ETHUSDT"))
    old, new = _both(_request(bars, targets))
    _assert_same_economics(old, new)


def test_zero_targets_leave_equity_untouched() -> None:
    old, new = _both(_request(SINGLE, ()))
    _assert_same_economics(old, new)
    assert new.pnl == 0


def test_the_money_quantum_agrees_but_is_defined_independently() -> None:
    assert REFERENCE_MONEY_QUANTUM == MONEY_QUANTUM


def test_the_reference_module_does_not_import_the_bar_engine() -> None:
    tree = ast.parse(inspect.getsource(reference))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not {name for name in imported if name.startswith("plugins.backtest")}, imported


def test_the_descriptor_is_distinct_from_the_bar_engines() -> None:
    new, old = ReferenceBacktester().descriptor, BarBacktester().descriptor
    assert (new.name, new.execution_model) == ("hlens_reference_backtest", "next_bar_open")
    assert new.deterministic and new.simulation_only
    assert new.plugin_key != old.plugin_key and new.content_hash() != old.content_hash()


# --- explicit refusals (ADR-0106 decision 1) --------------------------------------------------


def test_an_execution_model_is_refused_at_construction() -> None:
    carry = ExecutionModel(max_participation_rate=Decimal("0.1"), carry_over=True)
    funding = ExecutionModel(short_borrow_rate=Decimal("0.0001"))
    with pytest.raises(ReferenceScopeError, match="next_bar_open"):
        ReferenceBacktester(execution=carry)
    with pytest.raises(ReferenceScopeError, match="next_bar_open"):
        ReferenceBacktester(execution=funding)
    with pytest.raises(ReferenceScopeError):
        ReferenceBacktester(execution=object())
    assert issubclass(ReferenceScopeError, BacktestInputError)


def test_the_risk_loop_and_the_execution_report_are_refused() -> None:
    request = _request(SINGLE, ())
    engine = ReferenceBacktester()
    with pytest.raises(ReferenceScopeError, match="risk"):
        engine.run_with_risk(request, risk=None)
    with pytest.raises(ReferenceScopeError, match="report"):
        engine.run_with_report(request)


def test_a_non_request_is_refused() -> None:
    with pytest.raises(BacktestInputError, match="BacktestRequest"):
        ReferenceBacktester().run("not a request")  # type: ignore[arg-type]


def test_a_misaligned_bar_grid_is_refused_like_the_bar_engine() -> None:
    first = make_bars("BTCUSDT", wave_closes(4))
    late = tuple(
        bar.model_copy(
            update={
                "instrument": "ETHUSDT",
                "interval_start": bar.interval_start + MINUTE / 2,
                "interval_end": bar.interval_end - MINUTE / 4,
                "available_time": bar.interval_end - MINUTE / 4,
            }
        )
        for bar in first[:2]
    )
    request = _request(first + late, ())
    with pytest.raises(BacktestInputError, match="aligned bar grid"):
        BarBacktester().run(request)
    with pytest.raises(BacktestInputError, match="aligned bar grid"):
        ReferenceBacktester().run(request)
