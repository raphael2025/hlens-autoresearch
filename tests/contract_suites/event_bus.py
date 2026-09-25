"""Provider-agnostic contract suite for ``EventBusAdapter`` (ADR-0044; 10-migration.md §2)."""

from __future__ import annotations

from collections.abc import Callable

from core.contracts.event_bus import BusMessage, EventBusAdapter

BusCheck = Callable[[EventBusAdapter], None]


def check_at_least_once_until_ack(bus: EventBusAdapter) -> None:
    message = BusMessage.build("jobs", "k1", {"n": 1})
    bus.publish(message)
    assert bus.poll("c", "jobs", 10) == (message,)
    assert bus.poll("c", "jobs", 10) == (message,), "unacked messages must be redelivered"
    bus.ack("c", "jobs", message.message_id)
    assert bus.poll("c", "jobs", 10) == ()


def check_consumers_are_independent(bus: EventBusAdapter) -> None:
    message = BusMessage.build("jobs", "k1", {"n": 1})
    bus.publish(message)
    bus.ack("a", "jobs", message.message_id)
    assert bus.poll("b", "jobs", 10) == (message,)


def check_publish_order_within_a_topic(bus: EventBusAdapter) -> None:
    first, second = BusMessage.build("t", "1", {"i": 1}), BusMessage.build("t", "2", {"i": 2})
    bus.publish(first)
    bus.publish(second)
    assert bus.poll("c", "t", 10) == (first, second)
    assert bus.poll("c", "t", 1) == (first,)


BUS_CHECKS: tuple[BusCheck, ...] = (
    check_at_least_once_until_ack,
    check_consumers_are_independent,
    check_publish_order_within_a_topic,
)
