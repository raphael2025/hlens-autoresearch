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
from core.contracts.validation_profile import ProfileScope, ValidationProfile
from core.domain.base import Kind, Ref, content_hash
from core.domain.research import GateResult, ValidationReport, Verdict
from plugins.backtest import BarBacktester
from research.router import (
    DeviationError,
    PaperDeviation,
    RouterError,
    paper_deviation,
    validate_scope_bound_payload,
)
from research.router.deviation import RETURN_QUANTUM
from tests.research.router.test_paper import A, BARS, STRATEGIES, ZERO, _run
from tests.strategy_fixtures import T0, make_bars
from tests.factories import validation_profile


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


def scope_evidence() -> tuple[ValidationProfile, ValidationReport]:
    profile = validation_profile(
        name="p10_deviation_scope",
        scope=ProfileScope(
            venue="testvenue", symbol="BTC", timeframe="1m", research_class="swing"
        ),
    )
    report = ValidationReport(
        report_id="rep-vol-router",
        run_id="run-vol-router",
        subject=Ref(kind=Kind.STRATEGY, name="vol_router", version="1.0.0"),
        experiment_hash="a" * 64,
        constitution_version="1.0.0",
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        gates=(
            GateResult(
                gate_id="G5.sealed_oos",
                metric="sealed_oos",
                value=1.0,
                verdict=Verdict.PASS,
            ),
        ),
        verdict=Verdict.PASS,
    )
    return profile, report


def scope_kwargs() -> dict[str, object]:
    profile, report = scope_evidence()
    return {"validation_profile": profile, "validation_report": report}


def deviation() -> PaperDeviation:
    return paper_deviation(
        _run(), reference(), reference_request=reference_request(), **scope_kwargs()
    )


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
    assert payload["schema_version"] == "2.0.0"
    assert payload["declared_scope"]["symbol"] == "BTC"
    scope_body = {k: v for k, v in payload["declared_scope"].items() if k != "scope_hash"}
    assert payload["declared_scope"]["scope_hash"] == content_hash(scope_body)
    validate_scope_bound_payload(payload, **scope_kwargs())
    # a different run (another switching rate) is another report
    other = paper_deviation(_run("0.02"), reference(), **scope_kwargs())
    assert other.deviation_hash != report.deviation_hash


def test_a_run_compared_with_its_own_gross_result_has_only_the_switching_cost() -> None:
    run = _run()
    report = paper_deviation(run, run.gross, **scope_kwargs())
    assert report.summary.final_equity_difference == -run.total_switching_cost
    assert all(mark.equity_difference <= 0 for mark in report.marks)


def test_a_zero_switching_rate_against_its_gross_result_deviates_nowhere() -> None:
    run = _run("0")
    summary = paper_deviation(run, run.gross, **scope_kwargs()).summary
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
        paper_deviation(_run(), moved, **scope_kwargs())
    shorter = BarBacktester().run(
        BacktestRequest(
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            bars=BARS[:-1],
            targets=STRATEGIES[A].positions,
        )
    )
    with pytest.raises(DeviationError, match="equity marks"):
        paper_deviation(_run(), shorter, **scope_kwargs())


def test_another_initial_equity_is_refused() -> None:
    with pytest.raises(DeviationError, match="starts at"):
        paper_deviation(_run(), reference("2000"), **scope_kwargs())


def test_a_reference_on_other_instruments_is_refused() -> None:
    eth = make_bars("ETH", [Decimal(c) for c in ("100", "100", "110", "121", "110", "99", "99")])
    positions = tuple(p.model_copy(update={"instrument": "ETH"}) for p in STRATEGIES[A].positions)
    request = BacktestRequest(
        cost_model=ZERO, initial_equity=Decimal(1000), bars=eth, targets=positions
    )
    other = BarBacktester().run(request)
    with pytest.raises(DeviationError, match="does not price"):
        paper_deviation(_run(), other, **scope_kwargs())
    # a reference that answers its request, trades only BTC, but prices BTC and ETH
    both = BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        bars=BARS + eth,
        targets=STRATEGIES[A].positions,
    )
    wider = BarBacktester().run(both)
    with pytest.raises(DeviationError, match="the reference prices"):
        paper_deviation(_run(), wider, reference_request=both, **scope_kwargs())
    with pytest.raises(DeviationError, match="does not answer"):
        paper_deviation(
            _run(), reference(), reference_request=reference_request("2000"), **scope_kwargs()
        )


def test_a_tampered_run_is_refused() -> None:
    run = _run()
    tampered = dataclasses.replace(run, router="other@1.0.0")
    with pytest.raises(RouterError, match="run_hash"):
        paper_deviation(tampered, reference(), **scope_kwargs())


def test_only_a_run_and_a_backtest_result_are_compared() -> None:
    with pytest.raises(DeviationError):
        paper_deviation(_run(), _run(), **scope_kwargs())  # type: ignore[arg-type]


def test_scope_binding_is_required_and_fail_closed() -> None:
    with pytest.raises(DeviationError, match="requires a P8"):
        paper_deviation(_run(), reference())
    profile, report = scope_evidence()
    wrong_report = report.model_copy(update={"subject": A})
    with pytest.raises(DeviationError, match="not about this router"):
        paper_deviation(
            _run(), reference(), validation_profile=profile, validation_report=wrong_report
        )
    other_profile = validation_profile(
        name="other_scope",
        scope=ProfileScope(
            venue="testvenue", symbol="ETH", timeframe="1m", research_class="swing"
        ),
    )
    with pytest.raises(DeviationError, match="does not bind"):
        paper_deviation(
            _run(), reference(), validation_profile=other_profile, validation_report=report
        )
    changed_scope = ProfileScope(
        venue="testvenue", symbol="ETH", timeframe="1m", research_class="swing"
    )
    changed_profile = profile.model_copy(update={"scope": changed_scope})
    changed_report = report.model_copy(
        update={"validation_profile_hash": changed_profile.content_hash()}
    )
    with pytest.raises(DeviationError, match="exactly match"):
        paper_deviation(
            _run(), reference(), validation_profile=changed_profile, validation_report=changed_report
        )
    no_g5 = report.model_copy(
        update={
            "gates": (
                GateResult(gate_id="G0.repro", metric="repro", value=1.0, verdict=Verdict.PASS),
            )
        }
    )
    with pytest.raises(DeviationError, match="G5"):
        paper_deviation(_run(), reference(), validation_profile=profile, validation_report=no_g5)


def test_scope_payload_validation_rejects_legacy_and_tampering() -> None:
    payload = deviation().to_payload()
    scope = scope_kwargs()
    legacy = {**payload, "schema_version": "1.0.0"}
    with pytest.raises(DeviationError, match="schema 2.0.0"):
        validate_scope_bound_payload(legacy, **scope)
    changed_scope = {**payload, "declared_scope": {**payload["declared_scope"], "symbol": "ETH"}}
    with pytest.raises(DeviationError, match="scope hash"):
        validate_scope_bound_payload(changed_scope, **scope)
    changed_symbol = {**payload, "instruments": ["ETH"]}
    with pytest.raises(DeviationError, match="do not match"):
        validate_scope_bound_payload(changed_symbol, **scope)
