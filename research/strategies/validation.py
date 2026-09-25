"""Narrow local interface: "validate this backtest result" (Phase 5; ADR-0038).

Phase 5 does not validate anything by itself: it has no gates, no thresholds and no Profile numbers
(those belong to the Phase 4 pipeline, ``research/validation``, ADR-0037, built in parallel). The
strategy pipeline only needs this seam:

    BacktestValidator.validate(subject, spec, backtest) -> BacktestValidation

``BacktestValidation`` carries the ``ValidationReport`` the Phase 4 pipeline produced for the
subject, plus the ``ReasonCode`` to file in the Failure Registry when the verdict is ``FAIL``
(``GateResult`` carries no reason code, so the validator states it).

TODO(phase4-wiring, ADR-0037): the lead wires ``research/validation`` here — an adapter that maps a
``BacktestResult`` (equity curve, fills, cost model ref, bar grid) to the Phase 4 validation input
(returns series, sealed OOS split, frozen Validation Profile, trial count from the declared
parameter space) and returns its ``ValidationReport``. Until then ``evaluate_strategy`` runs with
``validator=None`` and every evaluation is ``NOT_VALIDATED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.contracts.strategy import BacktestResult
from core.domain.base import Ref
from core.domain.research import ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.errors import ReasonCode

__all__ = ["BacktestValidation", "BacktestValidator"]


@dataclass(frozen=True, slots=True)
class BacktestValidation:
    """A validator's answer: the report, and the failure reason when the verdict is ``FAIL``."""

    report: ValidationReport
    failure_reason: ReasonCode | None = None

    def __post_init__(self) -> None:
        if (self.report.verdict is Verdict.FAIL) != (self.failure_reason is not None):
            raise ValueError("failure_reason is required exactly when the verdict is FAIL")

    def check_subject(self, subject: Ref) -> None:
        if self.report.subject.target_identity() != subject.target_identity():
            raise ValueError(f"the report is about {self.report.subject}, not {subject}")


class BacktestValidator(Protocol):
    """Validate one strategy's backtest under the frozen Constitution + Validation Profile."""

    def validate(
        self, subject: Ref, spec: StrategySpec, backtest: BacktestResult
    ) -> BacktestValidation: ...
