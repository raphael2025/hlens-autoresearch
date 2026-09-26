"""P10 paper deviation: the router's net paper result vs a declared reference backtest.

The run is ``tests/research/router/test_paper.py``'s (a two-strategy router over seven BTC bars,
zero instrument cost, 1% switching cost); the reference is strategy A run alone through the same
backtester, bars and cost model.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from core.contracts.strategy import BacktestRequest, BacktestResult
from core.domain.base import content_hash
from plugins.backtest import BarBacktester
from research.router import DeviationError, PaperDeviation, RouterError, paper_deviation
from research.router.deviation import RETURN_QUANTUM
from tests.research.router.test_paper import BARS, STRATEGIES, ZERO, A, _run
from tests.strategy_fixtures import T0, make_bars


def reference_request(initial: str = "1000") -> BacktestRequest:
    """Strategy A alone over the run's bars and cost model (the declared reference)."""
    return BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(initial),
        bars=BARS,
        targets=STRATEGIES[A].positions,
    )


def reference(initial: str = "1000") -> BacktestResult:
    return BarBacktester().run(reference_request(initial))


def deviation() -> PaperDeviation:
    return paper_deviation(_run(), reference(), reference_request=reference_request())


def test_every_mark_compares_the_net_paper_equity_with_the_reference() -> None:
    run, ref = _run(), reference()
    report = deviation()
    assert [mark.time for mark in report.marks] == [p.time for p in run.result.equity_curve]
    previous_paper = previous_reference = Decimal(1000)
    for mark, ours, theirs in zip(
        report.marks, run.result.equity_curve, ref.equity_curve, strict=True
    ):
        assert (mark.paper_equity, mark.reference_equity) == (ours.equity, theirs.equity)
        assert mark.equity_difference == ours.equity - theirs.equity
        assert mark.paper_return == (ours.equity / previous_paper - 1).quantize(RETURN_QUANTUM)
        assert mark.reference_return == (theirs.equity / previous_reference - 1).quantize(
            RETURN_QUANTUM
        )
        assert mark.paper_return is not None and mark.reference_return is not None
        assert mark.return_difference == mark.paper_return - mark.reference_return
        previous_paper, previous_reference = ours.equity, theirs.equity


def test_hand_checked_marks() -> None:
    """A alone: flat at t1, long 10 @ 100 from t2 (1000, 1000, 1100, 1210, ...); the router is
    long at t2 (net of the 10 switching charge) then short half at t4 (test_paper's numbers)."""
    report = deviation()
    first, second, third = report.marks[:3]
    assert (first.paper_equity, first.reference_equity) == (Decimal(1000), Decimal(1000))
    assert first.equity_difference == 0 and first.return_difference == 0
    assert second.paper_equity == Decimal(990)  # 1000 - the 10 charged at t2
    assert second.equity_difference == Decimal(-10)
    assert second.paper_return == Decimal("-0.01").quantize(RETURN_QUANTUM)
    assert third.reference_equity == Decimal(1100)
    assert third.equity_difference == Decimal(1090) - Decimal(1100)


def test_summary_statistics() -> None:
    run, ref = _run(), reference()
    report = deviation()
    summary = report.summary
    differences = [mark.equity_difference for mark in report.marks]
    returns = [mark.return_difference for mark in report.marks]
    assert all(value is not None for value in returns)
    values = [value for value in returns if value is not None]
    assert summary.marks == len(report.marks) == len(run.result.equity_curve)
    assert summary.initial_equity == Decimal(1000)
    assert summary.final_equity_difference == run.result.final_equity - ref.final_equity
    assert summary.final_equity_difference == differences[-1]
    assert summary.max_abs_equity_difference == max(abs(value) for value in differences)
    widest = next(m for m in report.marks if abs(m.equity_difference) == max(map(abs, differences)))
    assert summary.max_abs_equity_difference_at == widest.time
    assert summary.paper_total_return == (run.result.final_equity / 1000 - 1).quantize(
        RETURN_QUANTUM
    )
    assert summary.total_return_difference == (
        summary.paper_total_return - summary.reference_total_return
    )
    assert summary.return_marks == len(values)
    mean = sum(values, Decimal(0)) / len(values)
    assert summary.mean_return_difference == mean.quantize(RETURN_QUANTUM)
    assert summary.mean_abs_return_difference == (
        sum((abs(v) for v in values), Decimal(0)) / len(values)
    ).quantize(RETURN_QUANTUM)
    assert summary.tracking_error is not None and summary.tracking_error > 0
    # sample variance, n - 1
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (len(values) - 1)
    assert abs(summary.tracking_error**2 - variance) < Decimal("1e-15")


def test_the_report_is_deterministic_and_content_hashed() -> None:
    report, again = deviation(), deviation()
    assert report == again and report.deviation_hash == again.deviation_hash
    payload = report.to_payload()
    body = {key: value for key, value in payload.items() if key != "deviation_hash"}
    assert payload["deviation_hash"] == report.deviation_hash == content_hash(body)
    assert payload["kind"] == "paper_deviation"
    assert payload["run_hash"] == _run().run_hash
    assert payload["reference_result_hash"] == reference().result_hash
    assert payload["instruments"] == ["BTC"]
    # a different run (another switching rate) is another report
    other = paper_deviation(_run("0.02"), reference())
    assert other.deviation_hash != report.deviation_hash


def test_a_run_compared_with_its_own_gross_result_has_only_the_switching_cost() -> None:
    run = _run()
    report = paper_deviation(run, run.gross)
    assert report.summary.final_equity_difference == -run.total_switching_cost
    assert all(mark.equity_difference <= 0 for mark in report.marks)


def test_a_zero_switching_rate_against_its_gross_result_deviates_nowhere() -> None:
    run = _run("0")
    summary = paper_deviation(run, run.gross).summary
    assert summary.max_abs_equity_difference == 0
    assert summary.tracking_error == 0 and summary.mean_return_difference == 0


# --- refusals ------------------------------------------------------------------------------


def test_misaligned_marks_are_refused() -> None:
    later = make_bars("BTC", [bar.close for bar in BARS], start=T0 + timedelta(seconds=1))
    moved = BarBacktester().run(
        BacktestRequest(
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            bars=later,
            targets=STRATEGIES[A].positions,
        )
    )
    assert len(moved.equity_curve) == len(_run().result.equity_curve)  # same count, other times
    with pytest.raises(DeviationError, match="equity marks"):
        paper_deviation(_run(), moved)
    shorter = BarBacktester().run(
        BacktestRequest(
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            bars=BARS[:-1],
            targets=STRATEGIES[A].positions,
        )
    )
    with pytest.raises(DeviationError, match="equity marks"):
        paper_deviation(_run(), shorter)


def test_another_initial_equity_is_refused() -> None:
    with pytest.raises(DeviationError, match="starts at"):
        paper_deviation(_run(), reference("2000"))


def test_a_reference_on_other_instruments_is_refused() -> None:
    eth = make_bars("ETH", [Decimal(c) for c in ("100", "100", "110", "121", "110", "99", "99")])
    positions = tuple(p.model_copy(update={"instrument": "ETH"}) for p in STRATEGIES[A].positions)
    request = BacktestRequest(
        cost_model=ZERO, initial_equity=Decimal(1000), bars=eth, targets=positions
    )
    other = BarBacktester().run(request)
    with pytest.raises(DeviationError, match="does not price"):
        paper_deviation(_run(), other)
    # a reference that answers its request, trades only BTC, but prices BTC and ETH
    both = BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        bars=BARS + eth,
        targets=STRATEGIES[A].positions,
    )
    wider = BarBacktester().run(both)
    with pytest.raises(DeviationError, match="the reference prices"):
        paper_deviation(_run(), wider, reference_request=both)
    with pytest.raises(DeviationError, match="does not answer"):
        paper_deviation(_run(), reference(), reference_request=reference_request("2000"))


def test_a_tampered_run_is_refused() -> None:
    run = _run()
    tampered = dataclasses.replace(run, router="other@1.0.0")
    with pytest.raises(RouterError, match="run_hash"):
        paper_deviation(tampered, reference())


def test_only_a_run_and_a_backtest_result_are_compared() -> None:
    with pytest.raises(DeviationError):
        paper_deviation(_run(), _run())  # type: ignore[arg-type]
