"""Minimal Validation Pipeline (Phase 4, ADR-0037). Research code: never production (H5).

Every numeric threshold is read from the ValidationProfile passed in; nothing here has a default
threshold. Status: FRAMEWORK_IMPLEMENTED / NOT_VALIDATED.
"""

from research.validation.pipeline import (
    InSampleInput,
    SealedOosInput,
    ValidationContext,
    build_report,
    failure_record,
    run_in_sample,
    run_sealed_oos,
)

__all__ = [
    "InSampleInput",
    "SealedOosInput",
    "ValidationContext",
    "build_report",
    "failure_record",
    "run_in_sample",
    "run_sealed_oos",
]
