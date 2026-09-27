"""First StateProviders over feature values (Phase 2; ADR-0035).

Three deterministic providers, each a plugin behind the ``StateProvider`` Protocol. They read only
the request (the visible set of ``StateRequest.visible_at``, which the runner also enforces), use
exact ``Decimal`` arithmetic and never emit floats. Every parameter — quantile cut points, minimum
history, trailing window, trend threshold, labels — is part of the ``StateSpec`` a provider serves
(the parameters are encoded in ``StateSpec.method`` by ``core.contracts.state.state_method``), so it
is bound by the spec hash in the descriptor. They are model parameters, not validation thresholds,
and none has a default: each spec states its own.

- ``VolatilityRegimeProvider`` (``trailing_quantile_buckets``): buckets the latest value of one
  realized-volatility feature by empirical quantiles of that feature's non-``None`` values inside
  the fixed trailing ``training_window`` ending at ``t`` (never a full-sample fit);
- ``LiquidityRegimeProvider``: the same model over a volume feature (e.g. ``bar_volume_sum_<n>``);
- ``TrendRangeProvider`` (``efficiency_ratio``): over the ``window`` latest values of one log-return
  feature, ``ER = |sum r| / sum |r|``; ``ER >= threshold`` is a trend (up / down by the sign of the
  sum), otherwise range (``sum |r| == 0`` is range). Rule based: no training window, no seed.

Quantile rule (exact, no interpolation): for ``n`` sorted history values ``h_1 <= ... <= h_n``
the ``q`` cut is ``h_k`` with ``k = max(1, ceil(q * n))``; the label index of a value ``x`` is the
number of cuts strictly below ``x``. The history includes the current value (known at ``t``).

Not computable (``None``): no visible value of the input feature, the latest value is ``None``,
fewer than ``min_history`` non-``None`` values in the window, or fewer than ``window`` returns /
a ``None`` among them. Nothing is filled.

Declared-unavailable: a funding-rate regime needs funding-rate data, which Phase 1 does not collect
(ADR-0022 scope); it is registered in ``DECLARED_UNAVAILABLE`` and docs/research/state-library.md,
and has no provider.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from itertools import pairwise
from typing import ClassVar, Final

from core.contracts.state import (
    MethodParam,
    StateInput,
    StateInputError,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
    UnsupportedState,
    parse_state_method,
    state_method,
)
from core.domain.base import FrozenMapping, Ref
from core.domain.specs import StateSpec

__all__ = [
    "DECLARED_UNAVAILABLE",
    "LIQUIDITY_LABELS",
    "TREND_LABELS",
    "VOLATILITY_LABELS",
    "LiquidityRegimeProvider",
    "TrendRangeProvider",
    "VolatilityRegimeProvider",
]

VOLATILITY_LABELS: Final = ("low_vol", "mid_vol", "high_vol")
LIQUIDITY_LABELS: Final = ("thin", "normal", "deep")
TREND_LABELS: Final = ("trend_down", "range", "trend_up")

#: State families the roadmap names whose inputs are not collected yet (name → reason).
DECLARED_UNAVAILABLE: Final[Mapping[str, str]] = FrozenMapping(
    {
        "funding_regime": (
            "needs funding-rate data; Phase 1 collects spot 1m bars only (ADR-0022); "
            "no FeatureSpec or StateProvider until a funding-rate source is approved"
        )
    }
)

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


def _number(item: StateInput) -> Decimal:
    value = item.value
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise StateInputError(f"{item.feature} @ {item.evaluation_time.isoformat()} is not numeric")
    return Decimal(value)


def _positive_int(params: Mapping[str, MethodParam], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


def _decimal_text(value: str | Decimal) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{value!r} is not a decimal number") from None
    if not parsed.is_finite():
        raise ValueError(f"{value!r} is not finite")
    return parsed


def _cuts(text: MethodParam | None) -> tuple[Decimal, ...]:
    if not isinstance(text, str) or not text:
        raise ValueError("cuts must be a comma-separated list of decimals")
    cuts = tuple(_decimal_text(part) for part in text.split(","))
    if any(not (Decimal(0) < cut < Decimal(1)) for cut in cuts):
        raise ValueError("every cut must be strictly between 0 and 1")
    if any(later <= earlier for earlier, later in pairwise(cuts)):
        raise ValueError("cuts must be strictly ascending")
    return cuts


def _single_feature(spec: StateSpec) -> Ref:
    if len(spec.features) != 1:
        raise ValueError(f"{spec.ref} must have exactly one feature input")
    return spec.features[0]


def _own(visible: Sequence[StateInput], feature: Ref) -> list[StateInput]:
    stray = {str(item.feature) for item in visible if item.feature != feature}
    if stray:
        raise StateInputError(f"inputs outside the spec's feature: {sorted(stray)}")
    return list(visible)  # canonical order: evaluation_time ascending


def _none(at: datetime) -> StateValue:
    return StateValue(evaluation_time=at, state=None, inputs_used=0)


class _StateProviderBase:
    """Shared plumbing: declared specs, descriptor, per-time visible inputs."""

    NAME: ClassVar[str]
    METHOD: ClassVar[str]
    VERSION: ClassVar[str] = "1.0.0"

    def __init__(self, specs: Iterable[StateSpec]) -> None:
        by_ref: dict[str, StateSpec] = {}
        for spec in specs:
            if not isinstance(spec, StateSpec):
                raise TypeError("specs must be StateSpec instances")
            try:
                canonical = self._canonical(spec)
            except ValueError as exc:
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec: {exc}") from None
            if canonical.content_hash() != spec.content_hash():
                raise ValueError(f"{spec.ref} is not a {self.NAME} spec (method or fields)")
            if str(spec.ref) in by_ref:
                raise ValueError(f"{spec.ref} is declared twice")
            by_ref[str(spec.ref)] = spec
        if not by_ref:
            raise ValueError("a provider must serve at least one spec")
        self._specs = by_ref
        self._descriptor = StateProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_states=FrozenMapping(
                {key: spec.content_hash() for key, spec in by_ref.items()}
            ),
        )

    @property
    def descriptor(self) -> StateProviderDescriptor:
        return self._descriptor

    def compute(self, request: StateRequest) -> StateResult:
        spec = self._specs.get(str(request.state))
        if spec is None or spec.content_hash() != request.spec_hash:
            raise UnsupportedState(f"{self.NAME} does not serve {request.state} with this hash")
        _, params = parse_state_method(spec.method)
        feature = _single_feature(spec)
        values = []
        for at in request.evaluation_times:
            own = _own(request.visible_at(at, spec.training_window), feature)
            values.append(self._value(spec, params, at, own))
        return StateResult.build(request, self._descriptor, values)

    def _params(self, spec: StateSpec) -> dict[str, MethodParam]:
        name, params = parse_state_method(spec.method)
        if name != self.METHOD:
            raise ValueError(f"method must be {self.METHOD!r}, got {name!r}")
        return params

    # ------------------------------------------------------------------ per provider

    def _canonical(self, spec: StateSpec) -> StateSpec:
        """The spec this provider would build from ``spec``'s own parameters."""
        raise NotImplementedError

    def _value(
        self,
        spec: StateSpec,
        params: Mapping[str, MethodParam],
        at: datetime,
        own: Sequence[StateInput],
    ) -> StateValue:
        raise NotImplementedError


class _QuantileRegimeProvider(_StateProviderBase):
    """Empirical-quantile buckets over a fixed trailing window (see module docs)."""

    METHOD = "trailing_quantile_buckets"
    LABELS: ClassVar[tuple[str, ...]]

    @classmethod
    def spec(
        cls,
        feature: Ref,
        *,
        cuts: Sequence[str | Decimal],
        min_history: int,
        training_window: timedelta,
        seed: int,
        labels: Sequence[str] | None = None,
        name: str | None = None,
        version: str = "1.0.0",
    ) -> StateSpec:
        """A spec of this model; ``cuts`` / ``min_history`` / window / seed are spec parameters."""
        cut_text = ",".join(str(_decimal_text(cut)) for cut in cuts)
        state_space = tuple(cls.LABELS if labels is None else labels)
        if len(state_space) != len(_cuts(cut_text)) + 1:
            raise ValueError("there must be exactly one more label than cuts")
        _positive_int({"min_history": min_history}, "min_history")
        if not isinstance(training_window, timedelta) or training_window <= timedelta(0):
            raise ValueError("a trained regime needs a positive training_window")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("a trained regime fixes an int seed")
        return StateSpec(
            name=cls.NAME if name is None else name,
            version=version,
            features=(feature,),
            state_space=state_space,
            method=state_method(cls.METHOD, {"cuts": cut_text, "min_history": min_history}),
            training_window=training_window,
            seed=seed,
        )

    def _canonical(self, spec: StateSpec) -> StateSpec:
        params = self._params(spec)
        if spec.training_window is None or spec.seed is None:
            raise ValueError("a trained regime needs training_window and seed")
        cuts = params.get("cuts")
        if set(params) != {"cuts", "min_history"} or not isinstance(cuts, str):
            raise ValueError("method params must be exactly cuts and min_history")
        return self.spec(
            _single_feature(spec),
            cuts=cuts.split(","),
            min_history=_positive_int(params, "min_history"),
            training_window=spec.training_window,
            seed=spec.seed,
            labels=spec.state_space,
            name=spec.name,
            version=spec.version,
        )

    def _value(
        self,
        spec: StateSpec,
        params: Mapping[str, MethodParam],
        at: datetime,
        own: Sequence[StateInput],
    ) -> StateValue:
        if not own or own[-1].value is None:
            return _none(at)
        current = own[-1]
        history = sorted(_number(item) for item in own if item.value is not None)
        if len(history) < _positive_int(params, "min_history"):
            return _none(at)
        n = len(history)
        thresholds = []
        with localcontext(_CONTEXT):
            for cut in _cuts(params.get("cuts")):
                k = max(1, int((cut * n).to_integral_value(rounding=ROUND_CEILING)))
                thresholds.append(history[min(k, n) - 1])
        x = _number(current)
        index = sum(1 for threshold in thresholds if threshold < x)
        return StateValue(
            evaluation_time=at,
            state=spec.state_space[index],
            inputs_used=n,
            latest_input_time=current.evaluation_time,
        )


class VolatilityRegimeProvider(_QuantileRegimeProvider):
    """Volatility regime from one realized-volatility feature (e.g. ``bar_realized_vol_<n>``)."""

    NAME = "volatility_regime"
    LABELS = VOLATILITY_LABELS


class LiquidityRegimeProvider(_QuantileRegimeProvider):
    """Liquidity regime from one volume feature (e.g. ``bar_volume_sum_<n>``)."""

    NAME = "liquidity_regime"
    LABELS = LIQUIDITY_LABELS


class TrendRangeProvider(_StateProviderBase):
    """Efficiency-ratio trend / range over the ``window`` latest log returns (see module docs)."""

    NAME = "trend_range"
    METHOD = "efficiency_ratio"

    @classmethod
    def spec(
        cls,
        feature: Ref,
        *,
        window: int,
        threshold: str | Decimal,
        labels: Sequence[str] = TREND_LABELS,
        name: str | None = None,
        version: str = "1.0.0",
    ) -> StateSpec:
        """A spec of this rule; ``window`` and ``threshold`` (in ``(0, 1]``) are spec parameters."""
        _positive_int({"window": window}, "window")
        level = _decimal_text(threshold)
        if not (Decimal(0) < level <= Decimal(1)):
            raise ValueError("threshold must be in (0, 1]")
        if len(tuple(labels)) != 3:
            raise ValueError("labels are (down, range, up)")
        return StateSpec(
            name=cls.NAME if name is None else name,
            version=version,
            features=(feature,),
            state_space=tuple(labels),
            method=state_method(cls.METHOD, {"threshold": str(level), "window": window}),
        )

    def _canonical(self, spec: StateSpec) -> StateSpec:
        params = self._params(spec)
        threshold = params.get("threshold")
        if set(params) != {"threshold", "window"} or not isinstance(threshold, str):
            raise ValueError("method params must be exactly threshold and window")
        return self.spec(
            _single_feature(spec),
            window=_positive_int(params, "window"),
            threshold=threshold,
            labels=spec.state_space,
            name=spec.name,
            version=spec.version,
        )

    def _value(
        self,
        spec: StateSpec,
        params: Mapping[str, MethodParam],
        at: datetime,
        own: Sequence[StateInput],
    ) -> StateValue:
        window = _positive_int(params, "window")
        run = own[len(own) - window :] if len(own) >= window else []
        if not run or any(item.value is None for item in run):
            return _none(at)
        returns = [_number(item) for item in run]
        down, flat, up = spec.state_space
        with localcontext(_CONTEXT):
            total = sum(returns, Decimal(0))
            path = sum((abs(r) for r in returns), Decimal(0))
            if path == 0:
                label = flat
            elif abs(total) / path >= _decimal_text(str(params["threshold"])):
                label = up if total > 0 else down
            else:
                label = flat
        return StateValue(
            evaluation_time=at,
            state=label,
            inputs_used=len(run),
            latest_input_time=run[-1].evaluation_time,
        )
