"""Cost model v1 applied to per-trade returns (ADR-0037 §4; Constitution C-R4, A6).

Every net figure in the gates goes through these functions; there is no cost-free path (roadmap
Phase 4: skipping the cost model is forbidden). The cost parameters come from the bound
``CostModelSpec``; the stress multipliers come from the Profile (``cost_stress``).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from core.contracts.cost_model import CostModelSpec
from core.contracts.validation_profile import ValidationProfile

__all__ = [
    "breakeven_cost_multiple",
    "cost_model_is_bound",
    "multiplier",
    "net_returns",
]


def multiplier(value: float) -> Decimal:
    """A Profile multiplier (float on the wire) as an exact ``Decimal`` of its shortest repr."""
    return Decimal(repr(value))


def net_returns(
    gross: Sequence[Decimal], cost_model: CostModelSpec, stress: Decimal = Decimal(1)
) -> tuple[Decimal, ...]:
    """Per-trade returns after one round trip of cost (times ``stress``)."""
    cost = cost_model.round_trip_cost(stress)
    return tuple(value - cost for value in gross)


def breakeven_cost_multiple(gross_mean: Decimal, cost_model: CostModelSpec) -> Decimal:
    """How many times the modelled round-trip cost the mean gross edge covers (may be negative)."""
    return gross_mean / cost_model.round_trip_cost()


def cost_model_is_bound(cost_model: CostModelSpec, profile: ValidationProfile) -> bool:
    """The Profile's ``cost_stress.cost_model`` names exactly this cost model."""
    return profile.cost_stress.cost_model.target_identity() == cost_model.ref.target_identity()
