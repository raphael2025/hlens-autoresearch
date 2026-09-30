"""Phase 10 framework: routing only among validated strategies, with switching costs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from core.domain.base import Kind, Ref
from core.lifecycle.strategy import LifecycleState
from research.router import RouterError, RouterSpec, StrategyRouter
from research.router.router import normalize_report_hashes

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


def test_a_negative_weight_is_refused() -> None:
    with pytest.raises(RouterError, match="non-negative"):
        StrategyRouter(_spec(table={"high": {str(A): Decimal("-0.1")}}), LIFECYCLE)


@pytest.mark.parametrize("weight", [Decimal("NaN"), Decimal("Infinity")])
def test_a_non_finite_weight_is_refused(weight: Decimal) -> None:
    with pytest.raises(RouterError, match="finite"):
        StrategyRouter(_spec(table={"high": {str(A): weight}}), LIFECYCLE)


@pytest.mark.parametrize("rate", [Decimal("-0.001"), Decimal("NaN"), Decimal("Infinity")])
def test_switching_cost_rate_must_be_finite_and_non_negative(rate: Decimal) -> None:
    with pytest.raises(RouterError, match="switching_cost_rate"):
        StrategyRouter(_spec(switching_cost_rate=rate), LIFECYCLE)


def test_route_requires_strictly_increasing_times() -> None:
    router = StrategyRouter(_spec(), LIFECYCLE)
    with pytest.raises(RouterError, match="strictly increasing"):
        router.route([(T0, "high"), (T0, "low")])
    with pytest.raises(RouterError, match="strictly increasing"):
        router.route([(T0 + timedelta(minutes=1), "high"), (T0, "low")])


def test_normalize_report_hashes_accepts_well_formed_input() -> None:
    sha = "a" * 64
    assert normalize_report_hashes({str(A): sha}) == {str(A): sha}


def test_normalize_report_hashes_rejects_a_non_sha256_hash() -> None:
    with pytest.raises(RouterError, match="sha256"):
        normalize_report_hashes({str(A): "not-a-hash"})


def test_normalize_report_hashes_rejects_a_duplicate_ref_given_twice() -> None:
    """``Ref`` and its string form for the same strategy must not silently double up."""
    sha = "a" * 64
    with pytest.raises(RouterError, match="given twice"):
        normalize_report_hashes(cast(Any, {A: sha, str(A): "b" * 64}))


def test_normalize_report_hashes_rejects_a_non_mapping() -> None:
    with pytest.raises(RouterError, match="map"):
        normalize_report_hashes([(str(A), "a" * 64)])  # type: ignore[arg-type]
