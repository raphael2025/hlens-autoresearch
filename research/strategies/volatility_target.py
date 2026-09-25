"""Volatility-targeted position scaling as a ``RiskProvider`` (Phase 5; ADR-0038). Research code.

Source: KnowledgeItem ``risk_volatility_managed_portfolios@1.0.0`` (Moreira & Muir 2017) and its
out-of-sample critique ``risk_volatility_managed_portfolios_out_of_sample@1.0.0``; both refs are the
``RiskPolicy.lineage``. They are claims to test, not evidence.

Policy ``risk:vol_target_bars@1.0.0``; its ``rules`` are the machine names reported in
``ConstrainedPosition.binding_rules``, applied in this order per instrument:

1. ``missing_volatility_flat`` — no visible, positive volatility estimate for the instrument →
   the position is flat (never sized on a guess);
2. ``volatility_scaling`` — ``weight × target_volatility / volatility``;
3. ``leverage_cap`` — the scale factor is capped at ``max_leverage``;
4. ``position_cap`` — ``|weight| <= max_abs_weight``;
5. ``gross_exposure_cap`` — if ``sum |weight| > max_gross_exposure``, every weight is scaled down
   pro rata (rounded toward zero, so the cap holds exactly).

The volatility estimate is the latest visible (``available_time <= decision_time``, by
``event_time``) observation of the policy's ``volatility_signal``. All policy parameters are decimal
text in ``RiskPolicy.params``; ``VOL_TARGET_PARAM_SPACE`` declares the values a study may try
(``RiskPolicy`` itself has no parameter-space field). These are policy parameters, not validation
thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from core.contracts.strategy import (
    ConstrainedPosition,
    RiskInputError,
    RiskProviderDescriptor,
    RiskRequest,
    RiskResult,
    SignalObservation,
    TargetPosition,
    UnsupportedRiskPolicy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import RiskPolicy
from research.strategies._params import decimal_param

__all__ = [
    "VOL_TARGET_KNOWLEDGE",
    "VOL_TARGET_PARAM_SPACE",
    "VOL_TARGET_POLICY_REF",
    "VOL_TARGET_RULES",
    "VolatilityTargetRiskProvider",
    "vol_target_policy",
]

VOL_TARGET_POLICY_REF: Final = Ref(kind=Kind.RISK, name="vol_target_bars", version="1.0.0")
VOL_TARGET_KNOWLEDGE: Final = (
    Ref(kind=Kind.KNOWLEDGE, name="risk_volatility_managed_portfolios", version="1.0.0"),
    Ref(
        kind=Kind.KNOWLEDGE,
        name="risk_volatility_managed_portfolios_out_of_sample",
        version="1.0.0",
    ),
)
VOL_TARGET_RULES: Final = (
    "missing_volatility_flat",
    "volatility_scaling",
    "leverage_cap",
    "position_cap",
    "gross_exposure_cap",
)
#: Declared policy parameter space. ``target_volatility`` is in the units of the volatility signal
#: (``bar_realized_vol_60`` = sqrt of the sum of 60 squared one-bar log returns).
VOL_TARGET_PARAM_SPACE: Final[dict[str, tuple[str, ...]]] = {
    "target_volatility": ("0.0025", "0.005", "0.01"),
    "max_leverage": ("1", "2"),
}
_DEFAULTS: Final[dict[str, str | int | float | bool]] = {
    "volatility_signal": "feature:bar_realized_vol_60@1.0.0",
    "target_volatility": "0.005",
    "max_leverage": "2",
    "max_abs_weight": "1",
    "max_gross_exposure": "1",
}
_SPEC_TIME: Final = datetime(2026, 9, 25, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_QUANTUM: Final = Decimal("1e-18")


def vol_target_policy(**overrides: str) -> RiskPolicy:
    """``risk:vol_target_bars@1.0.0`` with its default (or overridden) parameters."""
    params = dict(_DEFAULTS)
    for key, value in overrides.items():
        if key not in _DEFAULTS:
            raise ValueError(f"unknown vol_target_bars parameter {key!r}")
        params[key] = value
    return RiskPolicy(
        name=VOL_TARGET_POLICY_REF.name,
        version=VOL_TARGET_POLICY_REF.version,
        created_at=_SPEC_TIME,
        lineage=VOL_TARGET_KNOWLEDGE,
        rules=VOL_TARGET_RULES,
        params=FrozenMapping(params),
    )


class _Parsed:
    __slots__ = ("gross_cap", "max_abs", "max_leverage", "signal", "target_vol")

    def __init__(self, policy: RiskPolicy) -> None:
        signal = policy.params.get("volatility_signal")
        if not isinstance(signal, str):
            raise ValueError("volatility_signal must be a feature ref string")
        self.signal = Ref.parse(signal)
        if self.signal.kind is not Kind.FEATURE:
            raise ValueError("volatility_signal must reference a feature")
        self.target_vol = decimal_param(policy.params, "target_volatility")
        self.max_leverage = decimal_param(policy.params, "max_leverage")
        self.max_abs = decimal_param(policy.params, "max_abs_weight")
        self.gross_cap = decimal_param(policy.params, "max_gross_exposure")
        if min(self.target_vol, self.max_leverage, self.max_abs, self.gross_cap) <= 0:
            raise ValueError("vol_target_bars parameters must be positive")


def _latest_vol(
    visible: Sequence[SignalObservation], signal: Ref, instrument: str
) -> SignalObservation | None:
    series = [item for item in visible if item.signal == signal and item.instrument == instrument]
    if not series:
        return None
    latest = max(series, key=lambda item: (item.event_time, item.available_time))
    if latest.value is not None and not isinstance(latest.value, Decimal):
        raise RiskInputError(f"{signal} values must be Decimal or None")
    return latest


class VolatilityTargetRiskProvider:
    """``RiskProvider`` for ``vol_target_bars``; deterministic, ``Decimal`` only."""

    def __init__(self, policies: Sequence[RiskPolicy] | None = None) -> None:
        chosen = tuple(policies) if policies is not None else (vol_target_policy(),)
        self._policies = {str(policy.ref): (policy, _Parsed(policy)) for policy in chosen}
        self._descriptor = RiskProviderDescriptor(
            name="research_vol_target",
            version="0.1.0",
            deterministic=True,
            supported_policies=FrozenMapping(
                {key: policy.content_hash() for key, (policy, _) in self._policies.items()}
            ),
        )

    @property
    def descriptor(self) -> RiskProviderDescriptor:
        return self._descriptor

    def constrain(self, request: RiskRequest) -> RiskResult:
        if not isinstance(request, RiskRequest):
            raise RiskInputError("constrain needs a RiskRequest")
        if not self._descriptor.supports(request.policy, request.policy_hash):
            raise UnsupportedRiskPolicy(f"{request.policy} with this hash is not supported")
        _, parsed = self._policies[str(request.policy)]
        visible = request.visible()
        with localcontext(_CONTEXT):
            staged = [self._scale(target, visible, parsed) for target in request.targets]
            gross = sum((abs(weight) for _, weight, _, _ in staged), Decimal(0))
            positions: list[ConstrainedPosition] = []
            for target, weight, rules, used in staged:
                if gross > parsed.gross_cap and weight != 0:
                    weight = (weight * parsed.gross_cap / gross).quantize(
                        _QUANTUM, rounding=ROUND_DOWN
                    )
                    rules = rules | {"gross_exposure_cap"}
                if weight == target.target_weight:
                    rules = set()
                positions.append(
                    ConstrainedPosition(
                        instrument=target.instrument,
                        requested_weight=target.target_weight,
                        constrained_weight=weight,
                        binding_rules=tuple(sorted(rules)),
                        inputs_used=1 if used is not None else 0,
                        latest_input_available_time=used.available_time if used else None,
                    )
                )
        return RiskResult.build(request, self._descriptor, positions)

    @staticmethod
    def _scale(
        target: TargetPosition, visible: Sequence[SignalObservation], parsed: _Parsed
    ) -> tuple[TargetPosition, Decimal, set[str], SignalObservation | None]:
        requested = target.target_weight
        rules: set[str] = set()
        estimate = _latest_vol(visible, parsed.signal, target.instrument)
        vol = estimate.value if estimate is not None else None
        if not isinstance(vol, Decimal) or vol <= 0:
            if requested != 0:
                rules.add("missing_volatility_flat")
            return target, Decimal(0), rules, None
        scale = parsed.target_vol / vol
        if scale > parsed.max_leverage:
            scale = parsed.max_leverage
            rules.add("leverage_cap")
        weight = (requested * scale).quantize(_QUANTUM)
        if weight != requested:
            rules.add("volatility_scaling")
        if abs(weight) > parsed.max_abs:
            weight = parsed.max_abs if weight > 0 else -parsed.max_abs
            rules.add("position_cap")
        return target, weight, rules, estimate
