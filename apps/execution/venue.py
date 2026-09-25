"""The only execution venue in this build: an in-process simulation (ADR-0046).

``SimulatedVenue`` has no network, no account, no credential. It fills every order it receives in
full, at the supplied price adjusted by an injected cost model, and keeps every order and fill as an
append-only, content-addressed record. The same inputs always give the same fills.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apps.execution.errors import LiveExecutionRefused
from apps.execution.records import FillRecord, OrderRecord, Side, instrument_key
from core.domain.base import content_hash
from core.domain.execution import ExecutionMode

__all__ = ["CostModel", "LinearCostModel", "SimulatedVenue"]


class CostModel(Protocol):
    """How a simulated fill is priced. Implementations must be pure and deterministic."""

    @property
    def identity(self) -> str: ...

    def fill_price(self, side: Side, price: Decimal) -> Decimal: ...

    def fee(self, quantity: Decimal, fill_price: Decimal) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class LinearCostModel:
    """Proportional slippage against the trader plus a proportional fee on notional.

    Both rates are required parameters; there are no default numbers (ADR-0046).
    """

    fee_rate: Decimal
    slippage_rate: Decimal

    def __post_init__(self) -> None:
        for name in ("fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative Decimal")
        if self.slippage_rate >= 1:
            raise ValueError("slippage_rate must be below 1")

    @property
    def identity(self) -> str:
        return "linear:" + content_hash(
            {"fee_rate": str(self.fee_rate), "slippage_rate": str(self.slippage_rate)}
        )

    def fill_price(self, side: Side, price: Decimal) -> Decimal:
        return price * (1 + self.slippage_rate * side.sign)

    def fee(self, quantity: Decimal, fill_price: Decimal) -> Decimal:
        return abs(quantity) * fill_price * self.fee_rate


class SimulatedVenue:
    """In-process simulated venue. ``mode`` is SIMULATED and cannot be changed."""

    mode = ExecutionMode.SIMULATED

    def __init__(self, *, venue_id: str, cost_model: CostModel) -> None:
        if not venue_id:
            raise ValueError("venue_id must be non-empty")
        self._venue_id = venue_id
        self._cost_model = cost_model
        self._orders: list[OrderRecord] = []
        self._fills: list[FillRecord] = []
        self._filled: set[str] = set()
        self._positions: dict[tuple[str, str], Decimal] = {}

    @property
    def venue_id(self) -> str:
        return self._venue_id

    @property
    def orders(self) -> tuple[OrderRecord, ...]:
        return tuple(self._orders)

    @property
    def fills(self) -> tuple[FillRecord, ...]:
        return tuple(self._fills)

    def position(self, deployment_id: str, key: str) -> Decimal:
        return self._positions.get((deployment_id, key), Decimal(0))

    def positions(self, deployment_id: str) -> Mapping[str, Decimal]:
        return {k: q for (d, k), q in sorted(self._positions.items()) if d == deployment_id}

    def execute(self, order: OrderRecord, price: Decimal, at: datetime) -> FillRecord:
        """Record the order and fill it in full at ``price`` adjusted by the cost model."""
        if order.mode is not ExecutionMode.SIMULATED:
            raise LiveExecutionRefused("the simulated venue executes SIMULATED orders only")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise ValueError("price must be a finite, positive Decimal")
        if order.record_id in self._filled:
            raise ValueError(f"order {order.record_id} was already executed")
        fill_price = self._cost_model.fill_price(order.side, price)
        fill = FillRecord(
            order_id=order.record_id,
            venue_id=self._venue_id,
            deployment_id=order.deployment_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            reference_price=price,
            price=fill_price,
            fee=self._cost_model.fee(order.quantity, fill_price),
            cost_model=self._cost_model.identity,
            filled_at=at,
        )
        self._orders.append(order)
        self._fills.append(fill)
        self._filled.add(order.record_id)
        slot = (order.deployment_id, instrument_key(order.instrument))
        self._positions[slot] = self._positions.get(slot, Decimal(0)) + order.signed_quantity
        return fill
