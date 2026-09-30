"""Two-feature rule states over volatility / return features (ADR-0085; Phase 2).

Two deterministic providers, each a plugin behind the ``StateProvider`` Protocol
(``core.contracts.state``), following the style of ``plugins/states/regimes.py``: they read only
the request's visible set (``StateRequest.visible_at``, which the runner also enforces
structurally), use exact ``Decimal`` arithmetic and never emit floats. Every parameter is part of
the ``StateSpec`` a provider serves (encoded in ``StateSpec.method`` by
``core.contracts.state.state_method``), bound by the spec hash; none has a default (ADR-0085
§ 通用规则 2).

Unlike the single-feature quantile / efficiency-ratio models in ``regimes.py``, both states here
compare the **latest visible value of two declared features** — no ``training_window`` or ``seed``
(rule based, like ``TrendRangeProvider``). ``StateSpec.features`` fixes the order: the first
feature is the "numerator" role (the short-window volatility feature for
``volatility_squeeze``, the return feature for ``return_shock``), the second is the
"denominator" role (the long-window volatility feature / the volatility feature respectively).

- ``VolatilitySqueezeProvider`` (``vol_ratio_bands``, ``MST-SQUEEZE-001`` / ``MST-EXPANSION-001``):
  ``ratio = short_vol / long_vol`` compared against the explicit ``squeeze_below`` /
  ``expansion_above`` cut points (``squeeze_below < expansion_above``, both required):
  ``ratio < squeeze_below`` → ``squeeze``; ``ratio > expansion_above`` → ``expansion``; otherwise
  ``normal``. ``long_vol == 0`` is a division by zero: treated as not computable (``None``), never
  raised and never extrapolated (ADR-0085 § 通用规则 4).
- ``ReturnShockProvider`` (``abs_return_vol_multiple``, ``MST-SHOCK-001``):
  ``|bar_log_return| > k * bar_realized_vol_<window>`` → ``shock``, otherwise ``calm``; ``k`` is
  the explicit multiplier. No division is involved, so there is no zero-denominator case to guard.

Not computable (``None``): either feature has no visible value, or its latest visible value is
``None``, or (``volatility_squeeze`` only) the long-window value is exactly zero. Nothing is
filled. "Latest visible value" follows the same convention as ``regimes.py``: the most recent
input with ``evaluation_time <= t`` for that feature — not necessarily time-aligned with ``t``.

This module is self-contained (it does not import the private helpers of ``regimes.py``) but
mirrors its structure and its exact-``Decimal`` conventions one for one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
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
    "SHOCK_LABELS",
    "SQUEEZE_LABELS",
    "ReturnShockProvider",
    "VolatilitySqueezeProvider",
]

SQUEEZE_LABELS: Final = ("squeeze", "normal", "expansion")
SHOCK_LABELS: Final = ("calm", "shock")

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)


def _number(item: StateInput) -> Decimal:
    value = item.value
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise StateInputError(f"{item.feature} @ {item.evaluation_time.isoformat()} is not numeric")
    return Decimal(value)


def _decimal_text(value: str | Decimal) -> Decimal:
    if isinstance(value, bool):
        # Decimal(True) is Decimal(1): a boolean must not silently become a threshold.
        raise ValueError("state parameter must be a decimal, not a boolean")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{value!r} is not a decimal number") from None
    if not parsed.is_finite():
        raise ValueError(f"{value!r} is not finite")
    return parsed


def _none(at: datetime) -> StateValue:
    return StateValue(evaluation_time=at, state=None, inputs_used=0)


def _two_features(spec: StateSpec) -> tuple[Ref, Ref]:
    if len(spec.features) != 2:
        raise ValueError(f"{spec.ref} must have exactly two feature inputs")
    first, second = spec.features
    if first == second:
        raise ValueError(f"{spec.ref} needs two distinct feature inputs")
    return first, second


def _split_latest(
    visible: Sequence[StateInput], feature_a: Ref, feature_b: Ref
) -> tuple[StateInput | None, StateInput | None]:
    """The latest (largest ``evaluation_time``) visible input for each of the two features.

    ``visible`` is in canonical ``(evaluation_time, feature)`` order (``StateRequest`` /
    ``visible_at``), so for a fixed feature the matching items already appear in ascending time
    order; keeping the last match per feature while scanning once gives the latest one. Any input
    belonging to neither declared feature is a contract violation: fail closed.
    """
    latest_a: StateInput | None = None
    latest_b: StateInput | None = None
    stray: set[str] = set()
    for item in visible:
        if item.feature == feature_a:
            latest_a = item
        elif item.feature == feature_b:
            latest_b = item
        else:
            stray.add(str(item.feature))
    if stray:
        raise StateInputError(f"inputs outside the spec's features: {sorted(stray)}")
    return latest_a, latest_b


class _TwoFeatureStateProviderBase:
    """Shared plumbing: declared specs, descriptor, per-time split into the two features."""

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
        feature_a, feature_b = spec.features
        values = []
        for at in request.evaluation_times:
            visible = request.visible_at(at, spec.training_window)
            item_a, item_b = _split_latest(visible, feature_a, feature_b)
            values.append(self._value(spec, params, at, item_a, item_b))
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
        item_a: StateInput | None,
        item_b: StateInput | None,
    ) -> StateValue:
        raise NotImplementedError


class VolatilitySqueezeProvider(_TwoFeatureStateProviderBase):
    """Volatility ratio bands: ``short_vol / long_vol`` vs. explicit cut points (see module docs).

    ``MST-SQUEEZE-001`` / ``MST-EXPANSION-001``. Features are ``(short_vol, long_vol)``, in that
    order; ``long_vol == 0`` is not computable (division by zero → missing, ADR-0085 § 通用规则 4).
    """

    NAME = "volatility_squeeze"
    METHOD = "vol_ratio_bands"

    @classmethod
    def spec(
        cls,
        short_vol_feature: Ref,
        long_vol_feature: Ref,
        *,
        squeeze_below: str | Decimal,
        expansion_above: str | Decimal,
        labels: Sequence[str] = SQUEEZE_LABELS,
        name: str | None = None,
        version: str = "1.0.0",
    ) -> StateSpec:
        """A spec of this rule; ``squeeze_below`` / ``expansion_above`` are spec parameters."""
        below = _decimal_text(squeeze_below)
        above = _decimal_text(expansion_above)
        if below <= 0:
            raise ValueError("squeeze_below must be a positive decimal (a volatility ratio)")
        if below >= above:
            raise ValueError("squeeze_below must be strictly less than expansion_above")
        state_space = tuple(labels)
        if len(state_space) != 3:
            raise ValueError("labels are (squeeze, normal, expansion)")
        if short_vol_feature == long_vol_feature:
            raise ValueError("volatility_squeeze needs two distinct feature inputs (short, long)")
        return StateSpec(
            name=cls.NAME if name is None else name,
            version=version,
            features=(short_vol_feature, long_vol_feature),
            state_space=state_space,
            method=state_method(
                cls.METHOD, {"squeeze_below": str(below), "expansion_above": str(above)}
            ),
        )

    def _canonical(self, spec: StateSpec) -> StateSpec:
        params = self._params(spec)
        squeeze_below = params.get("squeeze_below")
        expansion_above = params.get("expansion_above")
        if (
            set(params) != {"squeeze_below", "expansion_above"}
            or not isinstance(squeeze_below, str)
            or not isinstance(expansion_above, str)
        ):
            raise ValueError("method params must be exactly squeeze_below and expansion_above")
        short_vol_feature, long_vol_feature = _two_features(spec)
        return self.spec(
            short_vol_feature,
            long_vol_feature,
            squeeze_below=squeeze_below,
            expansion_above=expansion_above,
            labels=spec.state_space,
            name=spec.name,
            version=spec.version,
        )

    def _value(
        self,
        spec: StateSpec,
        params: Mapping[str, MethodParam],
        at: datetime,
        item_a: StateInput | None,
        item_b: StateInput | None,
    ) -> StateValue:
        if item_a is None or item_a.value is None or item_b is None or item_b.value is None:
            return _none(at)
        long_value = _number(item_b)
        if long_value == 0:
            return _none(at)
        short_value = _number(item_a)
        squeeze, normal, expansion = spec.state_space
        with localcontext(_CONTEXT):
            ratio = short_value / long_value
        below_text = params["squeeze_below"]
        above_text = params["expansion_above"]
        if not isinstance(below_text, str) or not isinstance(above_text, str):
            raise ValueError("method params must contain canonical decimal strings")
        below = _decimal_text(below_text)
        above = _decimal_text(above_text)
        if ratio < below:
            label = squeeze
        elif ratio > above:
            label = expansion
        else:
            label = normal
        latest = max(item_a.evaluation_time, item_b.evaluation_time)
        return StateValue(evaluation_time=at, state=label, inputs_used=2, latest_input_time=latest)


class ReturnShockProvider(_TwoFeatureStateProviderBase):
    """``|return| > k * vol`` (see module docs).

    ``MST-SHOCK-001``. Features are ``(return, vol)``, in that order; ``k`` is the explicit
    multiplier. No division: unlike ``volatility_squeeze`` there is no zero-denominator case.
    """

    NAME = "return_shock"
    METHOD = "abs_return_vol_multiple"

    @classmethod
    def spec(
        cls,
        return_feature: Ref,
        vol_feature: Ref,
        *,
        k: str | Decimal,
        labels: Sequence[str] = SHOCK_LABELS,
        name: str | None = None,
        version: str = "1.0.0",
    ) -> StateSpec:
        """A spec of this rule; ``k`` (the volatility multiplier) is a spec parameter."""
        multiple = _decimal_text(k)
        if multiple <= 0:
            raise ValueError("k must be a positive decimal (a volatility multiple)")
        state_space = tuple(labels)
        if len(state_space) != 2:
            raise ValueError("labels are (calm, shock)")
        if return_feature == vol_feature:
            raise ValueError("return_shock needs two distinct feature inputs (return, vol)")
        return StateSpec(
            name=cls.NAME if name is None else name,
            version=version,
            features=(return_feature, vol_feature),
            state_space=state_space,
            method=state_method(cls.METHOD, {"k": str(multiple)}),
        )

    def _canonical(self, spec: StateSpec) -> StateSpec:
        params = self._params(spec)
        k = params.get("k")
        if set(params) != {"k"} or not isinstance(k, str):
            raise ValueError("method params must be exactly k")
        return_feature, vol_feature = _two_features(spec)
        return self.spec(
            return_feature,
            vol_feature,
            k=k,
            labels=spec.state_space,
            name=spec.name,
            version=spec.version,
        )

    def _value(
        self,
        spec: StateSpec,
        params: Mapping[str, MethodParam],
        at: datetime,
        item_a: StateInput | None,
        item_b: StateInput | None,
    ) -> StateValue:
        if item_a is None or item_a.value is None or item_b is None or item_b.value is None:
            return _none(at)
        calm, shock = spec.state_space
        multiple_text = params["k"]
        if not isinstance(multiple_text, str):
            raise ValueError("method params must contain a canonical decimal string")
        multiple = _decimal_text(multiple_text)
        with localcontext(_CONTEXT):
            threshold = multiple * _number(item_b)
            triggered = abs(_number(item_a)) > threshold
        label = shock if triggered else calm
        latest = max(item_a.evaluation_time, item_b.evaluation_time)
        return StateValue(evaluation_time=at, state=label, inputs_used=2, latest_input_time=latest)
