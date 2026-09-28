"""Which strategies are cross-sectional (ADR-0059, Accepted 2026-09-26). Research code (H5).

A cross-sectional strategy ranks instruments against each other (e.g. ``xsmom_bars``): on a single
instrument it is flat by definition, so the per-asset C-R3 re-runs of G4 carry no information
about it. The G4 input builder (``research.strategies.validation``) therefore re-runs such a
strategy on disjoint sub-universes of its declared instruments
(``research.validation.robustness.subuniverse_partition``) and the cross-asset check judges those.

The decision is a **declaration**, made here and nowhere else: a static set of
``StrategySpec.name`` values, reviewed like code. It is never inferred from results (flat
single-asset runs, zero returns, a trade pattern, ...): ``is_cross_sectional`` reads only the
spec's name. A strategy whose rule stops being cross-sectional must be given a new name. No
contract or Schema change: ``StrategySpec`` is untouched.
"""

from __future__ import annotations

from typing import Final

from core.domain.specs import StrategySpec
from research.strategies.cross_sectional_momentum import XSMOM_NAME
from research.strategies.dual_momentum import DUAL_MOMENTUM_NAME

__all__ = ["CROSS_SECTIONAL_STRATEGIES", "is_cross_sectional"]

#: ``StrategySpec.name`` of every strategy declared cross-sectional.
CROSS_SECTIONAL_STRATEGIES: Final[frozenset[str]] = frozenset({XSMOM_NAME, DUAL_MOMENTUM_NAME})


def is_cross_sectional(spec: StrategySpec) -> bool:
    """Whether ``spec`` is declared cross-sectional (by its name only; see module docs)."""
    return spec.name in CROSS_SECTIONAL_STRATEGIES
