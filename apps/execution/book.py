"""Average-cost position book built only from fills (used separately by risk and monitor).

Second-line risk and the monitor each own an instance and feed it the fills they observe, so neither
depends on the strategy's own view of its positions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from apps.execution.records import FillRecord, instrument_key

__all__ = ["PositionBook"]

_ZERO = Decimal(0)


@dataclass(slots=True)
class _Lot:
    quantity: Decimal = _ZERO
    average_price: Decimal = _ZERO


class PositionBook:
    def __init__(self, capital: Decimal) -> None:
        if not isinstance(capital, Decimal) or not capital.is_finite() or capital <= 0:
            raise ValueError("capital must be a finite, positive Decimal")
        self.capital = capital
        self._lots: dict[str, _Lot] = {}
        self._marks: dict[str, Decimal] = {}
        self.realized_pnl = _ZERO
        self.fees = _ZERO

    def positions(self) -> Mapping[str, Decimal]:
        return {k: lot.quantity for k, lot in sorted(self._lots.items()) if lot.quantity != 0}

    def quantity(self, key: str) -> Decimal:
        lot = self._lots.get(key)
        return lot.quantity if lot else _ZERO

    def mark(self, prices: Mapping[str, Decimal]) -> None:
        for key, price in prices.items():
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise ValueError(f"mark price for {key} must be a finite, positive Decimal")
            self._marks[key] = price

    def mark_of(self, key: str) -> Decimal | None:
        return self._marks.get(key)

    def apply(self, fill: FillRecord) -> None:
        key = instrument_key(fill.instrument)
        lot = self._lots.setdefault(key, _Lot())
        delta = fill.signed_quantity
        before = lot.quantity
        after = before + delta
        if before == 0 or (before > 0) == (delta > 0):
            lot.average_price = (abs(before) * lot.average_price + abs(delta) * fill.price) / abs(
                after
            )
        else:
            closed = min(abs(delta), abs(before))
            direction = 1 if before > 0 else -1
            self.realized_pnl += closed * (fill.price - lot.average_price) * direction
            if after == 0:
                lot.average_price = _ZERO
            elif (after > 0) != (before > 0):
                lot.average_price = fill.price
        lot.quantity = after
        self.fees += fill.fee
        self._marks[key] = fill.price

    def _mark_or_cost(self, key: str, lot: _Lot) -> Decimal:
        return self._marks.get(key, lot.average_price)

    def unrealized_pnl(self) -> Decimal:
        return sum(
            (
                lot.quantity * (self._mark_or_cost(k, lot) - lot.average_price)
                for k, lot in self._lots.items()
            ),
            _ZERO,
        )

    def gross_exposure(self, overrides: Mapping[str, Decimal] | None = None) -> Decimal:
        """Sum of |quantity x mark|; ``overrides`` replaces quantities (pre-trade)."""
        quantities = {k: lot.quantity for k, lot in self._lots.items()}
        quantities.update(overrides or {})
        total = _ZERO
        for key, quantity in quantities.items():
            lot = self._lots.get(key, _Lot())
            mark = self._mark_or_cost(key, lot)
            total += abs(quantity * mark)
        return total

    def equity(self) -> Decimal:
        return self.capital + self.realized_pnl - self.fees + self.unrealized_pnl()
