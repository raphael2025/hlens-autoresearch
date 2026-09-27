"""Event runner errors (Phase 3; ADR-0036). Re-exported by ``infrastructure.event.runner``."""

from __future__ import annotations

__all__ = ["EventRunnerError", "FutureConfirmationError"]


class EventRunnerError(Exception):
    """The run cannot produce an honest result (spec / request mismatch, a non-compliant answer)."""


class FutureConfirmationError(EventRunnerError):
    """Tables as of two checkpoints disagree: an event was back-dated or retracted."""
