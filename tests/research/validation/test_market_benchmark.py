"""ADR-0060: the C-T4 market benchmark rule and the inverse control (reported-only G2 items).

!!! TEST ONLY !!! The Profile is ``fixtures.TEST_ONLY_PROFILE`` with only its ``benchmark`` block's
``market_benchmark_rule`` / ``inverse_control_reported`` changed; no value here is a research value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext

import pytest

from core.contracts.outcome import OutcomeEvent
from core.contracts.synthetic import SyntheticMarket
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict, derive_verdict
from research.outcomes import OutcomeTable
from research.validation import run_in_sample
from research.validation.benchmark import (
    BENCHMARK_UNAVAILABLE,
    MARKET_BENCHMARK_RULES,
    BenchmarkEvidence,
    BenchmarkKind,
    MarketBenchmarkRule,
    Unavailable,
    benchmark_gates,
    compounded_return,
    resolve_market_benchmark,
)
from research.validation.calibration import MomentumSignStudy
from research.validation.gates import CONFIGURATION_MISSING
from research.validation.returns import PeriodReturns
from tests.research.validation.fixtures import (
    TEST_ONLY_PROFILE,
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
)

T = datetime(2024, 1, 1, tzinfo=UTC)
D = Decimal
#: The module's own arithmetic precision (``research.validation.benchmark``).
PREC50 = Context(prec=50)


def _profile(rule: str, inverse: bool) -> ValidationProfile:
    """TEST ONLY: ``TEST_ONLY_PROFILE`` with another C-T4 benchmark rule / inverse flag."""
    data = TEST_ONLY_PROFILE.model_dump()
    data["benchmark"] = {
        **data["benchmark"],
        "market_benchmark_rule": rule,
        "inverse_control_reported": inverse,
    }
    return ValidationProfile.model_validate(data)


def _series(gross: Sequence[str], cost: Sequence[str] | None = None) -> PeriodReturns:
    times = tuple(T + timedelta(hours=i + 1) for i in range(len(gross)))
    costs = cost if cost is not None else ["0"] * len(gross)
    return PeriodReturns(
        times=times, gross=tuple(D(x) for x in gross), cost=tuple(D(x) for x in costs)
    )


class _Source:
    """Records every call; answers with fixed evidence."""

    def __init__(self, evidence: BenchmarkEvidence) -> None:
        self.evidence = evidence
        self.calls: list[tuple[MarketBenchmarkRule | None, bool]] = []

    def __call__(self, rule: MarketBenchmarkRule | None, inverse: bool) -> BenchmarkEvidence:
        self.calls.append((rule, inverse))
        return self.evidence


STRATEGY = _series(["0.02", "-0.01", "0.03"], ["0.001", "0.001", "0"])
MARKET = _series(["0.01", "0.01", "-0.02"], ["0.0005", "0", "0"])
INVERSE = _series(["-0.02", "0.01", "-0.03"], ["0.001", "0.001", "0"])


def _by_id(gates: Sequence[GateResult]) -> dict[str, GateResult]:
    return {gate.gate_id: gate for gate in gates}


# =========================================================================================
# registry
# =========================================================================================


def test_the_registered_rules_are_exactly_the_adr_ones() -> None:
    assert dict(MARKET_BENCHMARK_RULES) == {
        "none": MarketBenchmarkRule("none", BenchmarkKind.NOT_APPLICABLE),
        "buy_and_hold_equal_weight": MarketBenchmarkRule(
            "buy_and_hold_equal_weight", BenchmarkKind.EQUAL_WEIGHT_BUY_AND_HOLD
        ),
        "flat": MarketBenchmarkRule("flat", BenchmarkKind.ZERO_EXPOSURE),
    }
    # Exact names only: no normalisation turns a near miss into a registered rule.
    for near_miss in ("Flat", " flat", "buy-and-hold", "test-only", "by-class"):
        assert resolve_market_benchmark(near_miss) is None


def test_equal_weights_split_one_across_the_instruments() -> None:
    rule = MARKET_BENCHMARK_RULES["buy_and_hold_equal_weight"]
    assert rule.weights(["B", "A"]) == {"A": D("0.5"), "B": D("0.5")}
    assert rule.weights(["A"]) == {"A": D(1)}
    three = rule.weights(["A", "B", "C"])
    with localcontext(PREC50):
        assert set(three.values()) == {D(1) / D(3)} and sum(three.values()) <= 1
    assert MARKET_BENCHMARK_RULES["flat"].weights(["A"]) == {}
    with pytest.raises(ValueError, match="at least one"):
        rule.weights([])


def test_compounded_return_uses_the_net_series() -> None:
    expected = D("1.019") * D("0.989") * D("1.03") - 1
    assert compounded_return(STRATEGY) == expected


# =========================================================================================
# which items a Profile calls for
# =========================================================================================


def test_no_source_adds_nothing() -> None:
    assert benchmark_gates(_profile("buy_and_hold_equal_weight", True), None) == ()
    assert benchmark_gates(_profile("test-only", True), None) == ()


def test_an_unregistered_rule_is_inconclusive_and_never_computed() -> None:
    source = _Source(BenchmarkEvidence(strategy=STRATEGY))
    (gate,) = benchmark_gates(_profile("test-only", False), source)
    assert gate.gate_id == "G2.market_benchmark"
    assert gate.verdict is Verdict.INCONCLUSIVE
    assert gate.metric == f"{CONFIGURATION_MISSING}:benchmark.market_benchmark_rule=test-only"
    assert gate.threshold is None and gate.threshold_source is None
    assert source.calls == []  # nothing to compute


def test_an_unregistered_rule_still_reports_a_requested_inverse_control() -> None:
    source = _Source(BenchmarkEvidence(strategy=STRATEGY, inverse=INVERSE))
    gates = benchmark_gates(_profile("by-class", True), source)
    assert [(g.gate_id, g.verdict) for g in gates] == [
        ("G2.market_benchmark", Verdict.INCONCLUSIVE),
        ("G2.inverse_control", Verdict.PASS),
    ]
    assert source.calls == [(None, True)]


def test_none_is_not_applicable_and_adds_no_gate() -> None:
    source = _Source(BenchmarkEvidence(strategy=STRATEGY))
    assert benchmark_gates(_profile("none", False), source) == ()
    assert source.calls == []


def test_none_with_the_inverse_control_reports_only_the_inverse() -> None:
    source = _Source(BenchmarkEvidence(strategy=STRATEGY, inverse=INVERSE))
    (gate,) = benchmark_gates(_profile("none", True), source)
    assert (gate.gate_id, gate.metric, gate.verdict) == (
        "G2.inverse_control",
        "inverse_net_return",
        Verdict.PASS,
    )
    assert gate.value == float(compounded_return(INVERSE))
    assert gate.threshold is None
    assert source.calls == [(None, True)]


# =========================================================================================
# computed items
# =========================================================================================


def test_buy_and_hold_reports_the_excess_and_its_period_series() -> None:
    source = _Source(BenchmarkEvidence(strategy=STRATEGY, benchmark=MARKET))
    gates = _by_id(benchmark_gates(_profile("buy_and_hold_equal_weight", False), source))
    rule = MARKET_BENCHMARK_RULES["buy_and_hold_equal_weight"]
    assert source.calls == [(rule, False)]
    base = "G2.market_benchmark.buy_and_hold_equal_weight"
    assert list(gates) == [
        base,
        f"{base}.benchmark_net_return",
        f"{base}.period_excess_mean",
        f"{base}.period_excess_positive_fraction",
    ]
    assert all(g.verdict is Verdict.PASS and g.threshold is None for g in gates.values())
    excess = [s - m for s, m in zip(STRATEGY.net(), MARKET.net(), strict=True)]
    assert gates[base].value == float(compounded_return(STRATEGY) - compounded_return(MARKET))
    assert gates[base].metric == "net_excess_return_vs_benchmark"
    assert gates[f"{base}.benchmark_net_return"].value == float(compounded_return(MARKET))
    with localcontext(PREC50):
        mean, two_thirds = sum(excess, D(0)) / 3, D(2) / D(3)
    assert gates[f"{base}.period_excess_mean"].value == float(mean)
    # excess per period: 0.0095, -0.021, 0.05 -> two of three positive
    assert gates[f"{base}.period_excess_positive_fraction"].value == float(two_thirds)


def test_flat_is_zero_exposure_and_needs_no_rerun() -> None:
    source = _Source(BenchmarkEvidence(strategy=STRATEGY))
    gates = _by_id(benchmark_gates(_profile("flat", False), source))
    assert source.calls == [(None, False)]  # the zero series is built here, not re-run
    base = "G2.market_benchmark.flat"
    assert gates[base].value == float(compounded_return(STRATEGY))
    assert gates[f"{base}.benchmark_net_return"].value == 0.0
    with localcontext(PREC50):
        two_thirds = D(2) / D(3)
    assert gates[f"{base}.period_excess_positive_fraction"].value == float(two_thirds)


def test_a_negative_excess_is_reported_and_never_decides() -> None:
    """Reported only: a strategy that trails its benchmark and whose inverse earns keeps its
    verdict (the C-T4 threshold is the null model)."""
    losing = _series(["-0.05", "-0.05"])
    evidence = BenchmarkEvidence(
        strategy=losing, benchmark=_series(["0.05", "0.05"]), inverse=_series(["0.05", "0.05"])
    )
    gates = benchmark_gates(_profile("buy_and_hold_equal_weight", True), _Source(evidence))
    assert gates[0].value < 0 and gates[-1].value > 0
    assert {g.verdict for g in gates} == {Verdict.PASS}
    passing = GateResult(gate_id="G3.adjusted_p_value", metric="p", value=0.0, verdict=Verdict.PASS)
    failing = passing.model_copy(update={"verdict": Verdict.FAIL})
    assert derive_verdict((passing, *gates)) is Verdict.PASS
    assert derive_verdict((failing, *gates)) is Verdict.FAIL


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (BenchmarkEvidence(strategy=STRATEGY), "benchmark_not_computed"),
        (
            BenchmarkEvidence(strategy=STRATEGY, benchmark=Unavailable("rerun_failed:X")),
            "rerun_failed:X",
        ),
        (
            BenchmarkEvidence(strategy=STRATEGY, benchmark=_series(["0.01", "0.02"])),
            "period_grid_mismatch",
        ),
    ],
)
def test_missing_benchmark_evidence_is_inconclusive(
    evidence: BenchmarkEvidence, reason: str
) -> None:
    (gate,) = benchmark_gates(_profile("buy_and_hold_equal_weight", False), _Source(evidence))
    assert gate.gate_id == "G2.market_benchmark.buy_and_hold_equal_weight"
    assert (gate.verdict, gate.metric) == (
        Verdict.INCONCLUSIVE,
        f"{BENCHMARK_UNAVAILABLE}:{reason}",
    )


@pytest.mark.parametrize(
    ("inverse", "reason"),
    [
        (None, "inverse_not_computed"),
        (Unavailable("execution_model_not_reproduced"), "execution_model_not_reproduced"),
        (_series(["0.01"]), "period_grid_mismatch"),
    ],
)
def test_missing_inverse_evidence_is_inconclusive(
    inverse: PeriodReturns | Unavailable | None, reason: str
) -> None:
    evidence = BenchmarkEvidence(strategy=STRATEGY, inverse=inverse)
    (gate,) = benchmark_gates(_profile("none", True), _Source(evidence))
    assert gate.gate_id == "G2.inverse_control"
    assert (gate.verdict, gate.metric) == (
        Verdict.INCONCLUSIVE,
        f"{BENCHMARK_UNAVAILABLE}:{reason}",
    )


def test_an_empty_strategy_series_is_inconclusive() -> None:
    empty = PeriodReturns(times=(), gross=(), cost=())
    (gate,) = benchmark_gates(_profile("flat", False), _Source(BenchmarkEvidence(strategy=empty)))
    assert (gate.verdict, gate.metric) == (
        Verdict.INCONCLUSIVE,
        f"{BENCHMARK_UNAVAILABLE}:no_periods",
    )


def test_an_unavailable_reason_must_be_given() -> None:
    with pytest.raises(ValueError, match="reason"):
        Unavailable(" ")


# =========================================================================================
# in the in-sample pipeline
# =========================================================================================


@pytest.fixture(scope="module")
def planted() -> tuple[SyntheticMarket, OutcomeTable]:
    market = generate(seed=3, strength="0.6")
    return market, outcome_table(market, research_events(market))


def _study(market: SyntheticMarket, table: OutcomeTable) -> MomentumSignStudy:
    events = [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]
    return MomentumSignStudy(market, events)


def test_the_items_close_g2_and_leave_every_other_gate_unchanged(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    profile = _profile("buy_and_hold_equal_weight", True)
    base = in_sample_input(table, _study(market, table), ctx=context(profile))
    source = _Source(BenchmarkEvidence(strategy=STRATEGY, benchmark=MARKET, inverse=INVERSE))
    without = run_in_sample(base)
    with_items = run_in_sample(replace(base, benchmark=source))
    added = [g for g in with_items if g not in without]
    assert [g for g in with_items if g in without] == list(without)
    assert [g.gate_id for g in added] == [
        "G2.market_benchmark.buy_and_hold_equal_weight",
        "G2.market_benchmark.buy_and_hold_equal_weight.benchmark_net_return",
        "G2.market_benchmark.buy_and_hold_equal_weight.period_excess_mean",
        "G2.market_benchmark.buy_and_hold_equal_weight.period_excess_positive_fraction",
        "G2.inverse_control",
    ]
    ids = [g.gate_id for g in with_items]
    # the items end G2, before G3
    assert ids.index("G2.inverse_control") + 1 == ids.index("G3.adjusted_p_value")
    assert derive_verdict(with_items) is derive_verdict(without)
    assert len(source.calls) == 1


def test_the_source_is_not_called_when_g2_is_not_reached(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    profile = _profile("buy_and_hold_equal_weight", True)
    source = _Source(BenchmarkEvidence(strategy=STRATEGY, benchmark=MARKET, inverse=INVERSE))
    inp = in_sample_input(
        table, _study(market, table), ctx=context(profile), reproduced_hash="0" * 64
    )
    gates = run_in_sample(replace(inp, benchmark=source))
    assert {g.gate_id.split(".")[0] for g in gates} == {"G0"}
    assert source.calls == []


def test_an_unregistered_rule_makes_the_report_inconclusive_never_pass(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    market, table = planted
    inp = in_sample_input(table, _study(market, table))  # TEST_ONLY_PROFILE: "test-only"
    assert derive_verdict(run_in_sample(inp)) is Verdict.PASS
    gates = run_in_sample(replace(inp, benchmark=_Source(BenchmarkEvidence(strategy=STRATEGY))))
    assert derive_verdict(gates) is Verdict.INCONCLUSIVE
    (gap,) = [g for g in gates if g.verdict is not Verdict.PASS]
    assert gap.gate_id == "G2.market_benchmark"
