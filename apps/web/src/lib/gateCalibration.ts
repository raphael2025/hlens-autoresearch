// View model of the Gate Calibration page (evidence only, never a Profile decision).
// Payload shape written by research/reports/gate_calibration.py (write_gate_calibration_report),
// which wraps research/synthetic_lab/gate_calibration.py's GateCalibrationReport.to_payload() —
// hand-typed here since /reports/{kind} has no per-kind OpenAPI schema (ReportEnvelope.payload is
// `dict[str, Any]`). Pure: tested by gateCalibration.test.ts with `node --test`.
//
// Detector failures (Phase 9, 2026-09-26): a run whose detector raised is INCONCLUSIVE and carries
// `detector_error` ("<ExceptionType>: <message>"); each arm's pipeline evidence carries
// `detector_errors` (their count). Both keys are **absent when zero** — additive, so older reports
// keep their hashes — hence optional here and shown only when present.

export type Interval = {
  method: string;
  alpha: string;
  lower: string;
  upper: string;
};

export type Rate = {
  n: number;
  count: number;
  rate: string;
  interval: Interval;
};

// One arm's pipeline-level evidence for one candidate: `false_positive_rate` on the noise arm,
// `power` on a planted-effect arm (research/synthetic_lab/gate_calibration.py's `_pass_key`).
export type ArmPipelineEvidence = {
  runs: number;
  false_positive_rate?: Rate;
  power?: Rate;
  inconclusive_rate: Rate;
  failed: number;
  sealed_oos_consumption_rate: Rate;
  detector_errors?: number;
};

// One gate's evidence within one arm, same `false_positive_rate` / `power` split.
export type GateArmEvidence = {
  false_positive_rate?: Rate;
  power?: Rate;
  inconclusive_rate: Rate;
  failed: number;
  not_evaluated: number;
};

export type CalibrationRun = {
  arm: string;
  seed: number;
  verdict: string;
  failing_gates?: string[];
  inconclusive_gates?: string[];
  market_hash?: string;
  sealed_oos_unsealed?: boolean;
  detector_error?: string;
};

export type CandidateEvidence = {
  profile: string;
  profile_hash: string;
  pipeline: Record<string, ArmPipelineEvidence>;
  gates: Record<string, Record<string, GateArmEvidence>>;
  runs: CalibrationRun[];
  sealed_oos_unsealings: number;
};

export type GateCalibrationPayload = {
  kind: string;
  schema_version: string;
  status: string;
  disclaimer: string;
  note: string;
  inputs: {
    detector?: { name?: string };
    interval?: { method?: string; alpha?: string };
    noise_seeds?: unknown[];
    planted_seeds?: unknown[];
    planted_effects?: { arm?: string }[];
  };
  candidates: CandidateEvidence[];
  report_hash: string;
};

export function asCalibrationPayload(
  payload: Record<string, unknown> | undefined,
): GateCalibrationPayload | null {
  if (payload === undefined || !Array.isArray(payload.candidates)) return null;
  return payload as unknown as GateCalibrationPayload;
}

// Same split as the writer's `_pass_key`: the noise arm reports a false-positive rate, every
// other (planted-effect) arm reports power. Never both on the same row.
export function passRate(evidence: { false_positive_rate?: Rate; power?: Rate }): {
  label: "false_positive_rate" | "power";
  rate: Rate;
} | null {
  if (evidence.false_positive_rate !== undefined) {
    return { label: "false_positive_rate", rate: evidence.false_positive_rate };
  }
  if (evidence.power !== undefined) {
    return { label: "power", rate: evidence.power };
  }
  return null;
}

export function ciCell(rate: Rate): string {
  return `[${rate.interval.lower}, ${rate.interval.upper}] (α=${rate.interval.alpha}, ${rate.interval.method})`;
}

/** An arm's detector-error count, or `null` when the payload has no such key (zero errors). */
export function detectorErrorsOf(evidence: ArmPipelineEvidence): number | null {
  return typeof evidence.detector_errors === "number" ? evidence.detector_errors : null;
}

/** Whether any arm of the candidate reports detector errors (the page then adds that column). */
export function hasDetectorErrors(candidate: CandidateEvidence): boolean {
  return Object.values(candidate.pipeline).some((evidence) => detectorErrorsOf(evidence) !== null);
}

/** The runs whose detector raised, in payload order (empty when none). */
export function detectorErrorRuns(candidate: CandidateEvidence): CalibrationRun[] {
  return (Array.isArray(candidate.runs) ? candidate.runs : []).filter(
    (run) => typeof run.detector_error === "string",
  );
}
