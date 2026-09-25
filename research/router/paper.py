"""Router paper run: routing weights x P5 target positions -> one simulated book (W1 wiring).

Research code (H5), paper / simulation only: nothing here places an order, holds a key or opens a
connection; the only execution is a ``BacktestProvider`` (simulation only by contract, ADR-0038).

``paper_run(router, states, strategies, ...)``:

1. **Route.** ``StrategyRouter.route`` over the P2 ``StateResult`` (one decision per evaluation
   time ``t``, from the state known at ``t``; ADR-0043).
2. **Combine.** For every decision time ``t`` and every instrument of the routed strategies, the
   combined target weight is ``sum_s w_s(t) * target_s(t, instrument)``, where ``target_s`` is the
   latest P5 ``TargetPosition`` of strategy ``s`` with ``decision_time <= t`` (as-of; a strategy
   with no position yet contributes nothing). ``inputs_used`` is the sum of the contributing
   positions' ``inputs_used`` and ``latest_input_available_time`` their maximum, so a combined
   target without information is flat, as the contract requires. Weights are quantized to
   ``WEIGHT_QUANTUM`` (half-even, 50 digits).
3. **Simulate.** The combined targets run through the injected ``BacktestProvider`` (``gross``,
   checked with ``check_answers``). It charges the request's cost model on instrument trades.
4. **Switching cost.** Every decision with non-zero router turnover is charged
   ``switching_cost_rate x turnover x E``, where ``E`` is the net equity at the latest equity mark
   at or before the decision (the initial equity before the first mark; nothing when ``E <= 0``).
   The charge is paid at the first equity mark **after** the decision, from cash, and is carried
   by every later mark. The simulated book is not re-sized for the charges (they are paid from the
   book's cash after the fact); a charge whose decision has no later mark is recorded but not
   paid. This cost is on top of the cost model: it prices strategy-allocation churn, which the
   instrument-level cost model does not see; a spec that wants only the cost model sets the rate
   to 0.
5. **The router's own result.** ``result`` is a ``BacktestResult`` over the same request, with the
   gross fills and the net equity curve, attributed to ``ROUTER_PAPER_BACKTEST`` — the router as
   a strategy object whose backtest can be validated like any other (P4 / P8, later).

Honest boundary: ``result``'s ``request_hash`` / ``provider_hash`` do not identify the router spec
or the inner backtester. ``RouterPaperRun.run_hash`` binds everything: router spec hash, state
result hash, every strategy result hash, decisions, the gross and the net result hashes and the
charges. Nothing here is validated; there is no threshold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.state import StateResult
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    PriceBar,
    StrategyResult,
    TargetPosition,
)
from core.domain.base import Ref, content_hash
from research.router.router import RouterError, RoutingDecision, StrategyRouter

__all__ = [
    "MONEY_QUANTUM",
    "ROUTER_PAPER_BACKTEST",
    "WEIGHT_QUANTUM",
    "RouterPaperRun",
    "SwitchingCharge",
    "combine_targets",
    "paper_run",
]

#: Combined target weights are quantized to this step.
WEIGHT_QUANTUM: Final = Decimal("1e-18")
#: Switching charges and net cash / equity are quantized to this step (as the P5 backtester's).
MONEY_QUANTUM: Final = Decimal("1e-18")
#: The identity of the router's own (net of switching cost) backtest result.
ROUTER_PAPER_BACKTEST: Final = BacktestProviderDescriptor(
    name="research_router_paper",
    version="0.1.0",
    deterministic=True,
    simulation_only=True,
    execution_model="next_bar_open",
)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class SwitchingCharge:
    decision_time: datetime
    turnover: Decimal
    rate: Decimal
    #: Net equity at the latest mark at or before the decision (initial equity before any mark).
    equity_base: Decimal
    amount: Decimal
    #: The equity mark that pays it; ``None`` when no mark follows the decision (not paid).
    charged_at: datetime | None


@dataclass(frozen=True, slots=True)
class RouterPaperRun:
    router: str
    router_spec_hash: str
    state_result_hash: str
    #: strategy ref string -> ``StrategyResult.result_hash`` of the positions that were combined
    strategy_result_hashes: Mapping[str, str]
    decisions: tuple[RoutingDecision, ...]
    targets: tuple[TargetPosition, ...]
    request: BacktestRequest
    #: The injected backtester on the combined targets (instrument costs only).
    gross: BacktestResult
    charges: tuple[SwitchingCharge, ...]
    #: The router's own result: ``gross`` net of the paid switching charges.
    result: BacktestResult
    run_hash: str

    @property
    def total_switching_cost(self) -> Decimal:
        return sum((c.amount for c in self.charges if c.charged_at is not None), Decimal(0))


def _positions_by_strategy(
    router: StrategyRouter, strategies: Mapping[Ref, StrategyResult]
) -> dict[str, tuple[TargetPosition, ...]]:
    given = {str(ref): result for ref, result in strategies.items()}
    for key, result in given.items():
        if not isinstance(result, StrategyResult):
            raise RouterError(f"{key}: expected a StrategyResult")
    routed = router.spec.strategies()
    missing = sorted(routed - set(given))
    extra = sorted(set(given) - routed)
    if missing:
        raise RouterError(f"no StrategyResult for routed strategies {missing}")
    if extra:
        raise RouterError(f"StrategyResults for strategies the router never routes: {extra}")
    return {key: given[key].positions for key in sorted(given)}


def combine_targets(
    decisions: Sequence[RoutingDecision],
    positions: Mapping[str, Sequence[TargetPosition]],
) -> tuple[TargetPosition, ...]:
    """Combined targets at every decision time (see module docs, step 2).

    ``positions`` maps a strategy ref string to its positions in ``(decision_time, instrument)``
    order (as ``StrategyResult.positions``).
    """
    instruments = sorted({item.instrument for items in positions.values() for item in items})
    cursor = dict.fromkeys(positions, 0)
    current: dict[str, dict[str, TargetPosition]] = {key: {} for key in positions}
    out: list[TargetPosition] = []
    with localcontext(_CONTEXT):
        for decision in decisions:
            for key, items in positions.items():  # advance each strategy to decision_time <= t
                index = cursor[key]
                while index < len(items) and items[index].decision_time <= decision.at:
                    current[key][items[index].instrument] = items[index]
                    index += 1
                cursor[key] = index
            for instrument in instruments:
                total = Decimal(0)
                used = 0
                latest: datetime | None = None
                for key, weight in sorted(decision.weights.items()):
                    position = current.get(key, {}).get(instrument)
                    if weight == 0 or position is None or position.inputs_used == 0:
                        continue
                    total += weight * position.target_weight
                    used += position.inputs_used
                    stamp = position.latest_input_available_time
                    if stamp is not None and (latest is None or stamp > latest):
                        latest = stamp
                out.append(
                    TargetPosition(
                        decision_time=decision.at,
                        instrument=instrument,
                        target_weight=total.quantize(WEIGHT_QUANTUM) if used else Decimal(0),
                        inputs_used=used,
                        latest_input_available_time=latest if used else None,
                    )
                )
    return tuple(out)


def _net_curve(
    decisions: Sequence[RoutingDecision],
    gross: BacktestResult,
    rate: Decimal,
) -> tuple[tuple[EquityPoint, ...], tuple[SwitchingCharge, ...]]:
    switches = [d for d in decisions if d.turnover != 0]
    charges: list[SwitchingCharge] = []
    points: list[EquityPoint] = []
    paid = Decimal(0)
    last_net = gross.initial_equity
    index = 0
    with localcontext(_CONTEXT):
        for point in gross.equity_curve:
            while index < len(switches) and switches[index].at < point.time:
                decision = switches[index]
                amount = (rate * decision.turnover * max(last_net, Decimal(0))).quantize(
                    MONEY_QUANTUM
                )
                charges.append(
                    SwitchingCharge(
                        decision.at, decision.turnover, rate, last_net, amount, point.time
                    )
                )
                paid += amount
                index += 1
            net = EquityPoint(
                time=point.time,
                cash=(point.cash - paid).quantize(MONEY_QUANTUM),
                equity=(point.equity - paid).quantize(MONEY_QUANTUM),
                gross_exposure=point.gross_exposure,
            )
            points.append(net)
            last_net = net.equity
        for decision in switches[index:]:
            amount = (rate * decision.turnover * max(last_net, Decimal(0))).quantize(MONEY_QUANTUM)
            charges.append(
                SwitchingCharge(decision.at, decision.turnover, rate, last_net, amount, None)
            )
    return tuple(points), tuple(charges)


def _run_hash(
    router: StrategyRouter,
    states: StateResult,
    strategy_hashes: Mapping[str, str],
    decisions: Sequence[RoutingDecision],
    gross: BacktestResult,
    charges: Sequence[SwitchingCharge],
    result: BacktestResult,
) -> str:
    return content_hash(
        {
            "router": f"{router.spec.name}@{router.spec.version}",
            "router_spec_hash": router.spec.spec_hash(),
            "state_result_hash": states.result_hash,
            "strategy_result_hashes": dict(strategy_hashes),
            "decisions": [
                {
                    "at": d.at.isoformat(),
                    "state": d.state,
                    "weights": {key: str(value) for key, value in sorted(d.weights.items())},
                    "turnover": str(d.turnover),
                }
                for d in decisions
            ],
            "request_hash": result.request_hash,
            "gross_result_hash": gross.result_hash,
            "charges": [
                {
                    "decision_time": c.decision_time.isoformat(),
                    "turnover": str(c.turnover),
                    "rate": str(c.rate),
                    "equity_base": str(c.equity_base),
                    "amount": str(c.amount),
                    "charged_at": None if c.charged_at is None else c.charged_at.isoformat(),
                }
                for c in charges
            ],
            "result_hash": result.result_hash,
        }
    )


def paper_run(
    router: StrategyRouter,
    states: StateResult,
    strategies: Mapping[Ref, StrategyResult],
    *,
    bars: Sequence[PriceBar],
    cost_model: BacktestCostModel,
    initial_equity: Decimal,
    backtester: BacktestProvider,
) -> RouterPaperRun:
    """Route, combine, simulate and charge switching costs (module docs). Paper only."""
    if not isinstance(router, StrategyRouter) or not isinstance(states, StateResult):
        raise RouterError("paper_run needs a StrategyRouter and a StateResult")
    positions = _positions_by_strategy(router, strategies)
    decisions = router.route([(value.evaluation_time, value.state) for value in states.values])
    targets = combine_targets(decisions, positions)
    request = BacktestRequest(
        cost_model=cost_model,
        initial_equity=initial_equity,
        bars=tuple(bars),
        targets=targets,
    )
    gross = backtester.run(request)
    gross.check_answers(request, backtester.descriptor)
    curve, charges = _net_curve(decisions, gross, router.spec.switching_cost_rate)
    result = BacktestResult.build(
        request,
        ROUTER_PAPER_BACKTEST,
        fills=gross.fills,
        equity_curve=curve,
        unexecuted_targets=gross.unexecuted_targets,
    )
    result.check_answers(request, ROUTER_PAPER_BACKTEST)
    strategy_hashes = {str(ref): answer.result_hash for ref, answer in strategies.items()}
    return RouterPaperRun(
        router=f"{router.spec.name}@{router.spec.version}",
        router_spec_hash=router.spec.spec_hash(),
        state_result_hash=states.result_hash,
        strategy_result_hashes=dict(sorted(strategy_hashes.items())),
        decisions=decisions,
        targets=targets,
        request=request,
        gross=gross,
        charges=charges,
        result=result,
        run_hash=_run_hash(router, states, strategy_hashes, decisions, gross, charges, result),
    )
