"""Kill Switch (09-security.md §5; ADR-0046).

Once tripped it stays tripped for the life of the instance: every later order is rejected before it
can reach the venue. There is deliberately no ``reset``; resuming needs a fresh service instance
created by a human, which is itself an auditable act. Every trip, including repeated ones, is
recorded and handed to the subscribed listeners (the execution service records and publishes it).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from apps.execution.records import KillSwitchTrip

__all__ = ["KillSwitch"]


class KillSwitch:
    def __init__(self) -> None:
        self._trips: list[KillSwitchTrip] = []
        self._listeners: list[Callable[[KillSwitchTrip], None]] = []

    @property
    def tripped(self) -> bool:
        return bool(self._trips)

    @property
    def trips(self) -> tuple[KillSwitchTrip, ...]:
        return tuple(self._trips)

    def subscribe(self, listener: Callable[[KillSwitchTrip], None]) -> None:
        self._listeners.append(listener)

    def trip(self, *, reason: str, tripped_by: str, at: datetime) -> KillSwitchTrip:
        record = KillSwitchTrip(
            sequence=len(self._trips), reason=reason, tripped_by=tripped_by, tripped_at=at
        )
        self._trips.append(record)
        for listener in tuple(self._listeners):
            listener(record)
        return record
