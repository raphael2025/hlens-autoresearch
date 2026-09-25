"""Time-series momentum on bars (Phase 5; ADR-0038). Research code — never production (H5).

Source: KnowledgeItem ``strategy_time_series_momentum@1.0.0`` (Moskowitz, Ooi & Pedersen 2012) and
``strategy_crypto_time_series_momentum@1.0.0``; both refs are the ``StrategySpec.lineage``. The
knowledge items are claims to test, not evidence (their status is ``unverified``).

Rule, per instrument and decision time ``t``: take the ``lookback`` latest visible
``bar_log_return`` observations (``available_time <= t``, by ``event_time``); their sum is the
trailing log return. Long when it is positive, short when negative (flat instead when
``long_only``), flat when zero. Each instrument gets an equal slice ``1 / len(instruments)`` of
gross exposure. Fewer than ``lookback`` values, or an explicit ``None`` inside the window, means
"not computable": the target is flat with ``inputs_used = 0`` (nothing is filled in).

The parameter space is declared in the spec (``param_search_space``); a request for any point
outside it is refused, so every trial is countable (Constitution C-T1). The declared values are
strategy parameters, not validation thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.strategy import (
    SignalObservation,
    StrategyInputError,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import StrategySpec
from research.strategies._params import resolve_params
from research.strategies.signals import LOG_RETURN_SIGNAL
from research.strategies.volatility_target import VOL_TARGET_POLICY_REF

__all__ = [
    "TSMOM_KNOWLEDGE",
    "TimeSeriesMomentumProvider",
    "tsmom_spec",
    "tsmom_vol_scaled_spec",
]

#: KnowledgeItem refs the strategy is drawn from (its lineage).
TSMOM_KNOWLEDGE: Final = (
    Ref(kind=Kind.KNOWLEDGE, name="strategy_time_series_momentum", version="1.0.0"),
    Ref(kind=Kind.KNOWLEDGE, name="strategy_crypto_time_series_momentum", version="1.0.0"),
)
#: Declared parameter space (lookback in bars of the signal's bar size).
TSMOM_PARAM_SPACE: Final[dict[str, tuple[str | int | float | bool, ...]]] = {
    "lookback": (60, 240, 1440),
    "long_only": (False, True),
}
_DEFAULTS: Final[dict[str, str | int | float | bool]] = {"lookback": 240, "long_only": False}
_SPEC_TIME: Final = datetime(2026, 9, 25, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_WEIGHT_QUANTUM: Final = Decimal("1e-18")


def _spec(name: str, risk_policy: Ref | None) -> StrategySpec:
    return StrategySpec(
        name=name,
        version="1.0.0",
        created_at=_SPEC_TIME,
        lineage=TSMOM_KNOWLEDGE,
        signals=(LOG_RETURN_SIGNAL,),
        params=FrozenMapping(_DEFAULTS),
        param_search_space=FrozenMapping(TSMOM_PARAM_SPACE),
        risk_policy=risk_policy,
    )


def tsmom_spec() -> StrategySpec:
    """``strategy:tsmom_bars@1.0.0`` — unconstrained time-series momentum."""
    return _spec("tsmom_bars", None)


def tsmom_vol_scaled_spec() -> StrategySpec:
    """``strategy:tsmom_bars_vol_scaled@1.0.0`` — the same signal under ``vol_target_bars``."""
    return _spec("tsmom_bars_vol_scaled", VOL_TARGET_POLICY_REF)


def _window(series: Sequence[SignalObservation], lookback: int) -> list[SignalObservation] | None:
    if len(series) < lookback:
        return None
    tail = list(series[len(series) - lookback :])
    if any(not isinstance(item.value, Decimal) for item in tail):
        if any(item.value is not None and not isinstance(item.value, Decimal) for item in tail):
            raise StrategyInputError("bar_log_return values must be Decimal or None")
        return None
    return tail


class TimeSeriesMomentumProvider:
    """``StrategyProvider`` for the TSMOM specs; deterministic, ``Decimal`` only."""

    def __init__(self, specs: Sequence[StrategySpec] | None = None) -> None:
        chosen = tuple(specs) if specs is not None else (tsmom_spec(), tsmom_vol_scaled_spec())
        self._specs = {str(spec.ref): spec for spec in chosen}
        self._descriptor = StrategyProviderDescriptor(
            name="research_tsmom",
            version="0.1.0",
            deterministic=True,
            supported_strategies=FrozenMapping(
                {key: spec.content_hash() for key, spec in self._specs.items()}
            ),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        if not isinstance(request, StrategyRequest):
            raise StrategyInputError("target_positions needs a StrategyRequest")
        if not self._descriptor.supports(request.strategy, request.spec_hash):
            raise UnsupportedStrategy(f"{request.strategy} with this spec hash is not supported")
        spec = self._specs[str(request.strategy)]
        params = resolve_params(spec, request.params)
        lookback, long_only = params["lookback"], params["long_only"]
        if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 1:
            raise UnsupportedStrategy("lookback must be a positive int")
        if not isinstance(long_only, bool):
            raise UnsupportedStrategy("long_only must be a bool")
        for item in request.signals:
            if item.signal != LOG_RETURN_SIGNAL:
                raise StrategyInputError(f"{spec.ref} does not consume signal {item.signal}")

        positions: list[TargetPosition] = []
        with localcontext(_CONTEXT):
            slice_weight = (Decimal(1) / len(request.instruments)).quantize(_WEIGHT_QUANTUM)
            for decision_time in request.decision_times:
                visible = request.visible_at(decision_time)
                for instrument in request.instruments:
                    series = sorted(
                        (item for item in visible if item.instrument == instrument),
                        key=lambda item: item.event_time,
                    )
                    positions.append(
                        self._position(
                            decision_time, instrument, series, lookback, long_only, slice_weight
                        )
                    )
        return StrategyResult.build(request, self._descriptor, positions)

    @staticmethod
    def _position(
        decision_time: datetime,
        instrument: str,
        series: Sequence[SignalObservation],
        lookback: int,
        long_only: bool,
        slice_weight: Decimal,
    ) -> TargetPosition:
        window = _window(series, lookback)
        if window is None:
            return TargetPosition(
                decision_time=decision_time,
                instrument=instrument,
                target_weight=Decimal(0),
                inputs_used=0,
            )
        trailing = sum(
            (item.value for item in window if isinstance(item.value, Decimal)), Decimal(0)
        )
        if trailing > 0:
            weight = slice_weight
        elif trailing < 0 and not long_only:
            weight = -slice_weight
        else:
            weight = Decimal(0)
        return TargetPosition(
            decision_time=decision_time,
            instrument=instrument,
            target_weight=weight,
            inputs_used=len(window),
            latest_input_available_time=max(item.available_time for item in window),
        )
