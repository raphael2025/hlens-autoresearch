"""Range- and return-based volatility FeatureProviders over bar OHLC (ADR-0085 §特征).

Four deterministic providers, each a plugin behind the ``FeatureProvider`` Protocol (ADR-0030),
following the same shape as ``plugins/features/bars.py``: every parameter (window, output scale,
declared lag, input representation) is part of the ``FeatureSpec`` a provider serves and is bound
by the spec hash in the descriptor. **No parameter has a library default** (ADR-0085 §通用规则 2):
``window`` is a required positional argument, never defaulted.

Input: one bar per observation, ``[event_time, event_end_time)`` with ``open`` / ``high`` / ``low``
/ ``close`` values (PIT-selected ``canonical.bars_1m`` rows or complete derived bars). All bars of a
request must be one symbol; overlapping bars are refused (``FeatureInputError``). A bar's
``high``/``low`` must be positive with ``low <= open, close <= high``; a bar violating this is
rejected as malformed input, never silently clamped — every formula below assumes it (see each
provider's docstring for why that keeps its output non-negative before the square root).

At each evaluation time ``t`` a provider orders the visible bars by ``event_time`` and looks at the
trailing run: a window of ``k`` bars counts only when the ``k`` latest bars are contiguous
(``end == next start``). Otherwise the value is ``None``: nothing is filled or interpolated.

Logarithms and square roots are computed in the exact same fixed 50-significant-digit ``Context``
that ``plugins/features/bars.py`` uses for its own ``ln`` / ``sqrt`` (``bars._LOG_CONTEXT``,
imported here rather than redefined, so both files round the same way and stay platform
independent), then quantized to ``scale`` decimal places, half-even. ``FEA-JUMP-QV-001`` needs
Decimal Pi at that same precision; the ``decimal`` module has no built-in constant for it, so this
module computes one with the series recipe from the standard library's own decimal documentation
(a Machin-like arctan series for ``4*atan(1)``) — deterministic and free of floats.

- ``ParkinsonVolatilityProvider`` (``parkinson_vol_<window>``, FEA-PARKINSON-001): trailing mean of
  the per-bar Parkinson variance ``(ln(H/L))^2 / (4 ln 2)`` over ``window`` bars, square-rooted;
- ``GarmanKlassVolatilityProvider`` (``garman_klass_vol_<window>``, FEA-GK-001): trailing mean of
  ``0.5*(ln(H/L))^2 - (2 ln 2 - 1)*(ln(C/O))^2`` over ``window`` bars, square-rooted. Given
  ``low <= open, close <= high`` this per-bar term is always ``>= 0`` (``|ln(C/O)| <= ln(H/L)``, so
  ``0.5*x^2 - (2 ln 2 - 1)*y^2 >= 0.5*x^2 - (2 ln 2 - 1)*x^2 = (1.5 - 2 ln 2)*x^2 >= 0``), so the
  mean needs no truncation before the square root;
- ``YangZhangVolatilityProvider`` (``yang_zhang_vol_<window>``, FEA-YZ-001): overnight
  (close-to-open across bars) sample variance + ``k`` * open-to-close sample variance + ``(1-k)`` *
  mean Rogers-Satchell variance, over the ``window`` (= ``n``) latest bars using their
  ``window + 1`` contiguous bars (the overnight return needs each bar's predecessor),
  ``k = 0.34 / (1.34 + (n+1) / (n-1))``; every summand is non-negative (sample variances, and
  Rogers-Satchell is a sum of two non-positive-times-non-positive and
  non-negative-times-non-negative products given the same OHLC ordering), so again no truncation
  is needed. ``window`` must be ``>= 2`` (``n - 1`` in the formula) — a smaller window is refused
  at spec construction, not silently coerced;
- ``JumpVarianceProvider`` (``jump_qv_<window>``, FEA-JUMP-QV-001): realized variance minus
  bipower variation, ``max(RV - (pi/2) * sum(|r_t| * |r_{t-1}|), 0)``, over the ``window`` latest
  one-bar log returns from ``window + 1`` contiguous bars. Unlike the three volatility providers
  above this is a variance-scale "jump quadratic variation", not square-rooted (ADR-0085 names it
  QV, not vol), and it truncates to ``>= 0`` explicitly (finite-sample RV - BPV can go negative;
  that is the one formula in this batch ADR-0085 says to clip).

Division by zero never occurs in these formulas from bar data alone (every denominator is either a
window-derived integer >= 1, or ``4 ln 2`` / ``n - 1`` / the Corwin-Schultz constant, all fixed by
construction); the "missing" case here is purely insufficient / non-contiguous history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, getcontext, localcontext
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
from plugins.features.bars import BAR_1M_INPUT, DEFAULT_SCALE, _LOG_CONTEXT

__all__ = [
    "GarmanKlassVolatilityProvider",
    "JumpVarianceProvider",
    "ParkinsonVolatilityProvider",
    "YangZhangVolatilityProvider",
]


def _decimal_pi() -> Decimal:
    """Pi at the active context's precision (the recipe from the ``decimal`` module docs).

    Deterministic and float-free: a Machin-like series evaluated purely in ``Decimal``, so the
    result is exactly reproducible at whatever precision the surrounding context asks for.
    """
    ctx = getcontext()
    ctx.prec += 2
    three = Decimal(3)
    lasts, t, s, n, na, d, da = Decimal(0), three, Decimal(3), 1, 0, 0, 24
    while s != lasts:
        lasts = s
        n, na = n + na, na + 8
        d, da = d + da, da + 32
        t = (t * n) / d
        s += t
    ctx.prec -= 2
    return +s


with localcontext(_LOG_CONTEXT):
    _LN2: Final = Decimal(2).ln()
    _FOUR_LN2: Final = 4 * _LN2
    _TWO_LN2_MINUS_1: Final = 2 * _LN2 - 1
    _PI: Final = _decimal_pi()
    _HALF_PI: Final = _PI / 2


class _RangeBar:
    __slots__ = ("available_time", "close", "end", "high", "low", "open", "start")

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
        if self.low <= 0 or self.high <= 0:
            raise FeatureInputError(f"{item.observation_key!r} has a non-positive high/low")
        if self.high < self.low:
            raise FeatureInputError(f"{item.observation_key!r} has high < low")
        if not (self.low <= self.open <= self.high):
            raise FeatureInputError(f"{item.observation_key!r} has open outside [low, high]")
        if not (self.low <= self.close <= self.high):
            raise FeatureInputError(f"{item.observation_key!r} has close outside [low, high]")


def _decimal(item: FeatureObservation, name: str) -> Decimal:
    value = item.values.get(name)
    if not isinstance(value, Decimal):
        raise FeatureInputError(f"{item.observation_key!r} has no Decimal {name!r} value")
    return value


def _bars(visible: Sequence[FeatureObservation]) -> list[_RangeBar]:
    symbols = {item.values.get("symbol") for item in visible}
    if len(symbols) > 1:
        raise FeatureInputError("a request must hold the bars of one symbol")
    bars = sorted((_RangeBar(item) for item in visible), key=lambda bar: bar.start)
    for earlier, later in pairwise(bars):
        if later.start < earlier.end:
            raise FeatureInputError(f"two bars overlap at {later.start.isoformat()}")
    return bars


def _trailing_run(bars: Sequence[_RangeBar], count: int) -> Sequence[_RangeBar] | None:
    """The ``count`` latest bars when they are contiguous; ``None`` otherwise."""
    if len(bars) < count:
        return None
    run = bars[len(bars) - count :]
    if any(earlier.end != later.start for earlier, later in pairwise(run)):
        return None
    return run


def _positive_int(params: Mapping[str, object], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


def _answer(at: datetime, value: Decimal | None, used: Sequence[_RangeBar]) -> FeatureValue:
    if value is None:
        return FeatureValue(evaluation_time=at, value=None, inputs_used=0)
    return FeatureValue(
        evaluation_time=at,
        value=value,
        inputs_used=len(used),
        latest_input_available_time=max(bar.available_time for bar in used),
    )


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


class _RangeFeatureProvider:
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
        raise NotImplementedError

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_RangeBar]) -> FeatureValue:
        raise NotImplementedError


def _quantum(spec: FeatureSpec) -> Decimal:
    return Decimal(1).scaleb(-_positive_int(spec.params, "scale"))


class ParkinsonVolatilityProvider(_RangeFeatureProvider):
    """Trailing sqrt-mean Parkinson variance: ``sqrt(mean((ln(H/L))^2 / (4 ln 2)))``."""

    NAME = "parkinson_vol"

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
            f"parkinson_vol_{window}",
            "sqrt(trailing mean over the `window` latest visible bars of "
            "(ln(high/low))^2 / (4 ln 2)) when contiguous; None otherwise. 50-digit ln and sqrt, "
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

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_RangeBar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        with localcontext(_LOG_CONTEXT):
            total = sum(((bar.high / bar.low).ln() ** 2 for bar in run), Decimal(0))
            mean = total / (_FOUR_LN2 * window)
            value = mean.sqrt().quantize(quantum)
        return _answer(at, value, run)


class GarmanKlassVolatilityProvider(_RangeFeatureProvider):
    """Trailing sqrt-mean Garman-Klass variance (module docstring proves it stays ``>= 0``)."""

    NAME = "garman_klass_vol"

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
            f"garman_klass_vol_{window}",
            "sqrt(trailing mean over the `window` latest visible bars of "
            "0.5*(ln(high/low))^2 - (2 ln 2 - 1)*(ln(close/open))^2) when contiguous; None "
            "otherwise. 50-digit ln and sqrt, quantized to `scale` places, half-even.",
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

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_RangeBar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        with localcontext(_LOG_CONTEXT):
            total = Decimal(0)
            for bar in run:
                hl = (bar.high / bar.low).ln()
                co = (bar.close / bar.open).ln()
                total += Decimal("0.5") * hl**2 - _TWO_LN2_MINUS_1 * co**2
            mean = total / window
            value = mean.sqrt().quantize(quantum)
        return _answer(at, value, run)


class YangZhangVolatilityProvider(_RangeFeatureProvider):
    """Trailing sqrt Yang-Zhang variance over ``window`` (= ``n``, must be ``>= 2``) bars."""

    NAME = "yang_zhang_vol"

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
        if window < 2:
            raise ValueError(
                f"window must be >= 2 for Yang-Zhang (n - 1 appears in the formula), got {window}"
            )
        return _build(
            f"yang_zhang_vol_{window}",
            "sqrt(overnight sample variance + k * open-to-close sample variance + (1-k) * mean "
            "Rogers-Satchell variance) over the `window` (= n) latest visible bars using their "
            "`window` + 1 contiguous bars, k = 0.34 / (1.34 + (n+1)/(n-1)); None when not "
            "contiguous. 50-digit ln and sqrt, quantized to `scale` places, half-even.",
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

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_RangeBar]) -> FeatureValue:
        n = _positive_int(spec.params, "window")
        run = _trailing_run(bars, n + 1)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        with localcontext(_LOG_CONTEXT):
            overnight = [(cur.open / prev.close).ln() for prev, cur in pairwise(run)]
            later = run[1:]
            open_close = [(bar.close / bar.open).ln() for bar in later]
            rogers_satchell = [
                (bar.high / bar.close).ln() * (bar.high / bar.open).ln()
                + (bar.low / bar.close).ln() * (bar.low / bar.open).ln()
                for bar in later
            ]
            n_dec = Decimal(n)
            o_mean = sum(overnight, Decimal(0)) / n_dec
            c_mean = sum(open_close, Decimal(0)) / n_dec
            v_o = sum(((x - o_mean) ** 2 for x in overnight), Decimal(0)) / (n_dec - 1)
            v_c = sum(((x - c_mean) ** 2 for x in open_close), Decimal(0)) / (n_dec - 1)
            v_rs = sum(rogers_satchell, Decimal(0)) / n_dec
            k = Decimal("0.34") / (Decimal("1.34") + (n_dec + 1) / (n_dec - 1))
            variance = v_o + k * v_c + (1 - k) * v_rs
            value = variance.sqrt().quantize(quantum)
        return _answer(at, value, run)


class JumpVarianceProvider(_RangeFeatureProvider):
    """``max(RV - (pi/2) * bipower variation, 0)`` over ``window`` one-bar log returns."""

    NAME = "jump_qv"

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
            f"jump_qv_{window}",
            "max(realized variance - (pi/2) * sum(|r_t|*|r_{t-1}|), 0) over the `window` latest "
            "one-bar log returns r_i = ln(close[i]/close[i-1]), from the `window` + 1 latest "
            "visible bars when contiguous; None otherwise. 50-digit ln, quantized to `scale` "
            "places, half-even. Not square-rooted: variance scale, not volatility scale.",
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

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_RangeBar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window + 1)
        if run is None:
            return _answer(at, None, ())
        quantum = _quantum(spec)
        with localcontext(_LOG_CONTEXT):
            returns = [(b.close / a.close).ln() for a, b in pairwise(run)]
            rv = sum((r**2 for r in returns), Decimal(0))
            bpv = _HALF_PI * sum(
                (abs(returns[i]) * abs(returns[i - 1]) for i in range(1, len(returns))), Decimal(0)
            )
            jump = rv - bpv
            value = (jump if jump > 0 else Decimal(0)).quantize(quantum)
        return _answer(at, value, run)
