"""Explicit parameter points for the ADR-0085 research strategies. Research code (H5).

ADR-0085 rule 2: no numeric default lives in the library. A spec is built only from a complete,
explicit parameter point, every value of which is a declared point of the strategy's parameter
space (the space itself is part of the strategy's definition, ADR-0038; the chosen point is part of
the experiment's pre-registration, C-T1 / C-T2). A missing, extra or undeclared value refuses the
construction; a provider refuses a spec whose declared space, signals or point differ from the
module's declaration (so a hand-built spec cannot widen the space).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from core.contracts.strategy import UnsupportedStrategy
from core.domain.base import FrozenMapping, Ref
from core.domain.specs import StrategySpec
from research.strategies._params import same_value

__all__ = ["ParamSpace", "build_spec", "check_spec", "positive_int"]

type ParamSpace = Mapping[str, tuple[str | int | float | bool, ...]]

_VERSION: Final = "1.0.0"


def _check_point(name: str, space: ParamSpace, point: Mapping[str, object]) -> None:
    missing = sorted(set(space) - set(point))
    if missing:
        raise ValueError(f"{name}: parameter {missing[0]!r} must be given explicitly")
    extra = sorted(set(point) - set(space))
    if extra:
        raise ValueError(f"{name}: parameter {extra[0]!r} is not in the declared space")
    for key, value in point.items():
        if not any(same_value(value, allowed) for allowed in space[key]):
            raise ValueError(
                f"{name}: {key}={value!r} is not a declared point of {tuple(space[key])!r}"
            )


def build_spec(
    *,
    name: str,
    created_at: datetime,
    lineage: tuple[Ref, ...],
    signals: tuple[Ref, ...],
    space: ParamSpace,
    point: Mapping[str, str | int | float | bool],
) -> StrategySpec:
    """``strategy:<name>@1.0.0`` at the explicit, declared parameter ``point``."""
    _check_point(name, space, point)
    return StrategySpec(
        name=name,
        version=_VERSION,
        created_at=created_at,
        lineage=lineage,
        signals=signals,
        params=FrozenMapping(dict(point)),
        param_search_space=FrozenMapping(dict(space)),
    )


def check_spec(
    spec: StrategySpec, *, name: str, signals: tuple[Ref, ...], space: ParamSpace
) -> None:
    """Refuse a spec that is not ``<name>@1.0.0`` with exactly the declared signals and space."""
    if not isinstance(spec, StrategySpec):
        raise ValueError("a StrategySpec is required")
    if (spec.name, spec.version) != (name, _VERSION):
        raise ValueError(f"{spec.ref} is not {name}@{_VERSION}")
    if tuple(spec.signals) != signals:
        raise ValueError(f"{spec.ref}: signals must be exactly {tuple(map(str, signals))}")
    declared = {key: tuple(values) for key, values in space.items()}
    given = {key: tuple(values) for key, values in spec.param_search_space.items()}
    if set(declared) != set(given) or any(
        len(declared[key]) != len(given[key])
        or not all(same_value(a, b) for a, b in zip(declared[key], given[key], strict=True))
        for key in declared
    ):
        raise ValueError(f"{spec.ref}: param_search_space differs from the declared space")
    _check_point(name, space, spec.params)


def positive_int(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise UnsupportedStrategy(f"{key} must be a positive int")
    return value
