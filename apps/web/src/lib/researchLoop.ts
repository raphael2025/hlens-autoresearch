// View model of the Research Loop page: one row per `research_loop_round` report, read from the
// fields the payload really has — `core.contracts.loop_audit.LoopRoundRecord` (ADR-0050):
// loop_id, round_index, as_of, status, stages[{name, status, error, ...}], transitions,
// round_usage / total_usage ({trials, llm_cost_units, compute_seconds}; decimals as strings),
// overrun ({stage, amount} | null), previous_hash. (There is no `budget_used` or `failures` key:
// the page used to read those and always showed 0.) apps/api only serves a round that validates
// against that contract, but the page still parses defensively: a missing field shows "—".
// Pure: tested by researchLoop.test.ts with `node --test` over apps/web/fixtures.
import type { ReportEnvelope } from "../api";

export type LoopUsage = {
  trials: number | null;
  llm_cost_units: number | null;
  compute_seconds: number | null;
};

/** A stage that did not complete (anything but COMPLETED / SKIPPED), with its error if any. */
export type StageProblem = { name: string; status: string; error: string | null };

export type RoundRow = {
  id: string;
  created: string;
  loopId: string;
  roundIndex: number | null;
  asOf: string;
  status: string;
  stagesRun: number;
  stagesSkipped: number;
  problems: StageProblem[];
  transitions: number;
  roundUsage: LoopUsage;
  totalUsage: LoopUsage;
  /** `"<stage>"` of the first overrunning stage, `null` when none. */
  overrunStage: string | null;
};

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value !== "" ? value : fallback;
}

/** A JSON number or a decimal string (the audit writes Decimals as strings); else `null`. */
export function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

export function usageOf(value: unknown): LoopUsage {
  const usage = record(value);
  return {
    trials: asNumber(usage.trials),
    llm_cost_units: asNumber(usage.llm_cost_units),
    compute_seconds: asNumber(usage.compute_seconds),
  };
}

export function roundRow(envelope: ReportEnvelope): RoundRow {
  const payload = record(envelope.payload);
  const stages = Array.isArray(payload.stages) ? payload.stages.map(record) : [];
  const problems: StageProblem[] = stages
    .filter((stage) => stage.status !== "COMPLETED" && stage.status !== "SKIPPED")
    .map((stage) => ({
      name: text(stage.name),
      status: text(stage.status),
      error: typeof stage.error === "string" ? stage.error : null,
    }));
  const overrun = typeof payload.overrun === "object" && payload.overrun !== null ? record(payload.overrun) : null;
  return {
    id: envelope.id,
    created: envelope.created,
    loopId: text(payload.loop_id),
    roundIndex: typeof payload.round_index === "number" ? payload.round_index : null,
    asOf: text(payload.as_of),
    status: text(payload.status),
    stagesRun: stages.filter((stage) => stage.status !== "SKIPPED").length,
    stagesSkipped: stages.filter((stage) => stage.status === "SKIPPED").length,
    problems,
    transitions: Array.isArray(payload.transitions) ? payload.transitions.length : 0,
    roundUsage: usageOf(payload.round_usage),
    totalUsage: usageOf(payload.total_usage),
    overrunStage: overrun === null ? null : text(overrun.stage),
  };
}

/** Rows in round order: by loop, then round index (unknown last), then creation time. */
export function roundRows(reports: readonly ReportEnvelope[]): RoundRow[] {
  return reports.map(roundRow).sort(
    (a, b) =>
      a.loopId.localeCompare(b.loopId) ||
      (a.roundIndex ?? Number.MAX_SAFE_INTEGER) - (b.roundIndex ?? Number.MAX_SAFE_INTEGER) ||
      a.created.localeCompare(b.created),
  );
}

export const USAGE_KEYS = ["trials", "llm_cost_units", "compute_seconds"] as const;

/** Chart series: per usage key, each round's charged usage (`null` where missing). */
export function usageSeries(rows: readonly RoundRow[]): {
  labels: string[];
  series: { key: (typeof USAGE_KEYS)[number]; round: (number | null)[]; total: (number | null)[] }[];
} {
  return {
    labels: rows.map((row) => `${row.loopId}#${row.roundIndex ?? "?"}`),
    series: USAGE_KEYS.map((key) => ({
      key,
      round: rows.map((row) => row.roundUsage[key]),
      total: rows.map((row) => row.totalUsage[key]),
    })),
  };
}

export function formatUsage(usage: LoopUsage): string {
  const show = (value: number | null) => (value === null ? "—" : String(value));
  return `trials ${show(usage.trials)} · llm ${show(usage.llm_cost_units)} · compute ${show(usage.compute_seconds)}s`;
}
