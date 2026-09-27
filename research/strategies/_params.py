"""Shared helpers for research strategies and risk policies (Phase 5; ADR-0038)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from core.contracts.strategy import UnsupportedStrategy
from core.domain.specs import StrategySpec

__all__ = ["decimal_param", "resolve_params", "same_value"]

type ParamValue = str | int | float | bool | Decimal


def same_value(left: object, right: object) -> bool:
    """Equality that does not confuse ``True`` with ``1`` (type and value both match)."""
    return type(left) is type(right) and left == right


def resolve_params(
    spec: StrategySpec, requested: Mapping[str, ParamValue]
) -> dict[str, ParamValue]:
    """The parameter point of one trial: spec defaults overridden by ``requested``.

    Every requested key must be declared in ``spec.param_search_space`` and every value must be one
    of the declared values — the declared space is what the trial count (Constitution C-T1) is
    computed from, so an undeclared point is refused (``UnsupportedStrategy``), never silently run.
    """
    resolved: dict[str, ParamValue] = dict(spec.params)
    for key, value in requested.items():
        space = spec.param_search_space.get(key)
        if space is None:
            raise UnsupportedStrategy(f"{spec.ref}: parameter {key!r} is not in the declared space")
        if not any(same_value(value, allowed) for allowed in space):
            raise UnsupportedStrategy(
                f"{spec.ref}: {key}={value!r} is not a declared point of {tuple(space)!r}"
            )
        resolved[key] = value
    return resolved


def decimal_param(params: Mapping[str, object], key: str) -> Decimal:
    """A numeric policy parameter stored as decimal text (``RiskPolicy.params`` holds no floats)."""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError(f"parameter {key!r} must be decimal text or int, got {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"parameter {key!r} is not a decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"parameter {key!r} must be finite")
    return parsed
