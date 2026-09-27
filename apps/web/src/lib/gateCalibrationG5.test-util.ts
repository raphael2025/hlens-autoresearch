// G5 blocks exactly as research/synthetic_lab/gate_calibration.py writes them: the toy G5 setup
// of tests/research/synthetic_lab/test_gate_calibration_g5.py with its `_RaisingOnPlanted`
// detector (TEST ONLY; every planted G5 raised), serialized by `GateCalibrationReport.to_payload()`.
// The noise block has no detector errors (so no bounds keys); the planted block has 4 G5 errors,
// hence `pass_rate_bounds` and `end_to_end_bounds`.
import type { SealedG5Evidence } from "./gateCalibration.ts";

export const NOISE_G5: SealedG5Evidence = {
  "reached": 4,
  "pass_rate": {
    "count": 3,
    "n": 4,
    "rate": "0.750000",
    "interval": {
      "method": "clopper-pearson",
      "alpha": "0.05",
      "lower": "0.194120",
      "upper": "0.993691"
    }
  },
  "inconclusive_rate": {
    "count": 0,
    "n": 4,
    "rate": "0.000000",
    "interval": {
      "method": "clopper-pearson",
      "alpha": "0.05",
      "lower": "0.000000",
      "upper": "0.602365"
    }
  },
  "fail_rate": {
    "count": 1,
    "n": 4,
    "rate": "0.250000",
    "interval": {
      "method": "clopper-pearson",
      "alpha": "0.05",
      "lower": "0.006309",
      "upper": "0.805880"
    }
  },
  "consumed_without_result": 0,
  "detector_errors": 0,
  "end_to_end_g0_g5": {
    "false_positive_rate": {
      "count": 3,
      "n": 4,
      "rate": "0.750000",
      "interval": {
        "method": "clopper-pearson",
        "alpha": "0.05",
        "lower": "0.194120",
        "upper": "0.993691"
      }
    }
  }
};

export const PLANTED_G5_WITH_ERRORS: SealedG5Evidence = {
  "reached": 4,
  "pass_rate": {
    "count": 0,
    "n": 4,
    "rate": "0.000000",
    "interval": {
      "method": "clopper-pearson",
      "alpha": "0.05",
      "lower": "0.000000",
      "upper": "0.602365"
    }
  },
  "inconclusive_rate": {
    "count": 4,
    "n": 4,
    "rate": "1.000000",
    "interval": {
      "method": "clopper-pearson",
      "alpha": "0.05",
      "lower": "0.397635",
      "upper": "1.000000"
    }
  },
  "fail_rate": {
    "count": 0,
    "n": 4,
    "rate": "0.000000",
    "interval": {
      "method": "clopper-pearson",
      "alpha": "0.05",
      "lower": "0.000000",
      "upper": "0.602365"
    }
  },
  "consumed_without_result": 4,
  "detector_errors": 4,
  "end_to_end_g0_g5": {
    "power": {
      "count": 0,
      "n": 4,
      "rate": "0.000000",
      "interval": {
        "method": "clopper-pearson",
        "alpha": "0.05",
        "lower": "0.000000",
        "upper": "0.602365"
      }
    }
  },
  "pass_rate_bounds": [
    "0.000000",
    "1.000000"
  ],
  "end_to_end_bounds": [
    "0.000000",
    "1.000000"
  ]
};
