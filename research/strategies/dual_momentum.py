"""Dual momentum (relative + absolute), spot long / flat (ADR-0085 ``STR-DUAL-MOM-001``).

Research code — never production (H5). Source: the strategy library entry ``STR-DUAL-MOM-001``
(Antonacci's dual momentum: pick the asset with the best trailing return — relative momentum — and
hold it only while its own trailing return is positive — absolute momentum). The spec's ``lineage``
cites the project knowledge items the rule combines:
``factor_crypto_market_size_momentum@1.0.0`` (relative, cross-sectional momentum) and
``strategy_time_series_momentum@1.0.0`` / ``strategy_crypto_time_series_momentum@1.0.0`` (absolute,
time-series momentum). They are claims to
test, not evidence.

ADR-0085 defines the universe as BTCUSDT / ETHUSDT; the provider ranks exactly the request's
instruments (the caller passes that pair) and hard-codes no instrument name. The strategy is
cross-sectional in the sense of ADR-0059: it ranks instruments against each other.

Rule, at decision time ``t`` over the request's instruments: each instrument's trailing log return
is the sum of its ``lookback`` latest visible ``bar_log_return`` observations (``available_time <=
t``, by ``event_time``; as ``tsmom_bars`` / ``xsmom_bars``). Then:

- if any instrument is not computable (fewer than ``lookback`` values, or an explicit ``None`` in
  its window), the best asset is unknown: every target is flat with ``inputs_used = 0`` (nothing
  is filled in, no partial ranking);
- if the highest trailing return is shared by more than one instrument, there is no relative
  signal: every target is flat (no name-based tie break);
- if the single best instrument's trailing return is ``<= 0`` (absolute momentum), every target is
  flat;
- otherwise the best instrument is held at weight ``1`` (the whole gross exposure) and every other
  instrument is flat.

When every instrument is computable, every position depends on every window, so each target's
``inputs_used`` / ``latest_input_available_time`` cover all of them. Spot long / flat only: no
short target (short cost, gap ST-4, is undefined).

The parameter space is declared here (``DUAL_MOMENTUM_PARAM_SPACE``); a spec is built only at an
explicit declared point (ADR-0085 rule 2: no default) and a request outside the space is refused
(``UnsupportedStrategy``, C-T1). The declared values are strategy parameters in bars of the signal's
bar size, not validation thresholds.
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
from research.strategies._declared import build_spec, check_spec, positive_int
from research.strategies._params import resolve_params
from research.strategies.signals import LOG_RETURN_SIGNAL

__all__ = [
    "DUAL_MOMENTUM_KNOWLEDGE",
    "DUAL_MOMENTUM_NAME",
    "DUAL_MOMENTUM_PARAM_SPACE",
    "DUAL_MOMENTUM_SIGNALS",
    "DualMomentumProvider",
    "dual_momentum_spec",
]

#: The strategy's name (``StrategySpec.name``); to be declared cross-sectional in
#: ``research.strategies.cross_section`` (ADR-0059).
DUAL_MOMENTUM_NAME: Final = "dual_momentum"
DUAL_MOMENTUM_KNOWLEDGE: Final = (
    Ref(kind=Kind.KNOWLEDGE, name="factor_crypto_market_size_momentum", version="1.0.0"),
    Ref(kind=Kind.KNOWLEDGE, name="strategy_time_series_momentum", version="1.0.0"),
    Ref(kind=Kind.KNOWLEDGE, name="strategy_crypto_time_series_momentum", version="1.0.0"),
)
DUAL_MOMENTUM_SIGNALS: Final = (LOG_RETURN_SIGNAL,)
#: Declared parameter space: the lookback in bars, the same grid as ``tsmom_bars`` /
#: ``xsmom_bars`` (1 h, 4 h, 1 d of 1m bars), so the momentum families are comparable. Three points.
DUAL_MOMENTUM_PARAM_SPACE: Final[dict[str, tuple[str | int | float | bool, ...]]] = {
    "lookback": (60, 240, 1440),
}
_SPEC_TIME: Final = datetime(2026, 9, 28, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


def dual_momentum_spec(*, lookback: int) -> StrategySpec:
    """``strategy:dual_momentum@1.0.0`` at an explicit, declared parameter point."""
    return build_spec(
        name=DUAL_MOMENTUM_NAME,
        created_at=_SPEC_TIME,
        lineage=DUAL_MOMENTUM_KNOWLEDGE,
        signals=DUAL_MOMENTUM_SIGNALS,
        space=DUAL_MOMENTUM_PARAM_SPACE,
        point={"lookback": lookback},
    )


def _window(series: Sequence[SignalObservation], lookback: int) -> list[SignalObservation] | None:
    if len(series) < lookback:
        return None
    tail = list(series[len(series) - lookback :])
    if any(item.value is not None and not isinstance(item.value, Decimal) for item in tail):
        raise StrategyInputError("bar_log_return values must be Decimal or None")
    if any(item.value is None for item in tail):
        return None
    return tail


def _best(windows: dict[str, list[SignalObservation]]) -> str | None:
    """The single instrument held (see module docs), or ``None`` = everything flat."""
    trailing = {
        name: sum((item.value for item in window if isinstance(item.value, Decimal)), Decimal(0))
        for name, window in windows.items()
    }
    top = max(trailing.values())
    leaders = [name for name, value in trailing.items() if value == top]
    if len(leaders) != 1 or top <= 0:
        return None
    return leaders[0]


class DualMomentumProvider:
    """``StrategyProvider`` for ``dual_momentum@1.0.0``; deterministic, ``Decimal`` only."""

    def __init__(self, specs: Sequence[StrategySpec]) -> None:
        chosen = tuple(specs)
        if not chosen:
            raise ValueError("at least one explicit dual_momentum spec is required")
        for spec in chosen:
            check_spec(
                spec,
                name=DUAL_MOMENTUM_NAME,
                signals=DUAL_MOMENTUM_SIGNALS,
                space=DUAL_MOMENTUM_PARAM_SPACE,
            )
        self._specs = {str(spec.ref): spec for spec in chosen}
        self._descriptor = StrategyProviderDescriptor(
            name="research_dual_momentum",
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
        lookback = positive_int(params["lookback"], "lookback")
        for item in request.signals:
            if item.signal != LOG_RETURN_SIGNAL:
                raise StrategyInputError(f"{spec.ref} does not consume signal {item.signal}")

        positions: list[TargetPosition] = []
        with localcontext(_CONTEXT):
            for decision_time in request.decision_times:
                visible = request.visible_at(decision_time)
                windows: dict[str, list[SignalObservation]] = {}
                for instrument in request.instruments:
                    series = sorted(
                        (item for item in visible if item.instrument == instrument),
                        key=lambda item: item.event_time,
                    )
                    window = _window(series, lookback)
                    if window is not None:
                        windows[instrument] = window
                complete = len(windows) == len(request.instruments)
                held = _best(windows) if complete else None
                used = sum(len(window) for window in windows.values()) if complete else 0
                latest = (
                    max(item.available_time for window in windows.values() for item in window)
                    if complete
                    else None
                )
                for instrument in request.instruments:
                    positions.append(
                        TargetPosition(
                            decision_time=decision_time,
                            instrument=instrument,
                            target_weight=Decimal(1) if instrument == held else Decimal(0),
                            inputs_used=used,
                            latest_input_available_time=latest,
                        )
                    )
        return StrategyResult.build(request, self._descriptor, positions)
