"""Synthetic Market Lab: method calibration on known truth (Phase 9, ADR-0042)."""

from research.synthetic_lab.calibration import CalibrationReport, calibrate
from research.synthetic_lab.gate_calibration import (
    DISCLAIMER,
    GateCalibrationReport,
    GateCalibrationSetup,
    StrategyValidatorDetector,
    run_gate_calibration,
)

__all__ = [
    "DISCLAIMER",
    "CalibrationReport",
    "GateCalibrationReport",
    "GateCalibrationSetup",
    "StrategyValidatorDetector",
    "calibrate",
    "run_gate_calibration",
]
