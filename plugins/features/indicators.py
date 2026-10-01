"""Classic technical indicators as FeatureProviders (ADR-0085 batch; ADR-0030 contract).

Eleven deterministic providers over the same bar observations as ``plugins/features/bars.py``
(1m Canonical bars or complete derived bars), each a plugin behind the ``FeatureProvider``
Protocol. Every parameter — window, period, scale, multiplier — is part of the ``FeatureSpec`` a
provider serves and is bound by the spec hash; **none of them has a default value** (ADR-0085
§"通用规则" #2): a spec missing a parameter is refused at construction, never silently filled.

Input: one bar per observation with ``open`` / ``high`` / ``low`` / ``close`` / ``volume`` values
(same PIT-selected ``canonical.bars_1m`` rows ``bars.py`` reads). All bars of a request must be one
symbol; overlapping bars are refused (``FeatureInputError``), exactly as in ``bars.py``.

Two lookback shapes are used, matching each indicator's own definition:

- **Fixed trailing window** (Bollinger %b / bandwidth, VWAP): only the ``window`` latest visible
  bars are used, and only when they are mutually contiguous (``end == next start``); otherwise the
  value is ``None``.
- **Growing trailing run** (ATR, RSI, MACD line / signal, ADX): Wilder-style smoothing has no fixed
  lookback of its own — each new bar updates a running average seeded once enough bars are seen, and
  every bar since the seed keeps influencing the current value. These providers use the
  *maximal contiguous run ending at the latest visible bar* (``_contiguous_tail``): with more
  visible history the seed happens earlier and the smoothed value differs, by design. A run shorter
  than the indicator's minimum bar count (stated per provider below) gives ``None``.

Objects (ADR-0085 §"特征" table; names chosen here, not fixed by the ADR):

- ``AtrProvider`` (``atr_<period>``, IND-ATR-001): Wilder ATR;
- ``RsiProvider`` (``rsi_<period>``, IND-RSI-001): Wilder RSI, ``avg_loss == 0`` → RSI = 100;
- ``MacdLineProvider`` / ``MacdSignalProvider`` (``macd_<fast>_<slow>_<signal>`` /
  ``macd_signal_<fast>_<slow>_<signal>``, IND-MACD-001): EMA(fast) − EMA(slow) and its EMA(signal);
- ``BbandsPercentBProvider`` / ``BbandsBandwidthProvider`` (``bbands_percent_b_<window>_<k>`` /
  ``bbands_bandwidth_<window>_<k>``, IND-BBANDS-001): Bollinger %b and bandwidth;
- ``VwapProvider`` (``vwap_<window>``, IND-VWAP-001): trailing volume-weighted average price;
- ``AdxProvider`` (``adx_<period>``, FEA-TREND-STRENGTH-001): Wilder ADX;
- ``BarCloseProvider`` / ``BarHighProvider`` / ``BarLowProvider`` (``bar_close`` / ``bar_high`` /
  ``bar_low``, all ``@1.0.0``, no parameters): the close / high / low of the latest visible bar,
  exact — added at PM's direction (not an ADR-0085 table line) for ``donchian_breakout@1.0.0`` /
  ``zscore_reversion@1.0.0`` to read a price signal from.

Division by zero (flat true range, zero band width, zero volume, …) is fail-closed: the value is
``None``, never a raised exception and never an arbitrary number (ADR-0085 §"通用规则" #4). All
arithmetic runs at 50 significant digits, half-even rounding (platform independent), then is
quantized to the spec's ``scale`` decimal places.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    localcontext,
)
from itertools import pairwise
from typing import ClassVar, Final

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
from plugins.features.bars import BAR_1M_INPUT

__all__ = [
    "AdxProvider",
    "AtrProvider",
    "BarCloseProvider",
    "BarHighProvider",
    "BarLowProvider",
    "BbandsBandwidthProvider",
    "BbandsPercentBProvider",
    "MacdLineProvider",
    "MacdSignalProvider",
    "RsiProvider",
    "VwapProvider",
]

#: 50 significant digits, half-even: correctly-rounded and platform independent (decimal module),
#: same convention as ``bars.py``'s ``_LOG_CONTEXT``. The default ``Context(...)`` traps
#: ``DivisionByZero`` / ``InvalidOperation`` / ``Overflow`` (not ``Inexact``, which we want here —
#: these indicators are inherently divisions, not exact sums).
_MATH_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


class _Bar:
    __slots__ = ("available_time", "close", "end", "high", "low", "open", "start", "volume")

    def __init__(self, item: FeatureObservation) -> None:
        if item.event_end_time is None:
            raise FeatureInputError(f"{item.observation_key!r} is not a bar (no event_end_time)")
        self.start: datetime = item.event_time
        self.end: datetime = item.event_end_time
        self.available_time: datetime = item.available_time
        self.open = _decimal(item, "open")
        self.high = _decimal(item, "high")
        self.low = _decimal(item, "low")
        self.close = _decimal(item, "close")
        self.volume = _decimal(item, "volume")
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise FeatureInputError(f"{item.observation_key!r} has a non-positive OHLC value")
        if self.high < self.low:
            raise FeatureInputError(f"{item.observation_key!r} has high < low")
        if self.volume < 0:
            raise FeatureInputError(f"{item.observation_key!r} has a negative volume")


def _decimal(item: FeatureObservation, name: str) -> Decimal:
    value = item.values.get(name)
    if not isinstance(value, Decimal):
        raise FeatureInputError(f"{item.observation_key!r} has no Decimal {name!r} value")
    return value


def _bars(visible: Sequence[FeatureObservation]) -> list[_Bar]:
    symbols = {item.values.get("symbol") for item in visible}
    if len(symbols) > 1:
        raise FeatureInputError("a request must hold the bars of one symbol")
    bars = sorted((_Bar(item) for item in visible), key=lambda bar: bar.start)
    for earlier, later in pairwise(bars):
        if later.start < earlier.end:
            raise FeatureInputError(f"two bars overlap at {later.start.isoformat()}")
    return bars


def _trailing_run(bars: Sequence[_Bar], count: int) -> Sequence[_Bar] | None:
    """The ``count`` latest bars when they are contiguous; ``None`` otherwise."""
    if len(bars) < count:
        return None
    run = bars[len(bars) - count :]
    if any(earlier.end != later.start for earlier, later in pairwise(run)):
        return None
    return run


def _contiguous_tail(bars: Sequence[_Bar]) -> list[_Bar]:
    """The maximal suffix of ``bars`` (sorted by ``start``) that is internally contiguous."""
    if not bars:
        return []
    tail = [bars[-1]]
    for bar in reversed(bars[:-1]):
        if bar.end == tail[0].start:
            tail.insert(0, bar)
        else:
            break
    return tail


def _positive_int(params: Mapping[str, object], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


def _nonneg_int(params: Mapping[str, object], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")
    return value


def _decimal_param(params: Mapping[str, object], name: str) -> Decimal:
    value = params.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string, got {value!r}")
    try:
        parsed = Decimal(value)
    except DecimalException:
        raise ValueError(f"{name} must be a decimal string, got {value!r}") from None
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _name_token(value: Decimal) -> str:
    """A ``NAME_PATTERN``-safe token for a positive Decimal used inside a spec name."""
    text = format(value, "f")
    if not text.replace(".", "").isdigit():
        raise ValueError(f"cannot form a name token from {value!r}")
    return text.replace(".", "p")


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


def _answer(at: datetime, value: Decimal | None, used: Sequence[_Bar]) -> FeatureValue:
    if value is None:
        return FeatureValue(evaluation_time=at, value=None, inputs_used=0)
    return FeatureValue(
        evaluation_time=at,
        value=value,
        inputs_used=len(used),
        latest_input_available_time=max(bar.available_time for bar in used),
    )


def _true_range(prev: _Bar, cur: _Bar) -> Decimal:
    return max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))


def _directional_moves(prev: _Bar, cur: _Bar) -> tuple[Decimal, Decimal]:
    up_move = cur.high - prev.high
    down_move = prev.low - cur.low
    plus_dm = up_move if up_move > down_move and up_move > 0 else Decimal(0)
    minus_dm = down_move if down_move > up_move and down_move > 0 else Decimal(0)
    return plus_dm, minus_dm


def _directional_index(tr_sum: Decimal, plus_sum: Decimal, minus_sum: Decimal) -> Decimal:
    """``100 * |+DI - -DI| / (+DI + -DI)``; raises a Decimal signal when either sum is zero."""
    plus_di = Decimal(100) * plus_sum / tr_sum
    minus_di = Decimal(100) * minus_sum / tr_sum
    total = plus_di + minus_di
    return Decimal(100) * abs(plus_di - minus_di) / total


class _IndicatorProvider:
    """Shared plumbing: declared specs, descriptor, per-time visible bars (mirrors ``bars.py``)."""

    NAME: ClassVar[str]
    VERSION: ClassVar[str] = "1.0.0"

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
            bars = _bars(request.visible_at(at, spec.available_lag))
            values.append(self._value(spec, at, bars))
        return FeatureResult.build(request, self._descriptor, values)

    # ------------------------------------------------------------------ per provider

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        """The spec this provider would build from ``spec``'s own parameters."""
        raise NotImplementedError

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        raise NotImplementedError


# ======================================================================================
# IND-ATR-001 — Wilder ATR
# ======================================================================================


class AtrProvider(_IndicatorProvider):
    """Wilder ATR(period): seeded by the mean of the first ``period`` TR values, Wilder-smoothed
    through every later bar of the trailing contiguous run."""

    NAME = "atr"

    @staticmethod
    def spec(
        period: int,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"period": period}, "period")
        _nonneg_int({"scale": scale}, "scale")
        return _build(
            f"atr_{period}",
            "Wilder ATR(period): TR_i = max(H_i-L_i, |H_i-C_{i-1}|, |L_i-C_{i-1}|) over the "
            "trailing contiguous run of visible bars. The mean of the first `period` TR values "
            "(the run's earliest `period`+1 bars) seeds ATR; every later bar of the run then "
            "updates it, ATR_i = (ATR_{i-1}*(period-1) + TR_i) / period. None when the run holds "
            "fewer than `period`+1 bars. 50-digit division, quantized to `scale` places, "
            "half-even.",
            {"period": period, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "period"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        period = _positive_int(spec.params, "period")
        scale = _nonneg_int(spec.params, "scale")
        run = _contiguous_tail(bars)
        if len(run) < period + 1:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                trs = [_true_range(a, b) for a, b in pairwise(run)]
                atr = sum(trs[:period], Decimal(0)) / period
                for tr in trs[period:]:
                    atr = (atr * (period - 1) + tr) / period
                value = atr.quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


# ======================================================================================
# IND-RSI-001 — Wilder RSI
# ======================================================================================


class RsiProvider(_IndicatorProvider):
    """Wilder RSI(period): average gain / average loss, both Wilder-smoothed; ``avg_loss == 0``
    → RSI = 100 (ADR-0085)."""

    NAME = "rsi"

    @staticmethod
    def spec(
        period: int,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"period": period}, "period")
        _nonneg_int({"scale": scale}, "scale")
        return _build(
            f"rsi_{period}",
            "Wilder RSI(period): gain_i = max(C_i - C_{i-1}, 0), loss_i = max(C_{i-1} - C_i, 0) "
            "over the trailing contiguous run. The mean of the first `period` gains / losses "
            "seeds avg_gain / avg_loss; every later bar updates them, "
            "avg_x_i = (avg_x_{i-1}*(period-1) + x_i) / period. "
            "RSI = 100 - 100/(1+avg_gain/avg_loss); avg_loss == 0 -> RSI = 100. "
            "None when the run holds fewer than `period`+1 bars. 50-digit division, quantized to "
            "`scale` places, half-even.",
            {"period": period, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "period"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        period = _positive_int(spec.params, "period")
        scale = _nonneg_int(spec.params, "scale")
        run = _contiguous_tail(bars)
        if len(run) < period + 1:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                deltas = [b.close - a.close for a, b in pairwise(run)]
                gains = [max(d, Decimal(0)) for d in deltas]
                losses = [max(-d, Decimal(0)) for d in deltas]
                avg_gain = sum(gains[:period], Decimal(0)) / period
                avg_loss = sum(losses[:period], Decimal(0)) / period
                for gain, loss in zip(gains[period:], losses[period:], strict=True):
                    avg_gain = (avg_gain * (period - 1) + gain) / period
                    avg_loss = (avg_loss * (period - 1) + loss) / period
                if avg_loss == 0:
                    rsi = Decimal(100)
                else:
                    rsi = Decimal(100) - Decimal(100) / (Decimal(1) + avg_gain / avg_loss)
                value = rsi.quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


# ======================================================================================
# IND-MACD-001 — MACD line and signal line
# ======================================================================================


def _validate_macd_params(fast: int, slow: int, signal: int) -> None:
    _positive_int({"fast": fast}, "fast")
    _positive_int({"slow": slow}, "slow")
    _positive_int({"signal": signal}, "signal")
    if fast >= slow:
        raise ValueError(f"fast must be < slow, got fast={fast} slow={slow}")


def _macd_series(
    closes: Sequence[Decimal], fast: int, slow: int, signal: int
) -> tuple[Decimal, Decimal | None] | None:
    """``(macd_value, signal_value)``; ``signal_value`` is ``None`` without enough MACD points.

    Both EMA(fast) and EMA(slow) are seeded by the *same* SMA of the first `slow` closes (so the
    first MACD point is exactly 0), then each updated bar by bar with its own alpha = 2/(n+1). The
    signal line is the EMA(signal) of the resulting MACD series, seeded the same way (SMA of its
    first `signal` points).
    """
    if len(closes) < slow:
        return None
    alpha_fast = Decimal(2) / (fast + 1)
    alpha_slow = Decimal(2) / (slow + 1)
    seed = sum(closes[:slow], Decimal(0)) / slow
    ema_fast = seed
    ema_slow = seed
    macd_values = [ema_fast - ema_slow]
    for close in closes[slow:]:
        ema_fast = close * alpha_fast + ema_fast * (1 - alpha_fast)
        ema_slow = close * alpha_slow + ema_slow * (1 - alpha_slow)
        macd_values.append(ema_fast - ema_slow)
    macd_value = macd_values[-1]
    if len(macd_values) < signal:
        return macd_value, None
    alpha_signal = Decimal(2) / (signal + 1)
    ema_signal = sum(macd_values[:signal], Decimal(0)) / signal
    for point in macd_values[signal:]:
        ema_signal = point * alpha_signal + ema_signal * (1 - alpha_signal)
    return macd_value, ema_signal


class MacdLineProvider(_IndicatorProvider):
    """MACD line = EMA(fast) - EMA(slow) of close (see ``_macd_series`` for the seeding rule)."""

    NAME = "macd_line"

    @staticmethod
    def spec(
        fast: int,
        slow: int,
        signal: int,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _validate_macd_params(fast, slow, signal)
        _nonneg_int({"scale": scale}, "scale")
        return _build(
            f"macd_{fast}_{slow}_{signal}",
            "MACD line = EMA(fast) - EMA(slow) of close over the trailing contiguous run. Both "
            "EMAs are seeded by the same SMA of the first `slow` closes (so the first MACD value "
            "is 0), then each updated bar by bar with alpha = 2/(n+1). `signal` is carried in the "
            "spec only to keep the parameter triple identical to the paired "
            "macd_signal_<fast>_<slow>_<signal> feature; it does not affect this value. None when "
            "the run holds fewer than `slow` bars. 50-digit division, quantized to `scale` "
            "places, half-even.",
            {"fast": fast, "slow": slow, "signal": signal, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "fast"),
            _positive_int(spec.params, "slow"),
            _positive_int(spec.params, "signal"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        fast = _positive_int(spec.params, "fast")
        slow = _positive_int(spec.params, "slow")
        signal = _positive_int(spec.params, "signal")
        scale = _nonneg_int(spec.params, "scale")
        run = _contiguous_tail(bars)
        if len(run) < slow:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                result = _macd_series([bar.close for bar in run], fast, slow, signal)
                if result is None:
                    return _answer(at, None, ())
                value = result[0].quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


class MacdSignalProvider(_IndicatorProvider):
    """MACD signal line = EMA(signal) of the MACD line (see ``_macd_series``)."""

    NAME = "macd_signal"

    @staticmethod
    def spec(
        fast: int,
        slow: int,
        signal: int,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _validate_macd_params(fast, slow, signal)
        _nonneg_int({"scale": scale}, "scale")
        return _build(
            f"macd_signal_{fast}_{slow}_{signal}",
            "MACD signal line = EMA(signal) of the MACD line (macd_<fast>_<slow>_<signal>): "
            "seeded by the SMA of the MACD line's first `signal` points, then updated point by "
            "point with alpha = 2/(signal+1). None when the trailing contiguous run holds fewer "
            "than `slow` + `signal` - 1 bars. 50-digit division, quantized to `scale` places, "
            "half-even.",
            {"fast": fast, "slow": slow, "signal": signal, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "fast"),
            _positive_int(spec.params, "slow"),
            _positive_int(spec.params, "signal"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        fast = _positive_int(spec.params, "fast")
        slow = _positive_int(spec.params, "slow")
        signal = _positive_int(spec.params, "signal")
        scale = _nonneg_int(spec.params, "scale")
        run = _contiguous_tail(bars)
        if len(run) < slow + signal - 1:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                result = _macd_series([bar.close for bar in run], fast, slow, signal)
                if result is None or result[1] is None:
                    return _answer(at, None, ())
                value = result[1].quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


# ======================================================================================
# IND-BBANDS-001 — Bollinger %b and bandwidth
# ======================================================================================


class BbandsPercentBProvider(_IndicatorProvider):
    """%b = (close - lower) / (upper - lower), upper/lower = SMA(window) +/- k * population
    stddev, over the `window` latest contiguous visible closes."""

    NAME = "bbands_percent_b"

    @staticmethod
    def spec(
        window: int,
        k: Decimal,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        _nonneg_int({"scale": scale}, "scale")
        if not isinstance(k, Decimal) or not k.is_finite() or k <= 0:
            raise ValueError(f"k must be a positive finite Decimal, got {k!r}")
        return _build(
            f"bbands_percent_b_{window}_{_name_token(k)}",
            "Bollinger %b: (close - lower) / (upper - lower), upper/lower = SMA(window) +/- k * "
            "population stddev of the `window` latest contiguous visible closes. None when the "
            "run is shorter than `window`, or upper == lower (zero band width). 50-digit division "
            "and sqrt, quantized to `scale` places, half-even.",
            {"window": window, "k": str(k), "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            _decimal_param(spec.params, "k"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        k = _decimal_param(spec.params, "k")
        scale = _nonneg_int(spec.params, "scale")
        run = _trailing_run(bars, window)
        if run is None:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                closes = [bar.close for bar in run]
                mean = sum(closes, Decimal(0)) / window
                variance = sum(((c - mean) ** 2 for c in closes), Decimal(0)) / window
                std = variance.sqrt()
                lower = mean - k * std
                band_range = 2 * k * std
                value = ((run[-1].close - lower) / band_range).quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


class BbandsBandwidthProvider(_IndicatorProvider):
    """Bandwidth = (upper - lower) / SMA(window) = 2 * k * population stddev / SMA(window)."""

    NAME = "bbands_bandwidth"

    @staticmethod
    def spec(
        window: int,
        k: Decimal,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        _nonneg_int({"scale": scale}, "scale")
        if not isinstance(k, Decimal) or not k.is_finite() or k <= 0:
            raise ValueError(f"k must be a positive finite Decimal, got {k!r}")
        return _build(
            f"bbands_bandwidth_{window}_{_name_token(k)}",
            "Bollinger bandwidth: (upper - lower) / SMA(window) = 2*k*std / SMA(window), std = "
            "population stddev of the `window` latest contiguous visible closes. None when the "
            "run is shorter than `window` (SMA(window) > 0 always holds given positive closes, so "
            "only insufficient history makes this None). 50-digit division and sqrt, quantized to "
            "`scale` places, half-even.",
            {"window": window, "k": str(k), "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            _decimal_param(spec.params, "k"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        k = _decimal_param(spec.params, "k")
        scale = _nonneg_int(spec.params, "scale")
        run = _trailing_run(bars, window)
        if run is None:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                closes = [bar.close for bar in run]
                mean = sum(closes, Decimal(0)) / window
                variance = sum(((c - mean) ** 2 for c in closes), Decimal(0)) / window
                std = variance.sqrt()
                band_range = 2 * k * std
                value = (band_range / mean).quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


# ======================================================================================
# IND-VWAP-001 — trailing VWAP
# ======================================================================================


class VwapProvider(_IndicatorProvider):
    """Trailing `window`-bar VWAP: sum(typical price * volume) / sum(volume), typical price =
    (H+L+C)/3, over the `window` latest contiguous visible bars."""

    NAME = "vwap"

    @staticmethod
    def spec(
        window: int,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        _nonneg_int({"scale": scale}, "scale")
        return _build(
            f"vwap_{window}",
            "Trailing `window`-bar VWAP: sum(typical price * volume) / sum(volume) over the "
            "`window` latest contiguous visible bars, typical price = (H+L+C)/3. None when the "
            "run is shorter than `window`, or the volume sum is zero. 50-digit division, "
            "quantized to `scale` places, half-even.",
            {"window": window, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        scale = _nonneg_int(spec.params, "scale")
        run = _trailing_run(bars, window)
        if run is None:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                numerator = sum(
                    (((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in run),
                    Decimal(0),
                )
                denominator = sum((bar.volume for bar in run), Decimal(0))
                value = (numerator / denominator).quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)


# ======================================================================================
# bar_close / bar_high / bar_low — the latest visible bar's price, exact, no parameters
#
# Added at PM's direction (not itself an ADR-0085 table line) to give the price signals used by
# donchian_breakout@1.0.0 / zscore_reversion@1.0.0 (ADR-0085 §"策略") a Feature to read from:
# the identities below (``bar_close@1.0.0`` / ``bar_high@1.0.0`` / ``bar_low@1.0.0``) are the
# ones PM specified verbatim. ``research/strategies/price_signals.py`` (the module PM says
# defines ``BAR_CLOSE_SIGNAL`` / ``BAR_HIGH_SIGNAL`` / ``BAR_LOW_SIGNAL``) does not exist in this
# worktree at the time of writing, so the exact constant strings there could not be cross-checked
# — only the three identities PM gave directly were used.
# ======================================================================================


class BarCloseProvider(_IndicatorProvider):
    """The close of the latest visible bar (by ``event_time``): exact, no window, no params."""

    NAME = "bar_close"

    @staticmethod
    def spec(
        *,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        return _build(
            "bar_close",
            "The close of the latest visible bar (by event_time); exact pass-through, no "
            "window, no rounding, no parameters. None when no bar is visible.",
            {},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            available_lag=spec.available_lag, bar_input=_single_input(spec), version=spec.version
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        if not bars:
            return _answer(at, None, ())
        latest = bars[-1]
        return _answer(at, latest.close, (latest,))


class BarHighProvider(_IndicatorProvider):
    """The high of the latest visible bar: exact, no window, no params."""

    NAME = "bar_high"

    @staticmethod
    def spec(
        *,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        return _build(
            "bar_high",
            "The high of the latest visible bar (by event_time); exact pass-through, no "
            "window, no rounding, no parameters. None when no bar is visible.",
            {},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            available_lag=spec.available_lag, bar_input=_single_input(spec), version=spec.version
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        if not bars:
            return _answer(at, None, ())
        latest = bars[-1]
        return _answer(at, latest.high, (latest,))


class BarLowProvider(_IndicatorProvider):
    """The low of the latest visible bar: exact, no window, no params."""

    NAME = "bar_low"

    @staticmethod
    def spec(
        *,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        return _build(
            "bar_low",
            "The low of the latest visible bar (by event_time); exact pass-through, no "
            "window, no rounding, no parameters. None when no bar is visible.",
            {},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            available_lag=spec.available_lag, bar_input=_single_input(spec), version=spec.version
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        if not bars:
            return _answer(at, None, ())
        latest = bars[-1]
        return _answer(at, latest.low, (latest,))


# ======================================================================================
# FEA-TREND-STRENGTH-001 — Wilder ADX
# ======================================================================================


class AdxProvider(_IndicatorProvider):
    """Wilder ADX(period): DX from Wilder-smoothed +DM/-DM/TR, then ADX = Wilder-smoothed DX."""

    NAME = "adx"

    @staticmethod
    def spec(
        period: int,
        *,
        scale: int,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"period": period}, "period")
        _nonneg_int({"scale": scale}, "scale")
        return _build(
            f"adx_{period}",
            "Wilder ADX(period) over the trailing contiguous run: TR / +DM / -DM per bar pair are "
            "Wilder-smoothed (seeded by the sum of the first `period` values, then "
            "smoothed_x_i = smoothed_x_{i-1} - smoothed_x_{i-1}/period + x_i for every later bar), "
            "giving DX_i = 100*|+DI_i - -DI_i|/(+DI_i + -DI_i) with +DI_i = 100*smoothed_+DM_i / "
            "smoothed_TR_i (-DI_i likewise). ADX is the mean of the first `period` DX values, then "
            "Wilder-smoothed, ADX_i = (ADX_{i-1}*(period-1) + DX_i)/period, through the remaining "
            "DX values. None when the run holds fewer than 2*period bars, or any TR / DI sum used "
            "along the way is zero. 50-digit division, quantized to `scale` places, half-even.",
            {"period": period, "scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "period"),
            scale=_nonneg_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        period = _positive_int(spec.params, "period")
        scale = _nonneg_int(spec.params, "scale")
        run = _contiguous_tail(bars)
        if len(run) < 2 * period:
            return _answer(at, None, ())
        try:
            with localcontext(_MATH_CONTEXT):
                trs: list[Decimal] = []
                plus_dms: list[Decimal] = []
                minus_dms: list[Decimal] = []
                for earlier, later in pairwise(run):
                    trs.append(_true_range(earlier, later))
                    plus_dm, minus_dm = _directional_moves(earlier, later)
                    plus_dms.append(plus_dm)
                    minus_dms.append(minus_dm)

                smoothed_tr = sum(trs[:period], Decimal(0))
                smoothed_plus = sum(plus_dms[:period], Decimal(0))
                smoothed_minus = sum(minus_dms[:period], Decimal(0))
                dx_values = [_directional_index(smoothed_tr, smoothed_plus, smoothed_minus)]
                for tr, plus_dm, minus_dm in zip(
                    trs[period:], plus_dms[period:], minus_dms[period:], strict=True
                ):
                    smoothed_tr = smoothed_tr - smoothed_tr / period + tr
                    smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm
                    smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm
                    dx_values.append(_directional_index(smoothed_tr, smoothed_plus, smoothed_minus))

                if len(dx_values) < period:
                    return _answer(at, None, ())
                adx = sum(dx_values[:period], Decimal(0)) / period
                for dx in dx_values[period:]:
                    adx = (adx * (period - 1) + dx) / period
                value = adx.quantize(Decimal(1).scaleb(-scale))
        except DecimalException:
            return _answer(at, None, ())
        return _answer(at, value, run)
