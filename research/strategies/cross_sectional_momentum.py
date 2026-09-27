"""Cross-sectional momentum on bars (Phase 5; ADR-0038). Research code — never production (H5).

Source: KnowledgeItem ``factor_crypto_market_size_momentum@1.0.0`` (Liu, Tsyvinski & Wu 2022:
a crypto market / size / momentum factor model); that ref is the ``StrategySpec.lineage``. The
knowledge item is a claim to test, not evidence (its status is ``unverified``). Only the momentum
leg is implemented here; market and size need data (market caps) the approved scope does not have.

Rule, at decision time ``t`` over the request's instruments: each instrument's trailing log return
is the sum of its ``lookback`` latest visible ``bar_log_return`` observations (``available_time <=
t``, by ``event_time``) — a monotone transform of the simple trailing return, so the ranking is the
same. An instrument with fewer than ``lookback`` values, or an explicit ``None`` inside its window,
is "not computable": it is not ranked and its target is flat with ``inputs_used = 0`` (nothing is
filled in). The computable instruments are ranked by trailing return, descending; ties are broken
by instrument name (ascending), so the ranking is deterministic. With ``k = min(top_n, m // 2)``
(``m`` = computable instruments):

- long the ``k`` highest, short the ``k`` lowest, each at ``gross_exposure / (2k)``; or, when
  ``long_only``, long the ``k`` highest at ``gross_exposure / k`` and nothing short;
- everything else flat. ``k = 0`` (fewer than two computable instruments: no cross-section) or no
  dispersion (every trailing return equal) means every target is flat.

Absolute weights sum to ``gross_exposure`` (each slice is rounded *down* to 18 places, so the sum
never exceeds it). A ranked instrument's position depends on every ranked window, so its
``inputs_used`` / ``latest_input_available_time`` cover all of them.

The parameter space is declared in the spec (``param_search_space``); a request for any point
outside it is refused, so every trial is countable (Constitution C-T1). The declared values are
strategy parameters (``lookback`` in bars of the signal's bar size), not validation thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext
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

__all__ = [
    "XSMOM_KNOWLEDGE",
    "XSMOM_NAME",
    "XSMOM_PARAM_SPACE",
    "CrossSectionalMomentumProvider",
    "xsmom_spec",
]

#: The strategy's name (``StrategySpec.name``); ``research.strategies.cross_section`` declares
#: it cross-sectional (ADR-0059).
XSMOM_NAME: Final = "xsmom_bars"
#: KnowledgeItem refs the strategy is drawn from (its lineage).
XSMOM_KNOWLEDGE: Final = (
    Ref(kind=Kind.KNOWLEDGE, name="factor_crypto_market_size_momentum", version="1.0.0"),
)
#: Declared parameter space. ``gross_exposure`` is an int: a spec holds no Decimal and a request
#: refuses numeric text, so an int is the only exact numeric type both sides accept.
XSMOM_PARAM_SPACE: Final[dict[str, tuple[str | int | float | bool, ...]]] = {
    "lookback": (60, 240, 1440),
    "long_only": (False, True),
    "top_n": (1, 2),
    "gross_exposure": (1,),
}
_DEFAULTS: Final[dict[str, str | int | float | bool]] = {
    "lookback": 240,
    "long_only": False,
    "top_n": 1,
    "gross_exposure": 1,
}
_SPEC_TIME: Final = datetime(2026, 9, 26, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_WEIGHT_QUANTUM: Final = Decimal("1e-18")


def xsmom_spec() -> StrategySpec:
    """``strategy:xsmom_bars@1.0.0`` — cross-sectional momentum over the requested instruments."""
    return StrategySpec(
        name=XSMOM_NAME,
        version="1.0.0",
        created_at=_SPEC_TIME,
        lineage=XSMOM_KNOWLEDGE,
        signals=(LOG_RETURN_SIGNAL,),
        params=FrozenMapping(_DEFAULTS),
        param_search_space=FrozenMapping(XSMOM_PARAM_SPACE),
    )


def _positive_int(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise UnsupportedStrategy(f"{key} must be a positive int")
    return value


def _window(series: Sequence[SignalObservation], lookback: int) -> list[SignalObservation] | None:
    if len(series) < lookback:
        return None
    tail = list(series[len(series) - lookback :])
    if any(not isinstance(item.value, Decimal) for item in tail):
        if any(item.value is not None and not isinstance(item.value, Decimal) for item in tail):
            raise StrategyInputError("bar_log_return values must be Decimal or None")
        return None
    return tail


class CrossSectionalMomentumProvider:
    """``StrategyProvider`` for ``xsmom_bars``; deterministic, ``Decimal`` only."""

    def __init__(self, specs: Sequence[StrategySpec] | None = None) -> None:
        chosen = tuple(specs) if specs is not None else (xsmom_spec(),)
        self._specs = {str(spec.ref): spec for spec in chosen}
        self._descriptor = StrategyProviderDescriptor(
            name="research_xsmom",
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
        lookback = _positive_int(params["lookback"], "lookback")
        top_n = _positive_int(params["top_n"], "top_n")
        long_only = params["long_only"]
        if not isinstance(long_only, bool):
            raise UnsupportedStrategy("long_only must be a bool")
        gross = Decimal(_positive_int(params["gross_exposure"], "gross_exposure"))
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
                weights = _weights(windows, top_n, long_only, gross)
                used = sum(len(window) for window in windows.values())
                latest = max(
                    (item.available_time for window in windows.values() for item in window),
                    default=None,
                )
                for instrument in request.instruments:
                    ranked = instrument in windows
                    positions.append(
                        TargetPosition(
                            decision_time=decision_time,
                            instrument=instrument,
                            target_weight=weights.get(instrument, Decimal(0)),
                            inputs_used=used if ranked else 0,
                            latest_input_available_time=latest if ranked else None,
                        )
                    )
        return StrategyResult.build(request, self._descriptor, positions)


def _weights(
    windows: dict[str, list[SignalObservation]], top_n: int, long_only: bool, gross: Decimal
) -> dict[str, Decimal]:
    """Target weights of the ranked instruments (absent = flat); see the module docs."""
    trailing = {
        name: sum((item.value for item in window if isinstance(item.value, Decimal)), Decimal(0))
        for name, window in windows.items()
    }
    k = min(top_n, len(trailing) // 2)
    if k == 0 or len(set(trailing.values())) == 1:
        return {}
    ranking = sorted(trailing, key=lambda name: (-trailing[name], name))
    if long_only:
        slice_weight = (gross / k).quantize(_WEIGHT_QUANTUM, rounding=ROUND_DOWN)
        return dict.fromkeys(ranking[:k], slice_weight)
    slice_weight = (gross / (2 * k)).quantize(_WEIGHT_QUANTUM, rounding=ROUND_DOWN)
    weights = dict.fromkeys(ranking[:k], slice_weight)
    weights.update(dict.fromkeys(ranking[-k:], -slice_weight))
    return weights
