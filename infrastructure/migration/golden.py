"""Golden experiment reruns (Phase 14; 10-migration.md §2, roadmap P14 acceptance).

A migration (new compute engine, backtest engine, catalog, ...) must reproduce the gold-standard
experiments: ``record_golden`` freezes a run's named outputs (``Decimal`` values, canonical JSON
hash); ``compare_golden`` reruns and reports every output that differs beyond the migration's
declared tolerance (bit-exact when the tolerance is zero). The tolerance is declared by the
migration's own ADR; it is not a validation threshold (Constitution / Profile untouched).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from core.domain.base import content_hash

__all__ = ["GoldenDiff", "GoldenRecord", "compare_golden", "record_golden"]

_ZERO: Final = Decimal(0)


class GoldenError(ValueError):
    """The golden record or the rerun cannot be compared (shape, types)."""


@dataclass(frozen=True, slots=True)
class GoldenRecord:
    name: str
    outputs: Mapping[str, Decimal]
    outputs_hash: str


@dataclass(frozen=True, slots=True)
class GoldenDiff:
    name: str
    tolerance: Decimal
    bit_identical: bool
    #: output name -> (golden, rerun) for every value beyond the tolerance or missing / extra.
    differences: Mapping[str, tuple[Decimal | None, Decimal | None]]

    @property
    def passed(self) -> bool:
        return not self.differences


def _check(outputs: Mapping[str, Decimal]) -> dict[str, Decimal]:
    checked: dict[str, Decimal] = {}
    for key, value in outputs.items():
        if not isinstance(key, str) or not key:
            raise GoldenError("golden output names must be non-empty strings")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise GoldenError(f"golden output {key!r} must be a finite Decimal")
        checked[key] = value
    return checked


def _hash(outputs: Mapping[str, Decimal]) -> str:
    return content_hash({key: str(value) for key, value in sorted(outputs.items())})


def record_golden(name: str, run: Callable[[], Mapping[str, Decimal]]) -> GoldenRecord:
    outputs = _check(run())
    return GoldenRecord(name=name, outputs=dict(outputs), outputs_hash=_hash(outputs))


def compare_golden(
    golden: GoldenRecord, rerun: Callable[[], Mapping[str, Decimal]], tolerance: Decimal
) -> GoldenDiff:
    if not isinstance(tolerance, Decimal) or not tolerance.is_finite() or tolerance < _ZERO:
        raise GoldenError("tolerance must be a finite, non-negative Decimal")
    outputs = _check(rerun())
    differences: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for key in sorted(set(golden.outputs) | set(outputs)):
        old, new = golden.outputs.get(key), outputs.get(key)
        if old is None or new is None or abs(old - new) > tolerance:
            differences[key] = (old, new)
    return GoldenDiff(
        name=golden.name,
        tolerance=tolerance,
        bit_identical=_hash(outputs) == golden.outputs_hash,
        differences=differences,
    )
