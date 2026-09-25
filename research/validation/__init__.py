"""Validation Pipeline: G0 – G3 + G5 (Phase 4, ADR-0037) and G4 robustness (Phase 8, ADR-0041).

Research code: never production (H5). Every numeric threshold is read from the ValidationProfile
passed in, or — for a rule the Profile contract has no field for — from an explicit parameter
recorded as ``param:<name>``; nothing here has a default threshold, and a rule with neither is
``INCONCLUSIVE`` (``profile_field_missing``). Status: FRAMEWORK_IMPLEMENTED / NOT_VALIDATED.
"""

from research.validation.g4 import (
    RobustnessInput,
    RobustnessParams,
    RobustnessResult,
    ValidationRun,
    run_robustness,
    run_validation,
)
from research.validation.pipeline import (
    InSampleInput,
    SealedOosInput,
    ValidationContext,
    build_report,
    failure_record,
    reason_for_gate,
    run_in_sample,
    run_sealed_oos,
    sealed_oos_without_result,
)
from research.validation.report import report_view, to_json
from research.validation.retro_audit import AuditSubject, RetroAuditReport, retro_audit

__all__ = [
    "AuditSubject",
    "InSampleInput",
    "RetroAuditReport",
    "RobustnessInput",
    "RobustnessParams",
    "RobustnessResult",
    "SealedOosInput",
    "ValidationContext",
    "ValidationRun",
    "build_report",
    "failure_record",
    "reason_for_gate",
    "report_view",
    "retro_audit",
    "run_in_sample",
    "run_robustness",
    "run_sealed_oos",
    "run_validation",
    "sealed_oos_without_result",
    "to_json",
]
