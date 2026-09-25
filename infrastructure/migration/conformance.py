"""Adapter conformance runs for a migration candidate (Phase 14; 10-migration.md §2).

A replacement adapter (catalog, storage, backtest, compute, event bus, LLM ...) must pass the same
provider-agnostic checks as the implementation it replaces. ``run_conformance`` runs named checks
(callables that raise on violation) against a fresh candidate per check and reports every
failure; nothing is skipped silently.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["ConformanceReport", "run_conformance"]


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    candidate: str
    checks: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and not self.failures


def run_conformance(
    candidate: str,
    make: Callable[[], Any],
    checks: Sequence[Callable[[Any], None]],
) -> ConformanceReport:
    if not checks:
        raise ValueError("a conformance run needs at least one check")
    failures: list[tuple[str, str]] = []
    for check in checks:
        try:
            check(make())
        except Exception as exc:  # noqa: BLE001 - every failure is reported, none is skipped
            failures.append((check.__name__, f"{type(exc).__name__}: {exc}"))
    return ConformanceReport(
        candidate=candidate,
        checks=tuple(check.__name__ for check in checks),
        failures=tuple(failures),
    )
