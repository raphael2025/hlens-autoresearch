"""Phase 10 framework: routing only among validated strategies, with switching costs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.domain.base import Kind, Ref
from core.lifecycle.strategy import LifecycleState
from research.router import RouterError, RouterSpec, StrategyRouter

A = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")
B = Ref(kind=Kind.STRATEGY, name="meanrev", version="1.0.0")
T0 = datetime(2024, 1, 1, tzinfo=UTC)
LIFECYCLE = {A: LifecycleState.ACTIVE, B: LifecycleState.PRODUCTION_CANDIDATE}


def _spec(**overrides: object) -> RouterSpec:
    fields: dict[str, object] = {
        "name": "vol_router",
        "version": "1.0.0",
        "table": {"high": {str(A): Decimal("0.6")}, "low": {str(B): Decimal("0.5")}},
        "fallback": {},
        "switching_cost_rate": Decimal("0.001"),
    }
    fields.update(overrides)
    return RouterSpec(**fields)  # type: ignore[arg-type]


def test_routing_follows_the_known_state_and_charges_turnover() -> None:
    router = StrategyRouter(_spec(), LIFECYCLE)
    decisions = router.route(
        [
            (T0, "high"),
            (T0 + timedelta(minutes=1), "high"),
            (T0 + timedelta(minutes=2), "low"),
            (T0 + timedelta(minutes=3), None),
        ]
    )
    assert [d.weights for d in decisions] == [
        {str(A): Decimal("0.6")},
        {str(A): Decimal("0.6")},
        {str(B): Decimal("0.5")},
        {},
    ]
    assert [d.turnover for d in decisions] == [
        Decimal("0.6"),
        Decimal(0),
        Decimal("1.1"),
        Decimal("0.5"),
    ]
    assert decisions[2].switching_cost == Decimal("0.0011")


def test_an_unvalidated_strategy_cannot_be_routed() -> None:
    with pytest.raises(RouterError, match="unvalidated"):
        StrategyRouter(_spec(), {A: LifecycleState.ACTIVE, B: LifecycleState.VALIDATION})


def test_leverage_is_refused() -> None:
    with pytest.raises(RouterError, match="at most 1"):
        StrategyRouter(
            _spec(table={"high": {str(A): Decimal("0.8"), str(B): Decimal("0.3")}}), LIFECYCLE
        )
