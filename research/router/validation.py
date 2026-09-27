"""Router self-validation (roadmap Phase 10, ADR-0043): the router as a validated strategy object.

``router.py`` says the router "is a strategy object that must pass full validation before
promotion"; ``paper.py`` gives it its own net result (``ROUTER_PAPER_BACKTEST``). This module is the
missing adapter: a ``research.strategies.validation.TrialRunner`` that re-runs the router's paper
run over declared inputs, so ``PipelineBacktestValidator`` validates the router's **net** paper
result (after instrument costs and switching costs) exactly as it validates a strategy's backtest.
Research code, paper / simulation only (H5); nothing here places an order. No core / contract /
Schema change: the router is presented to the validator as an ordinary ``StrategySpec``.

- ``router_strategy_spec(spec, state=...)``: the ``StrategySpec`` of the router — same name and
  version, its state as the only signal, ``params = {"router_spec_hash": RouterSpec.spec_hash()}``
  (so the spec's content hash, which a ``ValidationContext`` binds through the reproduction
  tuple, changes whenever the routing table, fallback or switching rate does) and an **empty**
  ``param_search_space``: a router spec has nothing to search, so its family has exactly
  ``ROUTER_TRIALS_PER_SPEC`` = 1 trial (C-T1 counting as for strategies: one trial per spec; a
  different table / rate is a different spec, hence another trial of the caller's family).
- ``RouterTrialRunner``: the ``TrialRunner``. The plain call (no delay, no offset, all
  instruments) **is** ``paper_run`` over the declared inputs — its ``backtest`` is
  ``RouterPaperRun.result``, so ``G0.reproducibility`` compares the recorded router result with a
  fresh ``paper_run``. Stress calls (G4) re-run the same steps (route -> ``combine_targets`` ->
  backtester -> the same switching-cost accounting as ``paper_run``):

  * ``decision_offset``: every routing decision is taken ``offset`` later, from the state known
    at the shifted time (as-of the state values; before the first value the state is unknown ->
    the fallback), with strategy positions as-of the shifted time (``combine_targets``);
  * ``delay_bars``: every combined target (and its switching charge) is executed ``delay_bars``
    bars later — same information, later fill; a target with no execution bar left is dropped
    (as ``research.strategies.pipeline``'s delay stress);
  * ``instruments``: only those instruments' combined targets and bars (the switching charge is
    the router's allocation churn and is kept whole).

  Any non-empty ``params`` is refused: a router has no search parameter.
- ``validate_router(validator, strategy_spec, run)``: checks that ``strategy_spec`` is the
  router's (``router_spec_hash`` equal to the run's), ``run.verify()``, then validates
  ``run.result``; ``RouterValidation.binding_hash`` binds report hash, router spec hash, router
  strategy spec hash and ``run_hash``.

Nothing here judges a verdict or holds a threshold; the Profile does.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from core.contracts.feature import ObservationScalar
from core.contracts.state import StateResult
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestRequest,
    BacktestResult,
    PriceBar,
    StrategyResult,
    execution_bar,
)
from core.domain.base import Kind, Ref, content_hash
from core.domain.specs import StrategySpec
from research.router.paper import (
    ROUTER_PAPER_BACKTEST,
    RouterPaperRun,
    # paper_run's own helpers, reused so stress runs account exactly as paper_run does
    _net_curve,
    _positions_by_strategy,
    combine_targets,
    paper_run,
)
from research.router.router import RouterError, RouterSpec, RoutingDecision, StrategyRouter
from research.strategies.validation import BacktestValidation, BacktestValidator, TrialRun

__all__ = [
    "ROUTER_SPEC_HASH_PARAM",
    "ROUTER_TRIALS_PER_SPEC",
    "RouterTrialRunner",
    "RouterValidation",
    "router_strategy_spec",
    "validate_router",
]

#: ``StrategySpec.params`` key carrying ``RouterSpec.spec_hash()``.
ROUTER_SPEC_HASH_PARAM: Final = "router_spec_hash"
#: Trials one router spec adds to its family (empty search space: one point).
ROUTER_TRIALS_PER_SPEC: Final = 1


def router_strategy_spec(
    spec: RouterSpec, *, state: Ref, created_at: datetime | None = None
) -> StrategySpec:
    """The router as a ``StrategySpec`` (module docs). ``state`` is the routed state's ref."""
    if state.kind is not Kind.STATE:
        raise RouterError(f"a router's signal is its state, not {state}")
    values: dict[str, object] = {
        "name": spec.name,
        "version": spec.version,
        "signals": (state,),
        "params": {ROUTER_SPEC_HASH_PARAM: spec.spec_hash()},
        "param_search_space": {},
    }
    if created_at is not None:
        values["created_at"] = created_at
    return StrategySpec.model_validate(values)


@dataclass(frozen=True, slots=True)
class RouterTrialRunner:
    """``TrialRunner`` of a router over declared inputs (module docs)."""

    router: StrategyRouter
    states: StateResult
    strategies: Mapping[Ref, StrategyResult]
    #: Sorted by ``(interval_start, instrument)``, as a ``BacktestRequest`` requires.
    bars: tuple[PriceBar, ...]
    cost_model: BacktestCostModel
    initial_equity: Decimal
    backtester: BacktestProvider
    validation_reports: Mapping[Ref, str] | Mapping[str, str] | None = None

    @property
    def router_spec_hash(self) -> str:
        return self.router.spec.spec_hash()

    def paper(self) -> RouterPaperRun:
        """The router's paper run over the declared inputs (exactly ``paper_run``)."""
        return paper_run(
            self.router,
            self.states,
            self.strategies,
            bars=self.bars,
            cost_model=self.cost_model,
            initial_equity=self.initial_equity,
            backtester=self.backtester,
            validation_reports=self.validation_reports,
        )

    def run(
        self,
        params: Mapping[str, ObservationScalar],
        *,
        delay_bars: int = 0,
        decision_offset: timedelta = timedelta(0),
        instruments: tuple[str, ...] | None = None,
    ) -> TrialRun:
        if params:
            raise ValueError(
                f"a router has no search parameters (one trial per router spec), got {dict(params)}"
            )
        if delay_bars < 0:
            raise ValueError("delay_bars must be >= 0")
        if not delay_bars and not decision_offset and instruments is None:
            base = self.paper()
            return TrialRun(
                targets=base.targets,
                backtest=base.result,
                bars=self.bars,
                cost_model=self.cost_model,
            )
        return self._stressed(delay_bars, decision_offset, instruments)

    # ----------------------------------------------------------------------------------

    def _decisions(self, offset: timedelta) -> tuple[RoutingDecision, ...]:
        values = sorted(self.states.values, key=lambda value: value.evaluation_time)
        times = [value.evaluation_time for value in values]
        points: list[tuple[datetime, str | None]] = []
        for value in values:
            at = value.evaluation_time + offset
            index = bisect_right(times, at) - 1  # the state known at the shifted time
            points.append((at, values[index].state if index >= 0 else None))
        return self.router.route(points)

    def _stressed(
        self, delay_bars: int, offset: timedelta, instruments: tuple[str, ...] | None
    ) -> TrialRun:
        positions = _positions_by_strategy(self.router, self.strategies)
        decisions = self._decisions(offset)
        targets = combine_targets(decisions, positions)
        bars = self.bars
        if instruments is not None:
            keep = set(instruments)
            known = {bar.instrument for bar in bars}
            if not keep <= known:
                raise ValueError(f"unknown instruments {sorted(keep - known)}")
            bars = tuple(bar for bar in bars if bar.instrument in keep)
            targets = tuple(t for t in targets if t.instrument in keep)
        if delay_bars:
            step = _bar_length(bars) * delay_bars
            targets = tuple(
                t.model_validate({**t.model_dump(), "decision_time": t.decision_time + step})
                for t in targets
                if execution_bar(bars, t.instrument, t.decision_time + step) is not None
            )
            decisions = tuple(replace(d, at=d.at + step) for d in decisions)
        request = BacktestRequest(
            cost_model=self.cost_model,
            initial_equity=self.initial_equity,
            bars=bars,
            targets=targets,
        )
        gross = self.backtester.run(request)
        gross.check_answers(request, self.backtester.descriptor)
        curve, _ = _net_curve(decisions, gross, self.router.spec.switching_cost_rate)
        result = BacktestResult.build(
            request,
            ROUTER_PAPER_BACKTEST,
            fills=gross.fills,
            equity_curve=curve,
            unexecuted_targets=gross.unexecuted_targets,
        )
        result.check_answers(request, ROUTER_PAPER_BACKTEST)
        return TrialRun(targets=targets, backtest=result, bars=bars, cost_model=self.cost_model)


def _bar_length(bars: Sequence[PriceBar]) -> timedelta:
    lengths = {bar.interval_end - bar.interval_start for bar in bars}
    if len(lengths) != 1:
        raise ValueError("delay stress needs one uniform bar length")
    return lengths.pop()


@dataclass(frozen=True, slots=True)
class RouterValidation:
    """A router's validation, bound to the router spec and its paper run."""

    validation: BacktestValidation
    router_spec_hash: str
    #: ``StrategySpec.content_hash()`` of ``router_strategy_spec`` (the report's subject).
    router_strategy_spec_hash: str
    paper_run_hash: str
    binding_hash: str


def _binding_hash(report_hash: str, spec_hash: str, strategy_hash: str, run_hash: str) -> str:
    return content_hash(
        {
            "kind": "router_validation",
            "report_hash": report_hash,
            "router_spec_hash": spec_hash,
            "router_strategy_spec_hash": strategy_hash,
            "paper_run_hash": run_hash,
        }
    )


def validate_router(
    validator: BacktestValidator, strategy_spec: StrategySpec, run: RouterPaperRun
) -> RouterValidation:
    """Validate a recorded router paper run through ``validator`` (module docs).

    The validator's ``ValidatorSetup.trials`` should be the ``RouterTrialRunner`` of the same
    router and inputs; if it re-runs something else, ``G0.reproducibility`` fails.
    """
    run.verify()
    claimed = strategy_spec.params.get(ROUTER_SPEC_HASH_PARAM)
    if claimed != run.router_spec_hash:
        raise RouterError("the strategy spec is not this router's (router_spec_hash differs)")
    if f"{strategy_spec.name}@{strategy_spec.version}" != run.router:
        raise RouterError(f"the strategy spec {strategy_spec.ref} is not router {run.router}")
    if strategy_spec.param_search_space:
        raise RouterError("a router strategy spec has no search space (one trial per spec)")
    validation = validator.validate(strategy_spec.ref, strategy_spec, run.result)
    strategy_hash = strategy_spec.content_hash()
    return RouterValidation(
        validation=validation,
        router_spec_hash=run.router_spec_hash,
        router_strategy_spec_hash=strategy_hash,
        paper_run_hash=run.run_hash,
        binding_hash=_binding_hash(
            validation.report.content_hash(), run.router_spec_hash, strategy_hash, run.run_hash
        ),
    )
