"""C-T4 market benchmark rule and inverse control: reported-only G2 items (ADR-0060).

Implementation note (Phase 4 / 5, 2026-09-26; CODE_COMPLETE / DEBUG_PENDING). No core / contract /
Schema change: ``BenchmarkParams.market_benchmark_rule`` / ``.inverse_control_reported`` already
exist; this module gives them their ADR-0060 semantics.

``benchmark.market_benchmark_rule`` names a **registered** rule (``MARKET_BENCHMARK_RULES``, exact
name, no normalisation):

- ``none`` — the strategy class has no market benchmark: "not applicable", **no gate**;
- ``buy_and_hold_equal_weight`` — an equal-weight (``1 / N``) buy-and-hold of the **same**
  instruments over the **same** research window (entered at the strategy's first decision time,
  never rebalanced), simulated by the **same** execution model and cost model as the strategy;
- ``flat`` — zero exposure (cash): every period's return and cost is zero.

Any other name is ``G2.market_benchmark`` = ``INCONCLUSIVE`` (metric
``configuration_missing:benchmark.market_benchmark_rule=<name>``) — never a PASS, and there is no
fallback rule. A computed benchmark is reported as ``G2.market_benchmark.<rule>`` (the strategy's
compounded net return minus the benchmark's), ``G2.market_benchmark.<rule>.benchmark_net_return``,
``G2.market_benchmark.<rule>.period_excess_mean`` and
``G2.market_benchmark.<rule>.period_excess_positive_fraction`` (the per-period net excess
series: its mean, and the fraction of all periods with a positive excess). With
``benchmark.inverse_control_reported`` true, ``G2.inverse_control`` reports the compounded net
return of the same strategy with **every target position negated**, through the same execution
and cost model; false computes nothing.

Every computed item is **reported only**: ``verdict = PASS`` means "computed", with no threshold
(exactly like ``G2.cost_report.<i>``), so it never changes ``derive_verdict``. The C-T4 threshold
is the null model (``G2.null_model_percentile``); a market-benchmark excess threshold would need
its own ADR and a Profile number. Evidence that cannot be produced (the caller could not re-run
the same execution model, a re-run raised, the benchmark's period grid is not the strategy's) is
``INCONCLUSIVE`` with metric ``benchmark_unavailable:<reason>`` — absence is never a PASS.

The evidence comes from a ``BenchmarkSource`` the caller supplies (``InSampleInput.benchmark``):
``research.strategies.validation`` re-runs the chosen trial's positions through its backtester. The
re-runs are robustness re-runs of the **same** trial; they never add a trial (``family_trial_count``
is unchanged). A source is called lazily, only when G2 is reached and only when something must be
computed (``none`` + no inverse control calls nothing). Without a source (``None``, the default of
the label-only pipeline, which has no price path) nothing is added and the report is byte-identical
to before this note.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict
from research.validation.gates import configuration_missing_gate, inconclusive_gate
from research.validation.returns import PeriodReturns

__all__ = [
    "BENCHMARK_UNAVAILABLE",
    "INVERSE_CONTROL_GATE",
    "MARKET_BENCHMARK_GATE",
    "MARKET_BENCHMARK_RULES",
    "BenchmarkEvidence",
    "BenchmarkKind",
    "BenchmarkSource",
    "MarketBenchmarkRule",
    "Unavailable",
    "benchmark_gates",
    "compounded_return",
    "resolve_market_benchmark",
]

#: Gate id of the market benchmark (``<id>.<rule>`` when computed; bare id when unregistered).
MARKET_BENCHMARK_GATE: Final = "G2.market_benchmark"
#: Gate id of the inverse control.
INVERSE_CONTROL_GATE: Final = "G2.inverse_control"
#: Metric prefix of a reported item whose evidence could not be produced.
BENCHMARK_UNAVAILABLE: Final = "benchmark_unavailable"

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


class BenchmarkKind(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ZERO_EXPOSURE = "zero_exposure"
    EQUAL_WEIGHT_BUY_AND_HOLD = "equal_weight_buy_and_hold"


@dataclass(frozen=True)
class MarketBenchmarkRule:
    """A registered ``benchmark.market_benchmark_rule``."""

    name: str
    kind: BenchmarkKind

    def weights(self, instruments: Sequence[str]) -> dict[str, Decimal]:
        """The benchmark's target weights over ``instruments`` (empty: no position)."""
        if self.kind is not BenchmarkKind.EQUAL_WEIGHT_BUY_AND_HOLD:
            return {}
        names = sorted(set(instruments))
        if not names:
            raise ValueError("an equal-weight benchmark needs at least one instrument")
        with localcontext(_CONTEXT):
            weight = Decimal(1) / Decimal(len(names))
        return dict.fromkeys(names, weight)


#: The registered rules (ADR-0060 §1); a name outside this registry is never computed.
MARKET_BENCHMARK_RULES: Final[Mapping[str, MarketBenchmarkRule]] = MappingProxyType(
    {
        rule.name: rule
        for rule in (
            MarketBenchmarkRule("none", BenchmarkKind.NOT_APPLICABLE),
            MarketBenchmarkRule(
                "buy_and_hold_equal_weight", BenchmarkKind.EQUAL_WEIGHT_BUY_AND_HOLD
            ),
            MarketBenchmarkRule("flat", BenchmarkKind.ZERO_EXPOSURE),
        )
    }
)


def resolve_market_benchmark(name: str) -> MarketBenchmarkRule | None:
    """The registered rule of exactly ``name``, or ``None`` (unregistered: INCONCLUSIVE)."""
    return MARKET_BENCHMARK_RULES.get(name)


@dataclass(frozen=True)
class Unavailable:
    """Evidence the source could not produce, and why (reported as INCONCLUSIVE)."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("an unavailable benchmark needs its reason")


@dataclass(frozen=True)
class BenchmarkEvidence:
    """What a source produced for one trial.

    - ``strategy``: the chosen trial's own period returns (the re-run G0 reproduced);
    - ``benchmark``: the rule's baseline re-run (``None`` when not requested: ``none``, ``flat``,
      an unregistered name);
    - ``inverse``: the negated-position re-run (``None`` when not requested).
    """

    strategy: PeriodReturns
    benchmark: PeriodReturns | Unavailable | None = None
    inverse: PeriodReturns | Unavailable | None = None


#: ``source(rule, inverse)``: ``rule`` is the rule whose baseline must be re-run (only an
#: equal-weight buy-and-hold; ``None`` otherwise); ``inverse`` whether the inverse control is due.
BenchmarkSource = Callable[[MarketBenchmarkRule | None, bool], BenchmarkEvidence]


def compounded_return(returns: PeriodReturns) -> Decimal:
    """The compounded net return (cost multiplier 1) of the series: ``prod(1 + net) - 1``."""
    with localcontext(_CONTEXT):
        growth = Decimal(1)
        for item in returns.net():
            growth *= 1 + item
        return growth - 1


def _reported(gate_id: str, metric: str, value: Decimal) -> GateResult:
    """A reported-only item: no threshold, ``PASS`` = computed (never decides)."""
    return GateResult(gate_id=gate_id, metric=metric, value=float(value), verdict=Verdict.PASS)


def _unavailable(gate_id: str, reason: str) -> GateResult:
    return inconclusive_gate(gate_id, f"{BENCHMARK_UNAVAILABLE}:{reason}", 0.0)


def _flat(times: PeriodReturns) -> PeriodReturns:
    zeros = tuple(Decimal(0) for _ in times.times)
    return PeriodReturns(times=times.times, gross=zeros, cost=zeros)


def _market_gates(rule: MarketBenchmarkRule, evidence: BenchmarkEvidence) -> tuple[GateResult, ...]:
    gate_id = f"{MARKET_BENCHMARK_GATE}.{rule.name}"
    strategy = evidence.strategy
    if rule.kind is BenchmarkKind.ZERO_EXPOSURE:
        baseline: PeriodReturns | Unavailable | None = _flat(strategy)
    else:
        baseline = evidence.benchmark
    if baseline is None:
        return (_unavailable(gate_id, "benchmark_not_computed"),)
    if isinstance(baseline, Unavailable):
        return (_unavailable(gate_id, baseline.reason),)
    if baseline.times != strategy.times:
        return (_unavailable(gate_id, "period_grid_mismatch"),)
    if not strategy.times:
        return (_unavailable(gate_id, "no_periods"),)
    with localcontext(_CONTEXT):
        excess = [s - b for s, b in zip(strategy.net(), baseline.net(), strict=True)]
        mean = sum(excess, Decimal(0)) / len(excess)
        positive = Decimal(sum(1 for item in excess if item > 0)) / len(excess)
        benchmark_return = compounded_return(baseline)
        total = compounded_return(strategy) - benchmark_return
    return (
        _reported(gate_id, "net_excess_return_vs_benchmark", total),
        _reported(f"{gate_id}.benchmark_net_return", "benchmark_net_return", benchmark_return),
        _reported(f"{gate_id}.period_excess_mean", "mean_per_period_net_excess_return", mean),
        _reported(
            f"{gate_id}.period_excess_positive_fraction",
            "fraction_of_periods_with_positive_net_excess",
            positive,
        ),
    )


def _inverse_gate(evidence: BenchmarkEvidence) -> GateResult:
    inverse = evidence.inverse
    if inverse is None:
        return _unavailable(INVERSE_CONTROL_GATE, "inverse_not_computed")
    if isinstance(inverse, Unavailable):
        return _unavailable(INVERSE_CONTROL_GATE, inverse.reason)
    if inverse.times != evidence.strategy.times:
        return _unavailable(INVERSE_CONTROL_GATE, "period_grid_mismatch")
    return _reported(INVERSE_CONTROL_GATE, "inverse_net_return", compounded_return(inverse))


def benchmark_gates(
    profile: ValidationProfile, source: BenchmarkSource | None
) -> tuple[GateResult, ...]:
    """The ADR-0060 items of ``profile`` (module docs); ``()`` without a source."""
    if source is None:
        return ()
    params = profile.benchmark
    rule = resolve_market_benchmark(params.market_benchmark_rule)
    market = rule is not None and rule.kind is not BenchmarkKind.NOT_APPLICABLE
    inverse = params.inverse_control_reported
    gates: list[GateResult] = []
    if rule is None:
        gates.append(
            configuration_missing_gate(
                MARKET_BENCHMARK_GATE,
                f"benchmark.market_benchmark_rule={params.market_benchmark_rule}",
            )
        )
    if not market and not inverse:
        return tuple(gates)
    rerun = (
        rule if rule is not None and rule.kind is BenchmarkKind.EQUAL_WEIGHT_BUY_AND_HOLD else None
    )
    evidence = source(rerun, inverse)
    if market and rule is not None:
        gates.extend(_market_gates(rule, evidence))
    if inverse:
        gates.append(_inverse_gate(evidence))
    return tuple(gates)
