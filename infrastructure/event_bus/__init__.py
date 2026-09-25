"""EventBusAdapter implementations (ADR-0044)."""

from infrastructure.event_bus.memory import InMemoryEventBus

__all__ = ["InMemoryEventBus"]
