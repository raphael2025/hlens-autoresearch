"""Phase 5 -> Phase 13 wiring: a ``StrategyProvider`` (+ optional ``RiskProvider``) as the
execution service's ``TargetPositionSource`` (ADR-0038 §Phase 5, ADR-0046; the wiring point named
in ``apps.execution.service.TargetPositionSource``).

``StrategyProviderTargetSource.target_positions(deployment_id, as_of)`` is the only way a Phase 5
strategy reaches the execution plane (``apps.execution.service.ExecutionService.run_once``):

1. pull the raw signal feed from the injected ``signals`` callable and keep only what a decision at
   ``as_of`` may honestly see — ``available_time <= as_of`` and ``knowledge_time <= as_of`` — before
   a ``StrategyRequest`` is ever built, so an over-eager source cannot leak the future in;
2. build a single-decision-time ``StrategyRequest`` (``knowledge_cutoff = as_of``,
   ``decision_times = (as_of,)``) and call the ``StrategyProvider``; the answer must pass
   ``StrategyResult.check_answers`` and must contain a position at ``as_of`` for every requested
   instrument, or the source refuses (``StrategySourceRefused``, fail closed — no position is ever
   filled in or assumed);
3. if a ``RiskProvider`` was supplied, constrain those positions with a ``RiskRequest`` built the
   same way (``ConstrainedPosition.as_target`` folds the risk-side answer back onto the strategy
   position); the risk answer must also pass ``RiskResult.check_answers``;
4. map each (possibly risk-constrained) ``target_weight`` to ``apps.execution.records.
   TargetPosition.quantity`` via the ``instruments`` map (signal-side instrument name ->
   ``core.domain.specs.Instrument``).

**Known v1 simplification** (framework batch; a debugging-pass follow-up, not a contract change):
step 4 maps the weight straight onto ``quantity`` — the execution plane's per-instrument
book-keeping (deltas, orders, fills, second-line risk) is exercised end-to-end with real numbers,
but real position sizing (``quantity = weight * equity / price``, as ``plugins.backtest.bar`` does
for the research backtester) needs equity and price knowledge this narrow seam does not have.

Only ``core.contracts.strategy`` / ``core.domain`` / ``apps.execution`` are imported — never
``research/`` or ``infrastructure/`` (``tests/test_architecture_boundaries.py``,
ADR-0046 red line).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal

from apps.execution.errors import ExecutionRefused
from apps.execution.records import TargetPosition as ExecutionTargetPosition
from apps.execution.records import TargetPositions
from core.contracts.strategy import (
    PortfolioState,
    RiskProvider,
    RiskRequest,
    SignalObservation,
    StrategyProvider,
    StrategyRequest,
)
from core.contracts.strategy import TargetPosition as SignalTargetPosition
from core.domain.base import FrozenMapping, Kind
from core.domain.specs import Instrument, RiskPolicy, StrategySpec

__all__ = ["StrategyProviderTargetSource", "StrategySourceRefused"]

#: ``(deployment_id, as_of) -> raw signal feed``; the source may over-return (e.g. every signal it
#: has ever seen) — the adapter keeps only what is honestly visible at ``as_of``.
SignalSource = Callable[[str, datetime], Iterable[SignalObservation]]
#: ``(deployment_id, as_of) -> PortfolioState`` for the risk step; ``as_of`` a state's ``as_of``
#: must never be later than.
PortfolioStateSource = Callable[[str, datetime], PortfolioState]


class StrategySourceRefused(ExecutionRefused):
    """The Phase 5 -> Phase 13 adapter refused to answer: fail closed, no position is assumed."""


def _default_portfolio_state(_deployment_id: str, as_of: datetime) -> PortfolioState:
    return PortfolioState(as_of=as_of, current_weights=FrozenMapping({}), equity=None)


class StrategyProviderTargetSource:
    """Wraps a ``StrategyProvider`` (+ optional ``RiskProvider``) as a ``TargetPositionSource``."""

    def __init__(
        self,
        *,
        strategy: StrategyProvider,
        strategy_spec: StrategySpec,
        instruments: Mapping[str, Instrument],
        signals: SignalSource,
        params: Mapping[str, Decimal | int | bool | str] | None = None,
        risk: RiskProvider | None = None,
        risk_policy: RiskPolicy | None = None,
        portfolio_state: PortfolioStateSource | None = None,
    ) -> None:
        if strategy_spec.ref.kind is not Kind.STRATEGY:
            raise ValueError(f"strategy_spec must be a strategy spec, got {strategy_spec.ref}")
        if not instruments:
            raise ValueError("instruments must not be empty")
        if (risk is None) != (risk_policy is None):
            raise ValueError("risk and risk_policy must be given together, or not at all")
        if risk_policy is not None and risk_policy.ref.kind is not Kind.RISK:
            raise ValueError(f"risk_policy must be a risk spec, got {risk_policy.ref}")
        self._strategy = strategy
        self._spec = strategy_spec
        self._instruments = dict(instruments)
        self._signals = signals
        self._params = dict(params or {})
        self._risk = risk
        self._risk_policy = risk_policy
        self._portfolio_state = portfolio_state or _default_portfolio_state

    def target_positions(self, deployment_id: str, as_of: datetime) -> TargetPositions:
        names = tuple(sorted(self._instruments))
        visible = self._visible_signals(deployment_id, as_of)

        request = StrategyRequest(
            strategy=self._spec.ref,
            spec_hash=self._spec.content_hash(),
            params=FrozenMapping(self._params),
            instruments=names,
            knowledge_cutoff=as_of,
            decision_times=(as_of,),
            signals=visible,
        )
        result = self._strategy.target_positions(request)
        try:
            result.check_answers(request, self._strategy.descriptor)
        except ValueError as exc:
            raise StrategySourceRefused(
                f"the strategy's answer did not pass check_answers: {exc}"
            ) from exc

        positions = result.at(as_of)
        if len(positions) != len(names):
            raise StrategySourceRefused(
                f"no strategy position at {as_of.isoformat()} for every requested instrument"
            )

        if self._risk is not None:
            positions = self._constrain(deployment_id, as_of, positions, visible)

        targets = tuple(
            ExecutionTargetPosition(
                instrument=self._instruments[item.instrument], quantity=item.target_weight
            )
            for item in positions
        )
        return TargetPositions(deployment_id=deployment_id, as_of=as_of, targets=targets)

    def _visible_signals(
        self, deployment_id: str, as_of: datetime
    ) -> tuple[SignalObservation, ...]:
        raw = tuple(self._signals(deployment_id, as_of))
        return tuple(
            item for item in raw if item.available_time <= as_of and item.knowledge_time <= as_of
        )

    def _constrain(
        self,
        deployment_id: str,
        as_of: datetime,
        positions: tuple[SignalTargetPosition, ...],
        visible: tuple[SignalObservation, ...],
    ) -> tuple[SignalTargetPosition, ...]:
        risk, risk_policy = self._risk, self._risk_policy
        if risk is None or risk_policy is None:
            raise RuntimeError("_constrain called without a configured RiskProvider")
        portfolio = self._portfolio_state(deployment_id, as_of)
        if portfolio.as_of > as_of:
            raise StrategySourceRefused("portfolio_state returned a state from after as_of")
        request = RiskRequest(
            policy=risk_policy.ref,
            policy_hash=risk_policy.content_hash(),
            decision_time=as_of,
            knowledge_cutoff=as_of,
            targets=positions,
            portfolio=portfolio,
            signals=visible,
        )
        result = risk.constrain(request)
        try:
            result.check_answers(request, risk.descriptor)
        except ValueError as exc:
            raise StrategySourceRefused(
                f"the risk provider's answer did not pass check_answers: {exc}"
            ) from exc
        by_instrument = {item.instrument: item for item in positions}
        return tuple(
            constrained.as_target(by_instrument[constrained.instrument])
            for constrained in result.positions
        )
