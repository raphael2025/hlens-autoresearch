"""P7 strategy-composition execution Providers (ADR-0082 3rd acceptance, ADR-0088, ADR-0100 #1).

Research code — never production (H5). Three thin ``CompositeStrategyProvider`` subclasses serve
exactly the ``StrategySpec`` values the pure P7 lowering (``research.hypotheses.
typed_plan_lowering``) emits for one composition type each. Their descriptor identity equals the
``provider`` value the lowering writes into the spec's ``params``, and those ``params`` (a fixed,
non-tunable declaration) are admitted only when they equal the lowering's declaration exactly:

- ``P7ConditionedStrategyProvider`` (``p7_conditioning_state_gate@1.0.0``,
  ``p7.conditioning.state_gate@1.0.0``): the base's target while the latest visible state label
  equals ``state_value``; flat when the state is another label, unknown (``None``) or missing;
- ``P7EnsembleStrategyProvider`` (``p7_ensemble_equal_weight_mean@1.0.0``,
  ``p7.ensemble.equal_weight_mean@1.0.0``): the equal-weight mean of the members' targets;
- ``P7NegatedStrategyProvider`` (``p7_negation_target_position@1.0.0``,
  ``p7.negation.target_position@1.0.0``): the base's target with the sign flipped; not a
  validation negative control; in a spot context a short target fails closed (gap ST-4).

The execution semantics are ``CompositeStrategyProvider``'s (see ``research.strategies.
composite``); nothing is re-implemented here. The referenced strategies are resolved through the
caller's explicit table; the P7 compiler (``research.hypotheses.typed_plan_compiler``) builds it
from the hash-verified plan resolution and the earlier nodes' Providers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from core.domain.specs import ConditionedStrategy, EnsembleStrategy, NegatedStrategy
from research.strategies.composite import CompositeStrategyProvider

__all__ = [
    "P7_STRATEGY_PROVIDERS",
    "P7ConditionedStrategyProvider",
    "P7EnsembleStrategyProvider",
    "P7NegatedStrategyProvider",
]

_CONDITIONED: Final[Mapping[str, object]] = {
    "definition": "p7.conditioning.state_gate@1.0.0",
    "operator": "conditioned",
    "provider": "p7_conditioning_state_gate@1.0.0",
    "semantic_version": "1.0.0",
    "unmatched_state": "flat",
    "unknown_state": "flat",
}
_ENSEMBLE: Final[Mapping[str, object]] = {
    "definition": "p7.ensemble.equal_weight_mean@1.0.0",
    "operator": "ensemble",
    "provider": "p7_ensemble_equal_weight_mean@1.0.0",
    "semantic_version": "1.0.0",
    "rule": "equal_weight_mean",
    "cost_basis": "net_combined_position_change",
}
_NEGATED: Final[Mapping[str, object]] = {
    "definition": "p7.negation.target_position@1.0.0",
    "operator": "negated",
    "provider": "p7_negation_target_position@1.0.0",
    "semantic_version": "1.0.0",
    "negates": "target_position",
    "cost_basis": "negated_trades",
    "validation_negative_control": False,
    "short_exposure": "provider_fail_closed_without_short_cost_model",
}


class _P7Composite(CompositeStrategyProvider):
    DEFINITION: ClassVar[str]

    @classmethod
    def plugin_key(cls) -> str:
        return f"{cls.NAME}@{cls.VERSION}"


class P7ConditionedStrategyProvider(_P7Composite):
    """``conditioned`` compositions of the P7 lowering (state gate; flat otherwise)."""

    NAME = "p7_conditioning_state_gate"
    VERSION = "1.0.0"
    DEFINITION = "p7.conditioning.state_gate@1.0.0"
    COMPOSITIONS = (ConditionedStrategy,)
    DECLARED_PARAMS = {ConditionedStrategy: _CONDITIONED}


class P7EnsembleStrategyProvider(_P7Composite):
    """``ensemble`` compositions of the P7 lowering (equal-weight mean of member targets)."""

    NAME = "p7_ensemble_equal_weight_mean"
    VERSION = "1.0.0"
    DEFINITION = "p7.ensemble.equal_weight_mean@1.0.0"
    COMPOSITIONS = (EnsembleStrategy,)
    DECLARED_PARAMS = {EnsembleStrategy: _ENSEMBLE}


class P7NegatedStrategyProvider(_P7Composite):
    """``negated`` compositions of the P7 lowering (negated target; spot short fails closed)."""

    NAME = "p7_negation_target_position"
    VERSION = "1.0.0"
    DEFINITION = "p7.negation.target_position@1.0.0"
    COMPOSITIONS = (NegatedStrategy,)
    DECLARED_PARAMS = {NegatedStrategy: _NEGATED}


#: Every P7 strategy Provider class, by the lowered definition it serves.
P7_STRATEGY_PROVIDERS: Final[Mapping[str, type[_P7Composite]]] = {
    cls.DEFINITION: cls
    for cls in (
        P7ConditionedStrategyProvider,
        P7EnsembleStrategyProvider,
        P7NegatedStrategyProvider,
    )
}
