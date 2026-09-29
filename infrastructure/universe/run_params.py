"""Explicit sorted-run resource parameters shared by Universe and Dataset source adapters."""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.pit.runs import RunLimits


@dataclass(frozen=True, slots=True)
class UniverseRunParams:
    """Sorted-run sizes for Universe event/projection streams and their Dataset adapters.

    No values have defaults (ADR-0077 DQ-9 remains open): callers choose them from capacity
    evidence and pass the same explicit parameters through both stages.
    """

    capacity: int
    merge_fanout: int
    limits: RunLimits

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("capacity", self.capacity, 1),
            ("merge_fanout", self.merge_fanout, 2),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
        if not isinstance(self.limits, RunLimits):
            raise ValueError("limits must be RunLimits")
