"""Deterministic ``StrategyProvider`` / ``RiskProvider`` fakes for the Phase 5 -> Phase 13 wiring
tests (``apps.execution.strategy_source``). Not production code: minimal, sign-of-latest-signal /
fixed-cap rules, only precise enough to exercise the real ``core.contracts.strategy`` invariants
(``StrategyResult.check_answers`` / ``RiskResult.check_answers``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from core.contracts.strategy import (
    ConstrainedPosition,
    RiskProviderDescriptor,
    RiskRequest,
    RiskResult,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedRiskPolicy,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import RiskPolicy, StrategySpec

__all__ = [
    "SIGNAL_REF",
    "WEIGHT",
    "FakeCapRisk",
    "FakeSignStrategy",
    "RequestShiftingStrategy",
    "fake_cap_risk_policy",
    "fake_strategy_spec",
]

SIGNAL_REF = Ref(kind=Kind.FEATURE, name="fake_signal", version="1.0.0")
WEIGHT = Decimal("0.5")
_SPEC_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def fake_strategy_spec(*, name: str = "fake_sign", risk_policy: Ref | None = None) -> StrategySpec:
    return StrategySpec(
        name=name,
        version="1.0.0",
        created_at=_SPEC_TIME,
        signals=(SIGNAL_REF,),
        risk_policy=risk_policy,
    )


class FakeSignStrategy:
    """``weight = sign(latest visible signal) * WEIGHT / n_instruments``; flat if none visible."""

    def __init__(self, specs: Sequence[StrategySpec]) -> None:
        self._specs = {str(spec.ref): spec for spec in specs}
        self._descriptor = StrategyProviderDescriptor(
            name="fake_sign_strategy",
            version="1.0.0",
            deterministic=True,
            supported_strategies=FrozenMapping(
                {key: spec.content_hash() for key, spec in self._specs.items()}
            ),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        if not self._descriptor.supports(request.strategy, request.spec_hash):
            raise UnsupportedStrategy(f"{request.strategy} with this spec hash is not supported")
        slice_weight = WEIGHT / len(request.instruments)
        positions: list[TargetPosition] = []
        for decision_time in request.decision_times:
            visible = request.visible_at(decision_time)
            for instrument in request.instruments:
                own = sorted(
                    (item for item in visible if item.instrument == instrument),
                    key=lambda item: item.event_time,
                )
                if not own or not isinstance(own[-1].value, Decimal):
                    positions.append(
                        TargetPosition(
                            decision_time=decision_time,
                            instrument=instrument,
                            target_weight=Decimal(0),
                            inputs_used=0,
                        )
                    )
                    continue
                latest = own[-1]
                latest_value = latest.value
                assert isinstance(latest_value, Decimal)
                sign = 1 if latest_value > 0 else -1 if latest_value < 0 else 0
                positions.append(
                    TargetPosition(
                        decision_time=decision_time,
                        instrument=instrument,
                        target_weight=slice_weight * sign,
                        inputs_used=1,
                        latest_input_available_time=latest.available_time,
                    )
                )
        return StrategyResult.build(request, self._descriptor, positions)


class RequestShiftingStrategy:
    """Wraps a ``FakeSignStrategy`` but answers a subtly different request: its result therefore
    fails ``check_answers`` against the request actually asked (``request_hash`` mismatch) — used to
    prove the wiring adapter fails closed on a non-compliant answer."""

    def __init__(self, inner: FakeSignStrategy) -> None:
        self._inner = inner

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._inner.descriptor

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        shifted = request.model_copy(update={"params": FrozenMapping({"noise": "shifted"})})
        return self._inner.target_positions(shifted)


def fake_cap_risk_policy(*, cap: str = "0.1", name: str = "fake_cap") -> RiskPolicy:
    return RiskPolicy(
        name=name,
        version="1.0.0",
        created_at=_SPEC_TIME,
        rules=("cap",),
        params=FrozenMapping({"cap": cap}),
    )


class FakeCapRisk:
    """Caps ``|weight|`` at the policy's ``cap`` param; uses no signals (rule based)."""

    def __init__(self, policies: Sequence[RiskPolicy]) -> None:
        self._policies = {str(policy.ref): policy for policy in policies}
        self._descriptor = RiskProviderDescriptor(
            name="fake_cap_risk",
            version="1.0.0",
            deterministic=True,
            supported_policies=FrozenMapping(
                {key: policy.content_hash() for key, policy in self._policies.items()}
            ),
        )

    @property
    def descriptor(self) -> RiskProviderDescriptor:
        return self._descriptor

    def constrain(self, request: RiskRequest) -> RiskResult:
        if not self._descriptor.supports(request.policy, request.policy_hash):
            raise UnsupportedRiskPolicy(f"{request.policy} with this hash is not supported")
        policy = self._policies[str(request.policy)]
        cap = Decimal(str(policy.params["cap"]))
        positions = []
        for target in request.targets:
            requested = target.target_weight
            constrained = max(-cap, min(cap, requested))
            positions.append(
                ConstrainedPosition(
                    instrument=target.instrument,
                    requested_weight=requested,
                    constrained_weight=constrained,
                    binding_rules=("cap",) if constrained != requested else (),
                    inputs_used=0,
                )
            )
        return RiskResult.build(request, self._descriptor, positions)
