export type RetroAuditFinding = Record<string, unknown> & {
  subject: string;
  lifecycle_state: string;
  recorded_verdict: string;
  current_verdict: string;
  effective_verdict: string;
  action: string;
  would_now_pass: boolean;
  gate_diffs: Record<string, unknown>[];
};

export type RetroAuditPayload = Record<string, unknown> & {
  kind: "retro_audit";
  schema_version: string;
  status: string;
  audited_at: string;
  rules: string;
  subjects: number;
  flagged_for_revalidation: number;
  rejected_that_would_now_pass: number;
  findings: RetroAuditFinding[];
  note: string;
  report_hash: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFinding(value: unknown): value is RetroAuditFinding {
  return (
    isRecord(value) &&
    typeof value.subject === "string" &&
    typeof value.lifecycle_state === "string" &&
    typeof value.recorded_verdict === "string" &&
    typeof value.current_verdict === "string" &&
    typeof value.effective_verdict === "string" &&
    typeof value.action === "string" &&
    typeof value.would_now_pass === "boolean" &&
    Array.isArray(value.gate_diffs) &&
    value.gate_diffs.every(isRecord)
  );
}

export function asRetroAuditPayload(
  payload: Record<string, unknown> | undefined,
): RetroAuditPayload | null {
  if (
    payload === undefined ||
    payload.kind !== "retro_audit" ||
    typeof payload.schema_version !== "string" ||
    typeof payload.status !== "string" ||
    typeof payload.audited_at !== "string" ||
    typeof payload.rules !== "string" ||
    typeof payload.subjects !== "number" ||
    typeof payload.flagged_for_revalidation !== "number" ||
    typeof payload.rejected_that_would_now_pass !== "number" ||
    !Array.isArray(payload.findings) ||
    !payload.findings.every(isFinding) ||
    typeof payload.note !== "string" ||
    typeof payload.report_hash !== "string"
  ) {
    return null;
  }
  return payload as unknown as RetroAuditPayload;
}

export function retroAuditLabel(report: RetroAuditPayload): string {
  return `${report.audited_at} · ${report.subjects} subjects · ${report.report_hash.slice(0, 12)}…`;
}

export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
