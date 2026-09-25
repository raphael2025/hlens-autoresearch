"""In-process, at-least-once event bus (ADR-0044; Phase 1-6 use no NATS, ADR-0021).

Per topic an append-only log; per (consumer, topic) the set of acknowledged message ids. ``poll``
returns the unacknowledged messages in publish order, so a message is redelivered until it is
acknowledged (at least once). Publishing the same content twice appends it twice (duplicates are
possible, as on a real bus); consumers de-duplicate by ``message_id``.
"""

from __future__ import annotations

from core.contracts.event_bus import BusMessage

__all__ = ["InMemoryEventBus"]


class InMemoryEventBus:
    def __init__(self) -> None:
        self._log: dict[str, list[BusMessage]] = {}
        self._acked: dict[tuple[str, str], set[str]] = {}

    def publish(self, message: BusMessage) -> None:
        if not isinstance(message, BusMessage):
            raise TypeError("publish needs a BusMessage")
        self._log.setdefault(message.topic, []).append(message)

    def poll(self, consumer: str, topic: str, limit: int) -> tuple[BusMessage, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        acked = self._acked.get((consumer, topic), set())
        pending = [m for m in self._log.get(topic, []) if m.message_id not in acked]
        return tuple(pending[:limit])

    def ack(self, consumer: str, topic: str, message_id: str) -> None:
        self._acked.setdefault((consumer, topic), set()).add(message_id)
