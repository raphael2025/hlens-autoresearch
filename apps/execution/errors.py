"""Execution-plane refusals (ADR-0046). Every refusal is also recorded where it happens."""

from __future__ import annotations

__all__ = [
    "DeploymentNotAdmitted",
    "ExecutionRefused",
    "LadderGateRefused",
    "LiveExecutionRefused",
]


class ExecutionRefused(Exception):
    """Base class: the execution service refused to act."""


class LiveExecutionRefused(ExecutionRefused):
    """LIVE execution is structurally absent from this build (ADR-0046 red line, H10).

    Raised for ``ExecutionMode.LIVE``, for a non-simulated venue, and for any ladder step beyond
    PAPER, with or without an ``AuthorizationRecord``.
    """


class LadderGateRefused(ExecutionRefused):
    """A ladder step that is not a legal transition (skipping a rung, going backwards)."""


class DeploymentNotAdmitted(ExecutionRefused):
    """Target positions from a deployment that was not admitted (ADR-0005 §4)."""
