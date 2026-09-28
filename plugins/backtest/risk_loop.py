"""Risk in the simulation loop with the realized equity path (ADR-0088 decision 3). Simulation only.

``PortfolioState.peak_equity`` is the peak of the **realized** equity path up to ``as_of``; the
backtest / execution layer supplies it and a risk policy never keeps its own memory of peaks. This
module builds that state from what the simulation has realized so far:

- the realized equity path at ``as_of`` is ``initial_equity`` followed by every ``EquityPoint`` of
  the curve with ``time <= as_of`` (an equity point is the book marked at a bar group's close, so
  its ``time`` is when it became known). ``equity`` is the last value of that path and
  ``peak_equity`` its maximum — running, so it never decreases as ``as_of`` advances;
- nothing later than ``as_of`` is read: a target decided at ``t`` executes at a bar starting at or
  after ``t``, whose equity point ends after ``t``, so the points at or before ``t`` are final when
  the risk step at ``t`` runs;
- a realized equity ``<= 0`` cannot be a ``PortfolioState.equity`` (positive by contract); the run
  is refused (``BacktestInputError``) rather than handing the policy a made-up state.

``BarBacktester.run_with_risk`` uses ``RiskLoop`` to call a ``RiskProvider`` once per decision time,
at the moment the simulation admits that decision's targets. ``current_weights`` are the previous
constrained target weights (the convention of the research pipeline's risk step). The run's
``BacktestResult`` is built against the request holding the **constrained** targets
(``RiskLoopRun.request``), so it is identical to ``BarBacktester.run`` on that request and passes
``check_answers`` against it: the simulation semantics are unchanged, only the targets are
constrained with path information.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.contracts.strategy import (
    BacktestInputError,
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    PortfolioState,
    RiskProvider,
    RiskRequest,
    RiskResult,
    SignalObservation,
    TargetPosition,
)
from core.domain.base import FrozenMapping, Kind, Ref
from plugins.backtest.execution import ExecutionReport

__all__ = ["RealizedEquityPath", "RiskLoop", "RiskLoopRun", "realized_portfolio_state"]


def realized_portfolio_state(
    initial_equity: Decimal,
    curve: Sequence[EquityPoint],
    as_of: datetime,
    current_weights: FrozenMapping[str, Decimal] | None = None,
) -> PortfolioState:
    """The ``PortfolioState`` at ``as_of`` from the realized path (reference implementation).

    Reads only ``initial_equity`` and the curve points with ``time <= as_of``.
    """
    path = RealizedEquityPath(initial_equity)
    path.advance(curve, as_of)
    return path.state(as_of, current_weights or FrozenMapping({}))


class RealizedEquityPath:
    """Running last value and peak of ``initial_equity`` + the equity points seen so far.

    ``advance`` consumes the curve points with ``time <= as_of`` (``as_of`` never goes back); the
    curve may keep growing between calls (append-only), later points are left unread.
    """

    __slots__ = ("_as_of", "_equity", "_peak", "_seen")

    def __init__(self, initial_equity: Decimal) -> None:
        if not isinstance(initial_equity, Decimal) or not initial_equity > 0:
            raise BacktestInputError("initial_equity must be a positive Decimal")
        self._equity = initial_equity
        self._peak = initial_equity
        self._seen = 0
        self._as_of: datetime | None = None

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def peak_equity(self) -> Decimal:
        return self._peak

    def advance(self, curve: Sequence[EquityPoint], as_of: datetime) -> None:
        if self._as_of is not None and as_of < self._as_of:
            raise BacktestInputError("the realized equity path only moves forward in time")
        self._as_of = as_of
        while self._seen < len(curve) and curve[self._seen].time <= as_of:
            self._equity = curve[self._seen].equity
            if self._equity > self._peak:
                self._peak = self._equity
            self._seen += 1

    def state(
        self, as_of: datetime, current_weights: FrozenMapping[str, Decimal]
    ) -> PortfolioState:
        if self._as_of is None or as_of != self._as_of:
            raise BacktestInputError("advance the path to as_of before taking its state")
        if self._equity <= 0:
            raise BacktestInputError(
                f"realized equity {self._equity} at {as_of.isoformat()} is not positive; "
                "PortfolioState cannot carry it (fail closed)"
            )
        return PortfolioState(
            as_of=as_of,
            current_weights=current_weights,
            equity=self._equity,
            peak_equity=self._peak,
        )


@dataclass(frozen=True, slots=True)
class RiskLoopRun:
    """``BarBacktester.run_with_risk``: the constrained request, its result and the risk trail."""

    request: BacktestRequest
    result: BacktestResult
    report: ExecutionReport
    risk_results: tuple[RiskResult, ...]
    portfolio_states: tuple[PortfolioState, ...]


class RiskLoop:
    """One risk call per decision time, fed the realized path at that time (see module docs)."""

    def __init__(
        self,
        *,
        initial_equity: Decimal,
        risk: RiskProvider,
        policy: Ref,
        policy_hash: str,
        knowledge_cutoff: datetime,
        signals: Sequence[SignalObservation],
    ) -> None:
        if not isinstance(policy, Ref) or policy.kind is not Kind.RISK:
            raise BacktestInputError("policy must be a kind=risk reference")
        self._path = RealizedEquityPath(initial_equity)
        self._risk = risk
        self._policy = policy
        self._policy_hash = policy_hash
        self._cutoff = knowledge_cutoff
        self._signals = tuple(sorted(signals, key=lambda item: item.available_time))
        self._weights: dict[str, Decimal] = {}
        self._targets: list[TargetPosition] = []
        self._results: list[RiskResult] = []
        self._states: list[PortfolioState] = []

    def constrain(
        self,
        decision_time: datetime,
        upstream: Sequence[TargetPosition],
        curve: Sequence[EquityPoint],
    ) -> tuple[TargetPosition, ...]:
        """The constrained targets of one decision time (all of ``upstream`` share it)."""
        self._path.advance(curve, decision_time)
        state = self._path.state(decision_time, FrozenMapping(dict(self._weights)))
        request = RiskRequest(
            policy=self._policy,
            policy_hash=self._policy_hash,
            decision_time=decision_time,
            knowledge_cutoff=self._cutoff,
            targets=tuple(upstream),
            portfolio=state,
            signals=tuple(item for item in self._signals if item.available_time <= decision_time),
        )
        answer = self._risk.constrain(request)
        try:
            answer.check_answers(request, self._risk.descriptor)
        except ValueError as exc:
            raise BacktestInputError(f"the risk provider answered inconsistently: {exc}") from exc
        by_name = {item.instrument: item for item in upstream}
        constrained = tuple(
            position.as_target(by_name[position.instrument]) for position in answer.positions
        )
        for position in answer.positions:
            self._weights[position.instrument] = position.constrained_weight
        self._results.append(answer)
        self._states.append(state)
        self._targets.extend(constrained)
        return constrained

    def finish(self, remaining: Sequence[TargetPosition], curve: Sequence[EquityPoint]) -> None:
        """Constrain the targets decided after the last bar (never executed, still recorded)."""
        index = 0
        while index < len(remaining):
            decided = remaining[index].decision_time
            end = index
            while end < len(remaining) and remaining[end].decision_time == decided:
                end += 1
            self.constrain(decided, remaining[index:end], curve)
            index = end

    def request(self, raw: BacktestRequest) -> BacktestRequest:
        """``raw`` with its targets replaced by the constrained ones."""
        return BacktestRequest(
            cost_model=raw.cost_model,
            initial_equity=raw.initial_equity,
            bars=raw.bars,
            targets=tuple(self._targets),
        )

    @property
    def risk_results(self) -> tuple[RiskResult, ...]:
        return tuple(self._results)

    @property
    def portfolio_states(self) -> tuple[PortfolioState, ...]:
        return tuple(self._states)
