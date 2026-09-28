"""Drawdown control as a ``RiskProvider`` (ADR-0085 ``RSK-DD-CONTROL-001``; ADR-0088 decision 3).

Research code — never production (H5). Source: the risk library entry ``RSK-DD-CONTROL-001``
(Grossman & Zhou 1993, drawdown-constrained allocation; ``hlens-knowledge``, read only). The project
knowledge base holds no seed ``KnowledgeItem`` for it, so the policy's ``lineage`` is empty and it
is not a library entry (as ``zscore_reversion``). It is a rule to test, not evidence.

Policy ``risk:drawdown_control@1.0.0``; its single rule is the machine name reported in
``ConstrainedPosition.binding_rules``:

- ``drawdown_scaling`` — with ``drawdown = 1 − equity / peak_equity`` (``PortfolioState``), when
  ``drawdown > max_drawdown`` (strictly) every target weight is multiplied by ``reduced_fraction``
  (rounded toward zero to 18 places, so ``|constrained| <= reduced_fraction × |requested|``);
  otherwise every target passes unchanged.

Path dependence (ADR-0088 decision 3): ``peak_equity`` is the peak of the **realized** equity path
up to ``as_of`` and is supplied by the backtest / execution layer (``plugins.backtest`` builds it
from the realized equity curve); this provider keeps no memory of past equity. When
``PortfolioState.equity`` or ``PortfolioState.peak_equity`` is missing the request is refused with
``RiskInputError`` (fail closed, ADR-0038 / ADR-0085) — never treated as "no drawdown".

Parameters (ADR-0085 rule 2: no default): ``max_drawdown`` in ``(0, 1)`` and ``reduced_fraction``
in ``[0, 1)``, decimal text in ``RiskPolicy.params``, both given explicitly to
``drawdown_control_policy``. They are risk-policy parameters, not validation thresholds. No
parameter space is declared here: whether risk parameter points are enforced / counted as trials is
the open gap R-1 of ``docs/research/risk-library.md``.
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
    UnsupportedRiskPolicy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import RiskPolicy
from research.strategies._params import decimal_param

__all__ = [
    "DRAWDOWN_PARAMS",
    "DRAWDOWN_POLICY_REF",
    "DRAWDOWN_RULES",
    "DrawdownControlRiskProvider",
    "drawdown",
    "drawdown_control_policy",
]

DRAWDOWN_POLICY_REF: Final = Ref(kind=Kind.RISK, name="drawdown_control", version="1.0.0")
DRAWDOWN_RULES: Final = ("drawdown_scaling",)
#: The policy's parameter names; every one must be given explicitly (no default).
DRAWDOWN_PARAMS: Final = ("max_drawdown", "reduced_fraction")
_SPEC_TIME: Final = datetime(2026, 9, 28, tzinfo=UTC)
_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_QUANTUM: Final = Decimal("1e-18")


def drawdown_control_policy(*, max_drawdown: str, reduced_fraction: str) -> RiskPolicy:
    """``risk:drawdown_control@1.0.0`` at an explicit parameter point (decimal text)."""
    policy = RiskPolicy(
        name=DRAWDOWN_POLICY_REF.name,
        version=DRAWDOWN_POLICY_REF.version,
        created_at=_SPEC_TIME,
        rules=DRAWDOWN_RULES,
        params=FrozenMapping({"max_drawdown": max_drawdown, "reduced_fraction": reduced_fraction}),
    )
    _Parsed(policy)  # refuse an out-of-range point at construction
    return policy


class _Parsed:
    __slots__ = ("max_drawdown", "reduced_fraction")

    def __init__(self, policy: RiskPolicy) -> None:
        if not isinstance(policy, RiskPolicy):
            raise ValueError("a RiskPolicy is required")
        if (policy.name, policy.version) != (DRAWDOWN_POLICY_REF.name, DRAWDOWN_POLICY_REF.version):
            raise ValueError(f"{policy.ref} is not {DRAWDOWN_POLICY_REF}")
        if tuple(policy.rules) != DRAWDOWN_RULES:
            raise ValueError(f"{policy.ref}: rules must be exactly {DRAWDOWN_RULES}")
        missing = sorted(set(DRAWDOWN_PARAMS) - set(policy.params))
        if missing:
            raise ValueError(f"{policy.ref}: parameter {missing[0]!r} must be given explicitly")
        extra = sorted(set(policy.params) - set(DRAWDOWN_PARAMS))
        if extra:
            raise ValueError(f"{policy.ref}: unknown parameter {extra[0]!r}")
        self.max_drawdown = decimal_param(policy.params, "max_drawdown")
        self.reduced_fraction = decimal_param(policy.params, "reduced_fraction")
        if not Decimal(0) < self.max_drawdown < Decimal(1):
            raise ValueError("max_drawdown must be in (0, 1)")
        if not Decimal(0) <= self.reduced_fraction < Decimal(1):
            raise ValueError("reduced_fraction must be in [0, 1)")


def drawdown(equity: Decimal, peak_equity: Decimal) -> Decimal:
    """``1 − equity / peak_equity`` at 50 significant digits (both positive, contract-checked)."""
    with localcontext(_CONTEXT):
        return Decimal(1) - equity / peak_equity


class DrawdownControlRiskProvider:
    """``RiskProvider`` for ``drawdown_control@1.0.0``; deterministic, ``Decimal`` only.

    ``policies`` are explicit (no default policy); one policy per ``name@version``.
    """

    def __init__(self, policies: Sequence[RiskPolicy]) -> None:
        chosen = tuple(policies)
        if not chosen:
            raise ValueError("at least one explicit drawdown_control policy is required")
        self._policies: dict[str, tuple[RiskPolicy, _Parsed]] = {}
        for policy in chosen:
            key = str(policy.ref)
            if key in self._policies:
                raise ValueError(f"{key} is given more than once")
            self._policies[key] = (policy, _Parsed(policy))
        self._descriptor = RiskProviderDescriptor(
            name="research_drawdown_control",
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
        portfolio = request.portfolio
        if portfolio.equity is None:
            raise RiskInputError(
                "drawdown_control needs PortfolioState.equity (path-dependent rule, fail closed)"
            )
        if portfolio.peak_equity is None:
            raise RiskInputError(
                "drawdown_control needs PortfolioState.peak_equity from the realized equity path "
                "(ADR-0088 decision 3, fail closed)"
            )
        breached = drawdown(portfolio.equity, portfolio.peak_equity) > parsed.max_drawdown
        positions: list[ConstrainedPosition] = []
        with localcontext(_CONTEXT):
            for target in request.targets:
                requested = target.target_weight
                weight = requested
                if breached:
                    weight = (requested * parsed.reduced_fraction).quantize(
                        _QUANTUM, rounding=ROUND_DOWN
                    )
                    if weight == 0:
                        weight = Decimal(0)  # never emit a signed zero
                positions.append(
                    ConstrainedPosition(
                        instrument=target.instrument,
                        requested_weight=requested,
                        constrained_weight=weight,
                        binding_rules=("drawdown_scaling",) if weight != requested else (),
                        inputs_used=0,
                    )
                )
        return RiskResult.build(request, self._descriptor, positions)
