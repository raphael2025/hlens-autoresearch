// View model of the Validation Reports page. Payload shape written by
// research/reports/validation.py (write_validation_report): a `core.domain.research.ValidationReport`
// dumped as JSON — hand-typed since /reports/{kind} has no per-kind OpenAPI schema
// (ReportEnvelope.payload is `dict[str, Any]`). Pure: tested by validationReport.test.ts with
// `node --test` over apps/web/fixtures (the current 2.1.0 report and the legacy 2.0.0 one).
//
// Exact values (ADR-0052 §1, contract 2.1.0): a `GateResult` may carry `value_exact` /
// `threshold_exact` — canonical decimal strings. When present they are authoritative (the verdict
// was decided on them) and the float `value` / `threshold` are only `float(exact)`, so the page
// shows the exact text. A 2.0.0 report has no such keys: the float is all there is and is shown.

export type Gate = {
  gate_id?: string;
  metric?: string;
  value?: number;
  threshold?: number | null;
  threshold_source?: string | null;
  verdict?: string;
  /** 2.1.0+: canonical decimal text; authoritative over `value` when present. */
  value_exact?: string;
  /** 2.1.0+: canonical decimal text; authoritative over `threshold` when present. */
  threshold_exact?: string;
  schema_version?: string;
};

export type ValidationReportPayload = {
  report_id?: string;
  run_id?: string;
  schema_version?: string;
  verdict: string;
  gates: Gate[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The payload as a validation report (a `verdict` and a `gates` list of objects), else `null`. */
export function asValidationReportPayload(
  payload: Record<string, unknown> | undefined,
): ValidationReportPayload | null {
  if (
    payload === undefined ||
    typeof payload.verdict !== "string" ||
    !Array.isArray(payload.gates) ||
    !payload.gates.every(isRecord)
  ) {
    return null;
  }
  return payload as unknown as ValidationReportPayload;
}

/** One number as the page shows it: the exact text when present, else the float, else "—". */
export type Shown = { text: string; exact: boolean };

export function shownNumber(exact: unknown, float: unknown): Shown {
  if (typeof exact === "string" && exact !== "") return { text: exact, exact: true };
  if (typeof float === "number" && Number.isFinite(float)) return { text: String(float), exact: false };
  return { text: "—", exact: false };
}

export type GateRow = {
  gateId: string;
  metric: string;
  value: Shown;
  threshold: Shown;
  thresholdSource: string;
  verdict: string;
  /** "exact" when the value (and so any threshold, ADR-0052 §1) is exact; else "float". */
  representation: "exact" | "float";
};

function text(value: unknown): string {
  return typeof value === "string" && value !== "" ? value : "—";
}

export function gateRows(report: ValidationReportPayload): GateRow[] {
  return report.gates.map((gate) => {
    const value = shownNumber(gate.value_exact, gate.value);
    return {
      gateId: text(gate.gate_id),
      metric: text(gate.metric),
      value,
      threshold: shownNumber(gate.threshold_exact, gate.threshold),
      thresholdSource: text(gate.threshold_source),
      verdict: text(gate.verdict),
      representation: value.exact ? "exact" : "float",
    };
  });
}

/** The list button text: `<id12>… [VERDICT]`, or the bare id when the payload does not parse. */
export function validationLabel(id: string, payload: Record<string, unknown> | undefined): string {
  const report = asValidationReportPayload(payload);
  return report === null ? id : `${id.slice(0, 12)}… [${report.verdict}]`;
}
