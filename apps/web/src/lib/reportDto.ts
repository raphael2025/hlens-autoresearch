import type { ReportEnvelope, ReportKind } from "../api";

export type ReportDTOView =
  | { status: "supported"; version: string }
  | { status: "unknown-version"; version: string }
  | { status: "invalid"; version: string; reason: string };

type Rule = {
  baseline: string;
  versions: readonly string[];
  required: readonly string[];
  hasPayloadSchemaVersion?: boolean;
};

// Keep in sync with apps/api/report_dto.py and ADR-0081. Missing schema_version means the
// registered baseline for legacy payloads; never write that inferred value back to the payload.
const RULES: Record<ReportKind, Rule> = {
  validation_report: { baseline: "2.5.0", versions: ["2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0", "2.5.0"], required: ["schema_version", "gates", "verdict"] },
  research_loop_round: {
    baseline: "1.0.0",
    versions: ["1.0.0"],
    required: ["round_index", "loop_id", "stages"],
    hasPayloadSchemaVersion: false,
  },
  state_strategy_matrix: { baseline: "1.0.0", versions: ["1.0.0"], required: ["matrix_hash", "cells", "strategy", "state"] },
  router_paper_run: {
    baseline: "1.0.0",
    versions: ["1.0.0"],
    required: ["run_hash", "router", "decisions", "charges"],
    hasPayloadSchemaVersion: false,
  },
  gate_calibration: { baseline: "1.0.0", versions: ["1.0.0"], required: ["schema_version", "report_hash", "candidates"] },
  router_stop: {
    baseline: "1.0.0",
    versions: ["1.0.0"],
    required: ["stop_hash", "router", "reason"],
    hasPayloadSchemaVersion: false,
  },
  state_diagnostics: {
    baseline: "1.1.0",
    versions: ["1.0.0", "1.1.0"],
    required: ["schema_version", "kind", "state_space"],
  },
  event_statistics: { baseline: "1.0.0", versions: ["1.0.0"], required: ["schema_version", "report_hash", "statistics"] },
  // 1.0.0 descriptive; 2.0.0 scope-only (ADR-0079); 2.1.0 scope + run binding (ADR-0104)
  paper_deviation: { baseline: "2.1.0", versions: ["1.0.0", "2.0.0", "2.1.0"], required: ["kind", "deviation_hash"] },
  degradation_check: { baseline: "1.1.0", versions: ["1.0.0", "1.1.0"], required: ["schema_version", "check_hash", "metrics"] },
  retro_audit: { baseline: "1.1.0", versions: ["1.0.0", "1.1.0"], required: ["schema_version", "report_hash", "kind"] },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// paper_deviation scope-bound DTO rules (ADR-0079 / ADR-0104). Shape only: apps/api verifies the
// hashes. Deliberately a copy of the helpers in paperDeviation.ts (this module is also run by
// `node --test`, which needs `.ts` import suffixes that the app's tsconfig does not allow).
const DEVIATION_SCOPE_VERSIONS: Record<string, string> = { "2.0.0": "1.0.0", "2.1.0": "1.1.0" };
const RUN_BINDING_HASHES = [
  "router_spec_hash",
  "router_strategy_spec_hash",
  "experiment_hash",
  "bars_hash",
  "cost_model_hash",
  "reference_request_hash",
] as const;
const isHash = (value: unknown): boolean => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);

function isRunBinding(value: unknown, referenceRequestHash: unknown): boolean {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value).sort().join(",");
  if (keys !== [...RUN_BINDING_HASHES, "window_start", "window_end"].sort().join(",")) return false;
  if (!RUN_BINDING_HASHES.every((field) => isHash(value[field]))) return false;
  const start = Date.parse(String(value.window_start));
  const end = Date.parse(String(value.window_end));
  return (
    typeof value.window_start === "string" && typeof value.window_end === "string" &&
    Number.isFinite(start) && Number.isFinite(end) && start <= end &&
    value.reference_request_hash === referenceRequestHash
  );
}

function isDeclaredScopeFor(payload: Record<string, unknown>, version: string): boolean {
  const scope = payload.declared_scope;
  if (!isRecord(scope) || scope.scope_schema_version !== DEVIATION_SCOPE_VERSIONS[version]) return false;
  if (!isHash(scope.scope_hash)) return false;
  if (
    !["validation_profile", "venue", "symbol", "timeframe", "research_class"].every(
      (field) => typeof scope[field] === "string",
    )
  ) return false;
  if (!isHash(scope.validation_profile_hash) || !isHash(scope.validation_report_hash)) return false;
  if (version === "2.1.0") return isRunBinding(scope.run_binding, payload.reference_request_hash);
  return !("run_binding" in scope);
}

export function inspectReportDTO(report: ReportEnvelope): ReportDTOView {
  const payload: unknown = report.payload;
  if (!isRecord(payload)) return { status: "invalid", version: "?", reason: "payload must be an object" };
  const rule = RULES[report.kind];
  const rawVersion = payload.schema_version;
  if (rule.hasPayloadSchemaVersion === false && rawVersion !== undefined) {
    return { status: "invalid", version: "?", reason: "payload must not carry schema_version" };
  }
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
  if (report.kind === "paper_deviation" && version in DEVIATION_SCOPE_VERSIONS) {
    if (!["declared_scope", "summary", "marks"].every((field) => field in payload)) {
      return { status: "invalid", version, reason: `${version} requires scope-bound report fields` };
    }
    if (!isDeclaredScopeFor(payload, version)) {
      return {
        status: "invalid",
        version,
        reason: `declared_scope does not match scope DTO ${DEVIATION_SCOPE_VERSIONS[version]}`,
      };
    }
  }
  return { status: "supported", version };
}
