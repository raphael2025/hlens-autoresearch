"""Measured stage time for the research loop (ADR-0049 measured compute, 2026-09-26).

Stages *declare* their compute seconds (``StageUsage.compute_seconds``) and the budget charges
``max(declared, reported)``. That stays the only thing charged and hashed. On top of it the
scheduler **measures** every ``stage.run`` with a monotonic wall clock and the process CPU clock
and keeps the measurement in ``StageMetrics`` — a side channel **outside** the hashed
``LoopRecord`` (wall-clock durations differ between runs; hashing them would make the audit
non-deterministic).

A stage whose measured time exceeds its declared compute seconds by more than an explicitly
configured tolerance (seconds, no default) is ``flagged``. Without a configured tolerance the
metrics only report (``flagged is None``). A flag never changes the round, the budget or the
halting state: those are decided on the deterministic, hashed record only.

"Measured" is ``max(wall, cpu)``: the process CPU clock counts every thread, so a multi-threaded
stage can use more CPU seconds than wall seconds; the larger of the two is the conservative
reading. The CPU clock is process-wide, so work of other threads during the stage is included.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "NANOS_PER_SECOND",
    "Clock",
    "RoundMetrics",
    "StageMetrics",
    "monotonic_clock",
    "seconds",
]

NANOS_PER_SECOND = Decimal(1_000_000_000)

#: A clock reading: ``(monotonic wall nanoseconds, process CPU nanoseconds)``.
type Clock = Callable[[], tuple[int, int]]


def monotonic_clock() -> tuple[int, int]:
    """The default clock: ``time.monotonic_ns()`` and ``time.process_time_ns()``."""
    return time.monotonic_ns(), time.process_time_ns()


def seconds(nanos: int) -> Decimal:
    """Exact ``Decimal`` seconds of a non-negative nanosecond count (a clock never runs back)."""
    if nanos < 0:
        raise ValueError("a monotonic clock went backwards")
    return Decimal(nanos) / NANOS_PER_SECOND


@dataclass(frozen=True, slots=True)
class StageMetrics:
    """The measured time of one stage run (never hashed, never charged)."""

    name: str
    declared_compute_seconds: Decimal
    wall_seconds: Decimal
    cpu_seconds: Decimal
    #: ``None``: no tolerance configured (report only); else whether the excess is over it.
    flagged: bool | None

    @property
    def measured_seconds(self) -> Decimal:
        return max(self.wall_seconds, self.cpu_seconds)

    @property
    def excess_seconds(self) -> Decimal:
        """How far the measurement is above the declaration (zero when it is not)."""
        return max(self.measured_seconds - self.declared_compute_seconds, Decimal(0))

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared_compute_seconds": str(self.declared_compute_seconds),
            "wall_seconds": str(self.wall_seconds),
            "cpu_seconds": str(self.cpu_seconds),
            "excess_seconds": str(self.excess_seconds),
            "flagged": self.flagged,
        }


@dataclass(frozen=True, slots=True)
class RoundMetrics:
    """Side-channel metrics of one round, linked to its audit record by ``record_hash``."""

    loop_id: str
    round_index: int
    record_hash: str
    #: The configured tolerance in seconds, ``None`` when none was configured.
    tolerance_seconds: Decimal | None
    stages: tuple[StageMetrics, ...]

    @property
    def flagged(self) -> tuple[str, ...]:
        """Names of the stages whose measured time exceeded declaration + tolerance."""
        return tuple(stage.name for stage in self.stages if stage.flagged)

    def payload(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "round_index": self.round_index,
            "record_hash": self.record_hash,
            "tolerance_seconds": (
                None if self.tolerance_seconds is None else str(self.tolerance_seconds)
            ),
            "stages": [stage.payload() for stage in self.stages],
            "flagged": list(self.flagged),
        }
