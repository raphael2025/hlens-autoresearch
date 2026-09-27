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
//
// Uncertainty from detector errors (B45 / B48 / B54): an arm with `detector_errors` also carries
// `pass_rate_bounds` — `[passed / n, (passed + errors) / n]`, decimal strings rounded outward — the
// range of its pipeline pass rate had every errored run gone either way. In G5 mode the arm's
// `sealed_oos_g5` block (G5 rates conditional on reaching G5, and `end_to_end_g0_g5` over every
// run) likewise adds `pass_rate_bounds` (denominator `reached`) and `end_to_end_bounds`
// (denominator every run) only when errors occurred. With errors the point rate is **not** the
// rate: the page shows the bounds next to it. A bounds key that is present but not a well-formed
// `[lower, upper]` pair is reported as malformed, never dropped (fail closed).

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
  pass_rate_bounds?: unknown;
  sealed_oos_g5?: SealedG5Evidence;
};

// One arm's G5 (sealed OOS) evidence under one candidate, G5 mode only. The conditional rates are
// `null` when no run of the arm reached G5.
export type SealedG5Evidence = {
  reached: number;
  pass_rate: Rate | null;
  inconclusive_rate: Rate | null;
  fail_rate: Rate | null;
  consumed_without_result: number;
  detector_errors: number;
  end_to_end_g0_g5: { false_positive_rate?: Rate; power?: Rate };
  pass_rate_bounds?: unknown;
  end_to_end_bounds?: unknown;
};

/** A bounds key as found in the payload: absent, a well-formed pair, or malformed. */
export type Bounds =
  | { kind: "absent" }
  | { kind: "bounds"; lower: string; upper: string }
  | { kind: "malformed" };

const DECIMAL = /^(0|[1-9][0-9]*)(\.[0-9]+)?$/;

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

/**
 * Read a `pass_rate_bounds` / `end_to_end_bounds` value: absent (`undefined`), a pair of
 * non-negative decimal strings with lower <= upper <= 1, or malformed (anything else).
 */
export function readBounds(value: unknown): Bounds {
  if (value === undefined) return { kind: "absent" };
  if (!Array.isArray(value) || value.length !== 2) return { kind: "malformed" };
  const [lower, upper] = value;
  if (typeof lower !== "string" || typeof upper !== "string") return { kind: "malformed" };
  if (!DECIMAL.test(lower) || !DECIMAL.test(upper)) return { kind: "malformed" };
  if (Number(lower) > Number(upper) || Number(upper) > 1) return { kind: "malformed" };
  return { kind: "bounds", lower, upper };
}

/** `[lower, upper]` for well-formed bounds, a visible marker for malformed ones, else `null`. */
export function boundsCell(bounds: Bounds): string | null {
  if (bounds.kind === "bounds") return `[${bounds.lower}, ${bounds.upper}]`;
  if (bounds.kind === "malformed") return "malformed bounds";
  return null;
}

/** Whether any arm of the candidate carries pipeline `pass_rate_bounds` (present or malformed). */
export function hasPassRateBounds(candidate: CandidateEvidence): boolean {
  return Object.values(candidate.pipeline).some(
    (evidence) => readBounds(evidence.pass_rate_bounds).kind !== "absent",
  );
}

/** An arm's G5 block, or `null` outside G5 mode. */
export function sealedG5Of(evidence: ArmPipelineEvidence): SealedG5Evidence | null {
  const block = evidence.sealed_oos_g5;
  return block !== undefined && block !== null && typeof block === "object" ? block : null;
}

/** The arms of the candidate that have a G5 block, in pipeline order. */
export function sealedG5Arms(candidate: CandidateEvidence): string[] {
  return Object.keys(candidate.pipeline).filter((arm) => sealedG5Of(candidate.pipeline[arm]) !== null);
}
