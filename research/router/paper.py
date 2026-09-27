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
charges (``RouterPaperRun.verify`` recomputes it from the recorded fields, so a tampered record
is refused). Nothing here is validated; there is no threshold.

Code completion (2026-09-26, CODE_COMPLETE / DEBUG_PENDING):

- **Validation report binding (optional).** ``paper_run(..., validation_reports=...)`` takes a
  mapping strategy ref -> validation report content hash. When supplied, every strategy the spec
  can route must have exactly one report (missing or extra entries are refused), and the mapping
  is recorded in ``RouterPaperRun.validation_reports`` and bound into ``run_hash``. When omitted
  (``None``, the default) nothing is recorded and ``run_hash`` is byte-identical to before. The
  hashes are recorded evidence references; in trust mode nothing opens or judges the reports.
- **Explicit stop.** ``paper_run_or_stop(spec, lifecycle, ...)`` builds the router and, when it
  is ``RouterStopped`` (no validated candidate, or every route flat; ``router.py``), returns a
  ``RouterStop`` record — reason, spec hash, lifecycle snapshot, input hashes and its own
  ``stop_hash`` — instead of a flat run that could be mistaken for a result. Nothing is simulated
  for a stopped router. Otherwise it is exactly ``paper_run``.
- **Eligibility evidence (optional, P10-ELIG).** A router built in evidence mode
  (``StrategyRouter(..., evidence=...)``, ``evidence.py``) has verified every routed strategy's
  ``ValidationReport``. ``paper_run`` then records ``RouterPaperRun.eligibility`` (per strategy:
  claimed lifecycle, report hash, subject, verdict, G5 gates) and the verified report hashes as
  ``validation_reports`` (a supplied ``validation_reports`` must equal them), both bound into
  ``run_hash``. ``paper_run_or_stop(..., evidence=...)`` records an evidence refusal as a
  ``RouterStop`` with reason ``eligibility_not_evidenced`` and every check (``eligibility``,
  bound into ``stop_hash``). Trust mode records nothing new: its hashes are unchanged.
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
from core.lifecycle.strategy import LifecycleState
from research.router.evidence import EligibilityCheck, EligibilityEvidence
from research.router.router import (
    RouterError,
    RouterSpec,
    RouterStopped,
    RouterStopReason,
    RoutingDecision,
    StrategyRouter,
    normalize_report_hashes,
)

__all__ = [
    "MONEY_QUANTUM",
    "ROUTER_PAPER_BACKTEST",
    "WEIGHT_QUANTUM",
    "RouterPaperRun",
    "RouterStop",
    "SwitchingCharge",
    "combine_targets",
    "paper_run",
    "paper_run_or_stop",
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
    #: strategy ref string -> validation report content hash, when the caller supplied them
    #: (``None``: not supplied, not recorded, not hashed).
    validation_reports: Mapping[str, str] | None = None
    #: Evidence mode only: the verified eligibility check of every routable strategy
    #: (``None``: trust mode, not recorded, not hashed).
    eligibility: tuple[EligibilityCheck, ...] | None = None

    @property
    def total_switching_cost(self) -> Decimal:
        return sum((c.amount for c in self.charges if c.charged_at is not None), Decimal(0))

    def expected_run_hash(self) -> str:
        """``run_hash`` recomputed from the recorded fields."""
        return _run_hash(
            router=self.router,
            router_spec_hash=self.router_spec_hash,
            state_result_hash=self.state_result_hash,
            strategy_hashes=self.strategy_result_hashes,
            decisions=self.decisions,
            request_hash=self.result.request_hash,
            gross_result_hash=self.gross.result_hash,
            charges=self.charges,
            result_hash=self.result.result_hash,
            validation_reports=self.validation_reports,
            eligibility=self.eligibility,
        )

    def verify(self) -> None:
        """Refuse a record whose fields no longer match its ``run_hash`` or its request."""
        request_hash = self.request.content_hash()
        if self.result.request_hash != request_hash or self.gross.request_hash != request_hash:
            raise RouterError("router paper run: results do not answer the recorded request")
        if self.expected_run_hash() != self.run_hash:
            raise RouterError("router paper run: run_hash does not match the recorded fields")


@dataclass(frozen=True, slots=True)
class RouterStop:
    """A paper run that did not happen because the router stopped (``RouterStopped``)."""

    router: str
    router_spec_hash: str
    reason: RouterStopReason
    detail: str
    #: strategy ref string -> lifecycle state value, as declared by the caller
    lifecycle: Mapping[str, str]
    state_result_hash: str
    strategy_result_hashes: Mapping[str, str]
    validation_reports: Mapping[str, str] | None
    stop_hash: str
    #: Evidence mode only: every eligibility check that was run (empty: the router stopped
    #: before verifying evidence); ``None`` in trust mode (not recorded, not hashed).
    eligibility: tuple[EligibilityCheck, ...] | None = None


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
    *,
    router: str,
    router_spec_hash: str,
    state_result_hash: str,
    strategy_hashes: Mapping[str, str],
    decisions: Sequence[RoutingDecision],
    request_hash: str,
    gross_result_hash: str,
    charges: Sequence[SwitchingCharge],
    result_hash: str,
    validation_reports: Mapping[str, str] | None,
    eligibility: Sequence[EligibilityCheck] | None = None,
) -> str:
    payload: dict[str, object] = {
        "router": router,
        "router_spec_hash": router_spec_hash,
        "state_result_hash": state_result_hash,
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
        "request_hash": request_hash,
        "gross_result_hash": gross_result_hash,
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
        "result_hash": result_hash,
    }
    if validation_reports is not None:  # no key when not supplied: earlier hashes are unchanged
        payload["validation_reports"] = dict(validation_reports)
    if eligibility is not None:  # evidence mode only: trust-mode hashes are unchanged
        payload["eligibility"] = [check.to_dict() for check in eligibility]
    return content_hash(payload)


def _check_reports(router: StrategyRouter, reports: Mapping[str, str]) -> None:
    routed = router.spec.strategies()
    missing = sorted(routed - set(reports))
    extra = sorted(set(reports) - routed)
    if missing:
        raise RouterError(f"routed strategies without a validation report: {missing}")
    if extra:
        raise RouterError(f"validation reports for strategies the router never routes: {extra}")


def paper_run(
    router: StrategyRouter,
    states: StateResult,
    strategies: Mapping[Ref, StrategyResult],
    *,
    bars: Sequence[PriceBar],
    cost_model: BacktestCostModel,
    initial_equity: Decimal,
    backtester: BacktestProvider,
    validation_reports: Mapping[Ref, str] | Mapping[str, str] | None = None,
) -> RouterPaperRun:
    """Route, combine, simulate and charge switching costs (module docs). Paper only."""
    if not isinstance(router, StrategyRouter) or not isinstance(states, StateResult):
        raise RouterError("paper_run needs a StrategyRouter and a StateResult")
    positions = _positions_by_strategy(router, strategies)
    reports = None if validation_reports is None else normalize_report_hashes(validation_reports)
    if router.report_hashes is not None:  # evidence mode: record the verified hashes
        if reports is None:
            reports = dict(router.report_hashes)
        elif reports != router.report_hashes:
            raise RouterError("validation_reports differ from the router's verified evidence")
    if reports is not None:
        _check_reports(router, reports)
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
    name = f"{router.spec.name}@{router.spec.version}"
    spec_hash = router.spec.spec_hash()
    return RouterPaperRun(
        router=name,
        router_spec_hash=spec_hash,
        state_result_hash=states.result_hash,
        strategy_result_hashes=dict(sorted(strategy_hashes.items())),
        decisions=decisions,
        targets=targets,
        request=request,
        gross=gross,
        charges=charges,
        result=result,
        run_hash=_run_hash(
            router=name,
            router_spec_hash=spec_hash,
            state_result_hash=states.result_hash,
            strategy_hashes=strategy_hashes,
            decisions=decisions,
            request_hash=result.request_hash,
            gross_result_hash=gross.result_hash,
            charges=charges,
            result_hash=result.result_hash,
            validation_reports=reports,
            eligibility=router.eligibility,
        ),
        validation_reports=reports,
        eligibility=router.eligibility,
    )


def paper_run_or_stop(
    spec: RouterSpec,
    lifecycle: Mapping[Ref, LifecycleState],
    states: StateResult,
    strategies: Mapping[Ref, StrategyResult],
    *,
    bars: Sequence[PriceBar],
    cost_model: BacktestCostModel,
    initial_equity: Decimal,
    backtester: BacktestProvider,
    validation_reports: Mapping[Ref, str] | Mapping[str, str] | None = None,
    evidence: EligibilityEvidence | None = None,
) -> RouterPaperRun | RouterStop:
    """``paper_run`` of ``StrategyRouter(spec, lifecycle, evidence=evidence)``, or its stop.

    Only ``RouterStopped`` (including an evidence refusal, ``RouterEligibilityRefused``) becomes
    a record; any other ``RouterError`` (an unvalidated strategy, an ill-formed table or input)
    is raised as before. In evidence mode a supplied ``validation_reports`` must equal
    ``evidence.report_hashes``; the stop records the evidence hashes.
    """
    if isinstance(evidence, EligibilityEvidence):
        claimed = normalize_report_hashes(evidence.report_hashes)
        supplied = validation_reports
        if supplied is not None and normalize_report_hashes(supplied) != claimed:
            raise RouterError("validation_reports differ from evidence.report_hashes")
        validation_reports = claimed
    try:
        router = StrategyRouter(spec, lifecycle, evidence=evidence)
    except RouterStopped as stopped:
        return _stop_record(stopped, lifecycle, states, strategies, validation_reports)
    return paper_run(
        router,
        states,
        strategies,
        bars=bars,
        cost_model=cost_model,
        initial_equity=initial_equity,
        backtester=backtester,
        validation_reports=validation_reports,
    )


def _stop_record(
    stopped: RouterStopped,
    lifecycle: Mapping[Ref, LifecycleState],
    states: StateResult,
    strategies: Mapping[Ref, StrategyResult],
    validation_reports: Mapping[Ref, str] | Mapping[str, str] | None,
) -> RouterStop:
    if not isinstance(states, StateResult):
        raise RouterError("a router stop record needs a StateResult") from stopped
    for key, answer in strategies.items():
        if not isinstance(answer, StrategyResult):
            raise RouterError(f"{key}: expected a StrategyResult") from stopped
    reports = None if validation_reports is None else normalize_report_hashes(validation_reports)
    snapshot = {str(ref): LifecycleState(state).value for ref, state in lifecycle.items()}
    strategy_hashes = {str(ref): answer.result_hash for ref, answer in strategies.items()}
    record: dict[str, object] = {
        "kind": "router_stop",
        "router": stopped.router,
        "router_spec_hash": stopped.spec_hash,
        "reason": stopped.reason,
        "detail": stopped.detail,
        "lifecycle": snapshot,
        "state_result_hash": states.result_hash,
        "strategy_result_hashes": strategy_hashes,
        "validation_reports": reports,
    }
    if stopped.eligibility is not None:  # evidence mode only: trust-mode stop hashes unchanged
        record["eligibility"] = [check.to_dict() for check in stopped.eligibility]
    return RouterStop(
        router=stopped.router,
        router_spec_hash=stopped.spec_hash,
        reason=stopped.reason,
        detail=stopped.detail,
        lifecycle=dict(sorted(snapshot.items())),
        state_result_hash=states.result_hash,
        strategy_result_hashes=dict(sorted(strategy_hashes.items())),
        validation_reports=reports,
        stop_hash=content_hash(record),
        eligibility=stopped.eligibility,
    )
