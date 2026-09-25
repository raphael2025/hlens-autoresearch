"""Real-time monitoring from fills: PnL, positions, drawdown and risk-breach events (ADR-0046).

The monitor keeps its own position book from fills and marks. It raises an ``Alert`` when the
drawdown from peak equity reaches the injected threshold (once per breach episode), and for every
risk rejection and kill-switch trip it is told about. Alerts go to every registered hook; a hook may
for example trip the kill switch.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from apps.execution.book import PositionBook
from apps.execution.records import (
    Alert,
    AlertKind,
    FillRecord,
    KillSwitchTrip,
    RejectionRecord,
)

__all__ = ["AlertHook", "Monitor", "MonitorSnapshot"]

AlertHook = Callable[[Alert], None]


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    positions: Mapping[str, Decimal]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    gross_exposure: Decimal


class Monitor:
    def __init__(self, *, capital: Decimal, max_drawdown: Decimal) -> None:
        """``max_drawdown`` is an amount in quote currency below peak equity; no default."""
        if not isinstance(max_drawdown, Decimal) or not max_drawdown.is_finite():
            raise ValueError("max_drawdown must be a finite Decimal")
        if max_drawdown <= 0:
            raise ValueError("max_drawdown must be positive")
        self._book = PositionBook(capital)
        self._max_drawdown = max_drawdown
        self._peak = capital
        self._in_breach = False
        self._alerts: list[Alert] = []
        self._hooks: list[AlertHook] = []

    @property
    def alerts(self) -> tuple[Alert, ...]:
        return tuple(self._alerts)

    def add_alert_hook(self, hook: AlertHook) -> None:
        self._hooks.append(hook)

    def snapshot(self) -> MonitorSnapshot:
        equity = self._book.equity()
        return MonitorSnapshot(
            positions=self._book.positions(),
            realized_pnl=self._book.realized_pnl,
            unrealized_pnl=self._book.unrealized_pnl(),
            fees=self._book.fees,
            equity=equity,
            peak_equity=self._peak,
            drawdown=self._peak - equity,
            gross_exposure=self._book.gross_exposure(),
        )

    def mark(self, prices: Mapping[str, Decimal], at: datetime) -> None:
        self._book.mark(prices)
        self._evaluate(at)

    def on_fill(self, fill: FillRecord) -> None:
        self._book.apply(fill)
        self._evaluate(fill.filled_at)

    def on_rejection(self, rejection: RejectionRecord) -> None:
        self._raise(
            AlertKind.RISK_REJECTION,
            f"{rejection.source.value} rejected an order: {rejection.reason}",
            at=rejection.rejected_at,
            observed=rejection.observed,
            threshold=rejection.limit,
            related=rejection.record_id,
        )

    def on_kill_switch(self, trip: KillSwitchTrip) -> None:
        self._raise(
            AlertKind.KILL_SWITCH_TRIPPED,
            f"kill switch tripped by {trip.tripped_by}: {trip.reason}",
            at=trip.tripped_at,
            related=trip.record_id,
        )

    def _evaluate(self, at: datetime) -> None:
        equity = self._book.equity()
        self._peak = max(self._peak, equity)
        drawdown = self._peak - equity
        if drawdown >= self._max_drawdown:
            if not self._in_breach:
                self._in_breach = True
                self._raise(
                    AlertKind.DRAWDOWN_BREACH,
                    "drawdown from peak equity reached the threshold",
                    at=at,
                    observed=drawdown,
                    threshold=self._max_drawdown,
                )
        else:
            self._in_breach = False

    def _raise(
        self,
        kind: AlertKind,
        message: str,
        *,
        at: datetime,
        observed: Decimal | None = None,
        threshold: Decimal | None = None,
        related: str | None = None,
    ) -> None:
        alert = Alert(
            sequence=len(self._alerts),
            kind=kind,
            message=message,
            observed=observed,
            threshold=threshold,
            related_record_id=related,
            raised_at=at,
        )
        self._alerts.append(alert)
        for hook in tuple(self._hooks):
            hook(alert)
