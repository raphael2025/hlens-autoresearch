"""First FeatureProviders over bar observations (Phase 1 F4; ADR-0030).

Three deterministic providers, each a plugin behind the ``FeatureProvider`` Protocol. They read
only the request (the visible set of ``FeatureRequest.visible_at``), use exact ``Decimal``
arithmetic and never emit floats. Every parameter — the rolling window, the output scale, the
declared lag, the input representation — is part of the ``FeatureSpec`` a provider serves, so it
is bound by the spec hash in the descriptor. A window is a feature parameter, not a validation
threshold.

Input: one bar per observation, ``[event_time, event_end_time)`` with ``close`` / ``volume``
values (PIT-selected ``canonical.bars_1m`` rows or complete derived bars, built by
``infrastructure/feature``).
All bars of a request must be one symbol; overlapping bars are refused (``FeatureInputError``).

At each evaluation time ``t`` a provider orders the visible bars by ``event_time`` and looks at the
trailing run: a window of ``k`` bars counts only when the ``k`` latest bars are contiguous
(``end == next start``). Otherwise — too few bars, or a gap inside the window — the value is
``None``: nothing is filled or interpolated. A stale trailing run is still a value;
``latest_input_available_time`` says how old it is.

- ``BarLogReturnProvider`` (``bar_log_return``): ``ln(close[k] / close[k-1])`` of the last two
  contiguous bars;
- ``BarRealizedVolatilityProvider`` (``bar_realized_vol_<n>``): ``sqrt(sum r_i^2)`` over the last
  ``n`` contiguous one-bar log returns (``n + 1`` bars);
- ``BarVolumeSumProvider`` (``bar_volume_sum_<n>``): exact ``sum volume`` over the last ``n``
  contiguous bars.

Logarithms and square roots are computed at 50 significant digits (the ``decimal`` module rounds
them correctly, so the result is platform independent) and quantized to ``scale`` decimal places,
half-even. Volume sums are exact (an inexact step is an error, never a silently rounded value).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import (
    ROUND_HALF_EVEN,
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
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import FeatureSpec

__all__ = [
    "BAR_1M_INPUT",
    "DEFAULT_SCALE",
    "BarLogReturnProvider",
    "BarRealizedVolatilityProvider",
    "BarVolumeSumProvider",
]

#: The input representation of PIT-selected Canonical 1m bars.
BAR_1M_INPUT: Final = Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")
#: Decimal places of log-return based outputs.
DEFAULT_SCALE: Final = 18
_LOG_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
#: decimal(38, 18) volumes: sums of many bars stay exact far below 80 digits.
_EXACT: Final = Context(prec=80, traps=[Inexact, InvalidOperation, Overflow, DivisionByZero])


class _Bar:
    __slots__ = ("available_time", "close", "end", "start", "volume")

    def __init__(self, item: FeatureObservation) -> None:
        if item.event_end_time is None:
            raise FeatureInputError(f"{item.observation_key!r} is not a bar (no event_end_time)")
        self.start: datetime = item.event_time
        self.end: datetime = item.event_end_time
        self.available_time: datetime = item.available_time
        self.close = _decimal(item, "close")
        self.volume = _decimal(item, "volume")
        if self.close <= 0:
            raise FeatureInputError(f"{item.observation_key!r} has a non-positive close")
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


def _log_return(earlier: _Bar, later: _Bar) -> Decimal:
    return (later.close / earlier.close).ln()


def _positive_int(params: Mapping[str, object], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


class _BarFeatureProvider:
    """Shared plumbing: declared specs, descriptor, per-time visible bars."""

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


def _answer(at: datetime, value: Decimal | None, used: Sequence[_Bar]) -> FeatureValue:
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


class BarLogReturnProvider(_BarFeatureProvider):
    """``ln(close_k / close_{k-1})`` of the two latest contiguous visible bars."""

    NAME = "bar_log_return"

    @staticmethod
    def spec(
        *,
        scale: int = DEFAULT_SCALE,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        return _build(
            "bar_log_return",
            "ln(close[k] / close[k-1]) of the two latest visible bars when contiguous "
            "(end[k-1] == start[k]); None otherwise. 50-digit ln, quantized to `scale` places, "
            "half-even.",
            {"scale": scale},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            scale=_positive_int(spec.params, "scale"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        run = _trailing_run(bars, 2)
        if run is None:
            return _answer(at, None, ())
        quantum = Decimal(1).scaleb(-_positive_int(spec.params, "scale"))
        with localcontext(_LOG_CONTEXT):
            value = _log_return(run[0], run[1]).quantize(quantum)
        return _answer(at, value, run)


class BarRealizedVolatilityProvider(_BarFeatureProvider):
    """``sqrt(sum r_i^2)`` over the ``window`` latest contiguous one-bar log returns."""

    NAME = "bar_realized_volatility"

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
            f"bar_realized_vol_{window}",
            "sqrt(sum over the `window` latest one-bar log returns r_i = ln(close[i] / close[i-1]) "
            "of r_i^2), from the `window` + 1 latest visible bars when contiguous; None otherwise. "
            "50-digit ln and sqrt, quantized to `scale` places, half-even.",
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

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        window = _positive_int(spec.params, "window")
        run = _trailing_run(bars, window + 1)
        if run is None:
            return _answer(at, None, ())
        quantum = Decimal(1).scaleb(-_positive_int(spec.params, "scale"))
        with localcontext(_LOG_CONTEXT):
            total = sum((_log_return(a, b) ** 2 for a, b in pairwise(run)), Decimal(0))
            value = total.sqrt().quantize(quantum)
        return _answer(at, value, run)


class BarVolumeSumProvider(_BarFeatureProvider):
    """Exact sum of ``volume`` over the ``window`` latest contiguous visible bars."""

    NAME = "bar_volume_sum"

    @staticmethod
    def spec(
        window: int,
        *,
        available_lag: timedelta = timedelta(0),
        bar_input: Ref = BAR_1M_INPUT,
        version: str = "1.0.0",
    ) -> FeatureSpec:
        _positive_int({"window": window}, "window")
        return _build(
            f"bar_volume_sum_{window}",
            "exact sum of volume over the `window` latest visible bars when contiguous; None "
            "otherwise.",
            {"window": window},
            version=version,
            available_lag=available_lag,
            bar_input=bar_input,
        )

    def _canonical(self, spec: FeatureSpec) -> FeatureSpec:
        return self.spec(
            _positive_int(spec.params, "window"),
            available_lag=spec.available_lag,
            bar_input=_single_input(spec),
            version=spec.version,
        )

    def _value(self, spec: FeatureSpec, at: datetime, bars: Sequence[_Bar]) -> FeatureValue:
        run = _trailing_run(bars, _positive_int(spec.params, "window"))
        if run is None:
            return _answer(at, None, ())
        try:
            with localcontext(_EXACT):
                value = sum((bar.volume for bar in run), Decimal(0))
        except DecimalException:
            raise FeatureInputError("the volume sum is not exact") from None
        return _answer(at, value, run)
