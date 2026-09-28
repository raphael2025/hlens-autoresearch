import type { ReportEnvelope, ReportKind } from "../api";

export type ReportDTOView =
  | { status: "supported"; version: string }
  | { status: "unknown-version"; version: string }
  | { status: "invalid"; version: string; reason: string };

type Rule = { baseline: string; versions: readonly string[]; required: readonly string[] };

// Keep in sync with apps/api/report_dto.py and ADR-0081. Missing schema_version means the
// registered baseline for legacy payloads; never write that inferred value back to the payload.
const RULES: Record<ReportKind, Rule> = {
  validation_report: { baseline: "2.3.0", versions: ["2.0.0", "2.1.0", "2.2.0", "2.3.0"], required: ["schema_version", "gates", "verdict"] },
  research_loop_round: { baseline: "1.0.0", versions: ["1.0.0"], required: ["round_index", "loop_id", "stages"] },
  state_strategy_matrix: { baseline: "1.0.0", versions: ["1.0.0"], required: ["matrix_hash", "cells", "strategy", "state"] },
  router_paper_run: { baseline: "1.0.0", versions: ["1.0.0"], required: ["run_hash", "router", "decisions", "charges"] },
  gate_calibration: { baseline: "1.0.0", versions: ["1.0.0"], required: ["schema_version", "report_hash", "candidates"] },
  router_stop: { baseline: "1.0.0", versions: ["1.0.0"], required: ["stop_hash", "router", "reason"] },
  state_diagnostics: { baseline: "1.1.0", versions: ["1.0.0", "1.1.0"], required: ["schema_version", "diagnostics_hash", "state_space"] },
  event_statistics: { baseline: "1.0.0", versions: ["1.0.0"], required: ["schema_version", "report_hash", "statistics"] },
  paper_deviation: { baseline: "2.0.0", versions: ["1.0.0", "2.0.0"], required: ["kind", "deviation_hash"] },
  degradation_check: { baseline: "1.1.0", versions: ["1.0.0", "1.1.0"], required: ["schema_version", "check_hash", "metrics"] },
  retro_audit: { baseline: "1.1.0", versions: ["1.0.0", "1.1.0"], required: ["schema_version", "report_hash", "kind"] },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function inspectReportDTO(report: ReportEnvelope): ReportDTOView {
  const payload: unknown = report.payload;
  if (!isRecord(payload)) return { status: "invalid", version: "?", reason: "payload must be an object" };
  const rule = RULES[report.kind];
  const rawVersion = payload.schema_version;
  if (rawVersion !== undefined && typeof rawVersion !== "string") {
    return { status: "invalid", version: "?", reason: "schema_version must be a string" };
  }
  const version = rawVersion ?? rule.baseline;
  if (!rule.versions.includes(version)) return { status: "unknown-version", version };
  const missing = rule.required.find((field) => !(field in payload));
  if (missing !== undefined) return { status: "invalid", version, reason: `missing ${missing}` };
  if (report.kind === "paper_deviation" && payload.kind !== "paper_deviation") {
    return { status: "invalid", version, reason: "kind does not match paper_deviation" };
  }
  if (
    report.kind === "paper_deviation" &&
    version === "2.0.0" &&
    !["declared_scope", "summary", "marks"].every((field) => field in payload)
  ) {
    return { status: "invalid", version, reason: "2.0.0 requires scope-bound report fields" };
  }
  if (report.kind === "paper_deviation" && version === "2.0.0") {
    const scope = payload.declared_scope;
    if (
      !isRecord(scope) ||
      scope.scope_schema_version !== "1.0.0" ||
      typeof scope.scope_hash !== "string" ||
      !/^[0-9a-f]{64}$/.test(scope.scope_hash) ||
      ![
        "validation_profile",
        "validation_profile_hash",
        "validation_report_hash",
        "venue",
        "symbol",
        "timeframe",
        "research_class",
      ].every((field) => typeof scope[field] === "string") ||
      typeof scope.validation_profile_hash !== "string" ||
      !/^[0-9a-f]{64}$/.test(scope.validation_profile_hash) ||
      typeof scope.validation_report_hash !== "string" ||
      !/^[0-9a-f]{64}$/.test(scope.validation_report_hash)
    ) {
      return { status: "invalid", version, reason: "declared_scope does not match scope DTO 1.0.0" };
    }
  }
  return { status: "supported", version };
}
