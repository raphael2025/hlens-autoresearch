"""Microstructure FeatureProviders over bar OHLCV and kline taker fields (ADR-0085 §特征).

Three deterministic providers, each a plugin behind the ``FeatureProvider`` Protocol (ADR-0030),
following the same shape as ``plugins/features/bars.py``: every parameter (window, output scale,
declared lag, input representation) is part of the ``FeatureSpec`` a provider serves and is bound by
the spec hash in the descriptor. **No parameter has a library default** (ADR-0085 §通用规则 2):
``window`` is always a required positional argument, never defaulted.

Input: one bar per observation, ``[event_time, event_end_time)``. Each provider only requires the
bar fields its formula uses (``volume`` + ``taker_buy_base_volume``; ``close`` + ``quote_volume``;
``high`` + ``low``) — all of them are columns ``infrastructure/feature/observations.py`` already
carries as observation values for every 1m kline bar (``BAR_VALUE_COLUMNS``), so no new input path
is needed. All bars of a request must be one symbol; overlapping bars are refused
(``FeatureInputError``).

At each evaluation time ``t`` a provider orders the visible bars by ``event_time`` and looks at the
trailing run: a window of ``k`` bars counts only when the ``k`` latest bars are contiguous
(``end == next start``). Otherwise the value is ``None``: nothing is filled or interpolated.

Logarithms and square roots (``FEA-CS-SPREAD-001`` needs both, plus ``exp``) run in the exact same
fixed 50-significant-digit ``Context`` that ``plugins/features/bars.py`` uses for its own ``ln`` /
``sqrt`` (``bars._LOG_CONTEXT``, imported here rather than redefined, so both files round the same
way). Exact sums (the volume totals in ``FEA-TAKER-FLOW-001``) run in a local ``_EXACT`` ``Context``
with the same parameters ``bars.py`` uses for ``bar_volume_sum`` (``prec=80`` with the same traps) —
mirrored rather than imported, since it is not one of the constants ``bars.py`` exports.

- ``TakerFlowImbalanceProvider`` (``taker_flow_<window>``, FEA-TAKER-FLOW-001): volume-weighted
  taker-buy share over the trailing ``window`` bars, ``2 * sum(taker_buy_base_volume) /
  sum(volume) - 1``; ``sum(volume) == 0`` is the one data-dependent division by zero in this module
  and is treated as missing (``None``), never as an arbitrary value;
- ``AmihudIlliquidityProvider`` (``amihud_illiq_<window>``, MSTX-AMIHUD-001): trailing mean of
  ``|r_i| / quote_volume_i`` over the ``window`` latest one-bar log returns (``window + 1``
  contiguous bars), ``quote_volume`` taken from the later bar of each return pair; any bar in the
  window with ``quote_volume == 0`` makes the whole windowed value ``None`` (a partial mean over
  fewer terms would silently redefine the window, which ADR-0085 §通用规则 4 does not allow);
- ``CorwinSchultzSpreadProvider`` (``cs_spread_<window>``, FEA-CS-SPREAD-001): the Corwin & Schultz
  (2012) two-bar high-low spread estimator, applied to each adjacent bar pair in the trailing
  ``window`` (= ``window + 1`` contiguous bars), each estimate truncated to ``>= 0`` (ADR-0085:
  "负值截断为 0") before the trailing mean over the ``window`` estimates. Given ``low > 0``
  (validated at parse time) the estimator's ``beta`` and ``gamma`` terms are non-negative sums/logs
  of ratios ``>= 1``, so no other operation in the formula needs guarding.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from itertools import pairwise
from typing import ClassVar, Final, cast

from core.contracts.feature import (
    FeatureInputError,
    FeatureObservation,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
    UnsupportedFeature,
)
from core.domain.base import FrozenMapping, Ref
from core.domain.specs import FeatureSpec
from plugins.features.bars import _LOG_CONTEXT, BAR_1M_INPUT, DEFAULT_SCALE

__all__ = [
    "AmihudIlliquidityProvider",
    "CorwinSchultzSpreadProvider",
    "TakerFlowImbalanceProvider",
]

#: decimal(38, 18)-scale exact sums: mirrors ``bars._EXACT`` (same rationale — an inexact step in a
#: volume sum is an error, never a silently rounded value).
_EXACT: Final = Context(prec=80, traps=[Inexact, InvalidOperation, Overflow, DivisionByZero])

with localcontext(_LOG_CONTEXT):
    _SQRT2: Final = Decimal(2).sqrt()
    _CS_DENOM: Final = 3 - 2 * _SQRT2


def _decimal(item: FeatureObservation, name: str) -> Decimal:
    value = item.values.get(name)
    if not isinstance(value, Decimal):
        raise FeatureInputError(f"{item.observation_key!r} has no Decimal {name!r} value")
    return value


def _positive_int(params: Mapping[str, object], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


def _single_input(spec: FeatureSpec) -> Ref:
    if len(spec.inputs) != 1 or not isinstance(spec.inputs[0], Ref):
        raise ValueError(f"{spec.ref} must have exactly one representation input")
    return spec.inputs[0]


def _build(
    name: str,
    definition: str,
    params: dict[str, str | int | float | bool],
    *,
    version: str,
    available_lag: timedelta,
    bar_input: Ref,
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        version=version,
        definition=definition,
        inputs=(bar_input,),
        params=FrozenMapping(params),
        available_lag=available_lag,
    )


def _quantum(spec: FeatureSpec) -> Decimal:
    return Decimal(1).scaleb(-_positive_int(spec.params, "scale"))


# ==========================================================================================
# bar shapes: each provider only parses the fields its formula needs
# ==========================================================================================


class _FlowBar:
    __slots__ = ("available_time", "end", "start", "taker", "volume")

    def __init__(self, item: FeatureObservation) -> None:
        if item.event_end_time is None:
            raise FeatureInputError(f"{item.observation_key!r} is not a bar (no event_end_time)")
        self.start: datetime = item.event_time
        self.end: datetime = item.event_end_time
        self.available_time: datetime = item.available_time
        self.volume = _decimal(item, "volume")
        self.taker = _decimal(item, "taker_buy_base_volume")
        if self.volume < 0:
            raise FeatureInputError(f"{item.observation_key!r} has a negative volume")
        if self.taker < 0 or self.taker > self.volume:
            raise FeatureInputError(
                f"{item.observation_key!r} has taker_buy_base_volume outside [0, volume]"
            )


class _LiquidityBar:
    __slots__ = ("available_time", "close", "end", "quote_volume", "start")

    def __init__(self, item: FeatureObservation) -> None:
        if item.event_end_time is None:
            raise FeatureInputError(f"{item.observation_key!r} is not a bar (no event_end_time)")
        self.start: datetime = item.event_time
        self.end: datetime = item.event_end_time
        self.available_time: datetime = item.available_time
        self.close = _decimal(item, "close")
        self.quote_volume = _decimal(item, "quote_volume")
        if self.close <= 0:
            raise FeatureInputError(f"{item.observation_key!r} has a non-positive close")
        if self.quote_volume < 0:
            raise FeatureInputError(f"{item.observation_key!r} has a negative quote_volume")


class _SpreadBar:
    __slots__ = ("available_time", "end", "high", "low", "start")

    def __init__(self, item: FeatureObservation) -> None:
        if item.event_end_time is None:
            raise FeatureInputError(f"{item.observation_key!r} is not a bar (no event_end_time)")
        self.start: datetime = item.event_time
        self.end: datetime = item.event_end_time
        self.available_time: datetime = item.available_time
        self.high = _decimal(item, "high")
        self.low = _decimal(item, "low")
        if self.low <= 0 or self.high <= 0:
            raise FeatureInputError(f"{item.observation_key!r} has a non-positive high/low")
        if self.high < self.low:
            raise FeatureInputError(f"{item.observation_key!r} has high < low")


type _MicroBar = _FlowBar | _LiquidityBar | _SpreadBar


def _bars(visible: Sequence[FeatureObservation], builder: type[_MicroBar]) -> list[_MicroBar]:
    symbols = {item.values.get("symbol") for item in visible}
    if len(symbols) > 1:
        raise FeatureInputError("a request must hold the bars of one symbol")
    bars = sorted((builder(item) for item in visible), key=lambda bar: bar.start)
    for earlier, later in pairwise(bars):
        if later.start < earlier.end:
            raise FeatureInputError(f"two bars overlap at {later.start.isoformat()}")
    return bars


def _trailing_run(bars: Sequence[_MicroBar], count: int) -> Sequence[_MicroBar] | None:
    """The ``count`` latest bars when they are contiguous; ``None`` otherwise."""
    if len(bars) < count:
        return None
    run = bars[len(bars) - count :]
    if any(earlier.end != later.start for earlier, later in pairwise(run)):
        return None
    return run


def _answer(at: datetime, value: Decimal | None, used: Sequence[_MicroBar]) -> FeatureValue:
    if value is None:
        return FeatureValue(evaluation_time=at, value=None, inputs_used=0)
    return FeatureValue(
        evaluation_time=at,
        value=value,
        inputs_used=len(used),
        latest_input_available_time=max(bar.available_time for bar in used),
    )


class _MicroFeatureProvider:
    """Shared plumbing: declared specs, descriptor, per-time visible bars (mirrors ``bars.py``)."""

    NAME: ClassVar[str]
    VERSION: ClassVar[str] = "1.0.0"
    BAR: ClassVar[type[_MicroBar]]

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        by_ref: dict[str, FeatureSpec] = {}
        for spec in specs:
            if not isinstance(spec, FeatureSpec):
                raise TypeError("specs must be FeatureSpec instances")
            try:
                canonical = self._canonical(spec)
            except ValueError as exc:
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec: {exc}") from None
            if canonical.content_hash() != spec.content_hash():
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec (definition or params)")
            if str(spec.ref) in by_ref:
                raise ValueError(f"{spec.ref} is declared twice")
            by_ref[str(spec.ref)] = spec
        if not by_ref:
            raise ValueError("a provider must serve at least one spec")
        self._specs = by_ref
        self._descriptor = ProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_features=FrozenMapping(
                {key: spec.content_hash() for key, spec in by_ref.items()}
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def compute(self, request: FeatureRequest) -> FeatureResult:
        spec = self._specs.get(str(request.feature))
        if spec is None or spec.content_hash() != request.spec_hash:
            raise UnsupportedFeature(f"{self.NAME} does not serve {request.feature} with this hash")
        values = []
        for at in request.evaluation_times:
            bars = _bars(request.visible_at(at, spec.available_lag), self.BAR)
            values.append(self._value(spec, at, bars))
        return FeatureResult.build(request, self._descriptor, values)

    # ------------------------------------------------------------------ per provider

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        raise NotImplementedError

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_MicroBar]) -> FeatureValue:
        raise NotImplementedError


class TakerFlowImbalanceProvider(_MicroFeatureProvider):
    """Volume-weighted taker-buy imbalance: ``2 * sum(taker) / sum(volume) - 1``."""

    NAME = "taker_flow"
    BAR = _FlowBar

    @staticmethod
    def spec(
        window: int,
        *,
        scale: int = DEFAULT_SCALE,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        return _build(
            f"taker_flow_{window}",
            "2 * sum(taker_buy_base_volume) / sum(volume) - 1 over the `window` latest visible "
            "bars when contiguous; sum(volume) == 0 is None (division by zero, not filled); None "
            "otherwise when not enough contiguous history. Exact sums, then a 50-digit division "
            "quantized to `scale` places, half-even.",
            {"window": window, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            scale=_positive_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_MicroBar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        flow_run = cast(Sequence[_FlowBar], run)
        try:
            with localcontext(_EXACT):
                total_volume = sum((bar.volume for bar in flow_run), Decimal(0))
                total_taker = sum((bar.taker for bar in flow_run), Decimal(0))
        except DecimalException:
            raise FeatureInputError("the taker flow sum is not exact") from None
        if total_volume == 0:
            return _answer(at, None, ())
        with localcontext(_LOG_CONTEXT):
            value = (2 * total_taker / total_volume - 1).quantize(quantum)
        return _answer(at, value, run)


class AmihudIlliquidityProvider(_MicroFeatureProvider):
    """Trailing mean of ``|r_i| / quote_volume_i`` over ``window`` one-bar log returns."""

    NAME = "amihud_illiq"
    BAR = _LiquidityBar

    @staticmethod
    def spec(
        window: int,
        *,
        scale: int = DEFAULT_SCALE,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        return _build(
            f"amihud_illiq_{window}",
            "mean over the `window` latest one-bar log returns r_i = ln(close[i]/close[i-1]) of "
            "|r_i| / quote_volume[i] (quote_volume of the later bar of each pair), from the "
            "`window` + 1 latest visible bars when contiguous; None if not enough contiguous "
            "history or any quote_volume in the window is 0 (division by zero, not filled). "
            "50-digit ln and division, quantized to `scale` places, half-even.",
            {"window": window, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            scale=_positive_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_MicroBar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window + 1)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        liquidity_run = cast(Sequence[_LiquidityBar], run)
        with localcontext(_LOG_CONTEXT):
            terms = []
            for earlier, later in pairwise(liquidity_run):
                if later.quote_volume == 0:
                    return _answer(at, None, ())
                r = (later.close / earlier.close).ln()
                terms.append(abs(r) / later.quote_volume)
            value = (sum(terms, Decimal(0)) / window).quantize(quantum)
        return _answer(at, value, run)


class CorwinSchultzSpreadProvider(_MicroFeatureProvider):
    """Trailing mean of the Corwin-Schultz two-bar spread estimate, each truncated to ``>= 0``."""

    NAME = "cs_spread"
    BAR = _SpreadBar

    @staticmethod
    def spec(
        window: int,
        *,
        scale: int = DEFAULT_SCALE,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        return _build(
            f"cs_spread_{window}",
            "mean over the `window` latest adjacent-bar-pair Corwin-Schultz (2012) spread "
            "estimates (each truncated to >= 0), from the `window` + 1 latest visible bars when "
            "contiguous; None otherwise. 50-digit ln, sqrt and exp, quantized to `scale` places, "
            "half-even.",
            {"window": window, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            scale=_positive_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_MicroBar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window + 1)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        spread_run = cast(Sequence[_SpreadBar], run)
        with localcontext(_LOG_CONTEXT):
            estimates = []
            for earlier, later in pairwise(spread_run):
                hl_later = (later.high / later.low).ln()
                hl_earlier = (earlier.high / earlier.low).ln()
                beta = hl_later**2 + hl_earlier**2
                hi = max(later.high, earlier.high)
                lo = min(later.low, earlier.low)
                gamma = (hi / lo).ln() ** 2
                alpha = ((2 * beta).sqrt() - beta.sqrt()) / _CS_DENOM - (gamma / _CS_DENOM).sqrt()
                e_alpha = alpha.exp()
                spread = 2 * (e_alpha - 1) / (1 + e_alpha)
                estimates.append(spread if spread > 0 else Decimal(0))
            value = (sum(estimates, Decimal(0)) / window).quantize(quantum)
        return _answer(at, value, run)
