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

/**
 * The three usage dimensions and their units (apps/worker/loop.py `StageUsage`: an integer trial
 * count, abstract LLM cost units — no currency — and compute seconds). They are not commensurable,
 * so each gets its own chart and scale (never one shared numeric axis).
 */
export const USAGE_DIMENSIONS = [
  { key: "trials", unit: "count" },
  { key: "llm_cost_units", unit: "cost units" },
  { key: "compute_seconds", unit: "s" },
] as const;

export const USAGE_KEYS = USAGE_DIMENSIONS.map((dimension) => dimension.key);

export type UsageKey = (typeof USAGE_DIMENSIONS)[number]["key"];

/** One dimension's chart data: each round's charged usage and the loop's cumulative total. */
export type UsageSeries = {
  key: UsageKey;
  unit: string;
  /** `"<key> (<unit>)"`, the chart's title. */
  title: string;
  round: (number | null)[];
  total: (number | null)[];
};

/** Chart series: per usage dimension, each round's usage and the running total (`null` where missing). */
export function usageSeries(rows: readonly RoundRow[]): { labels: string[]; series: UsageSeries[] } {
  return {
    labels: rows.map((row) => `${row.loopId}#${row.roundIndex ?? "?"}`),
    series: USAGE_DIMENSIONS.map(({ key, unit }) => ({
      key,
      unit,
      title: `${key} (${unit})`,
      round: rows.map((row) => row.roundUsage[key]),
      total: rows.map((row) => row.totalUsage[key]),
    })),
  };
}

/** `value` with its unit, for a tooltip ("—" when missing). */
export function withUnit(value: unknown, unit: string): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value} ${unit}` : "—";
}

type AxisOption = { type: "value"; name: string; position: "left" | "right"; alignTicks: boolean };
type SeriesOption = { name: string; type: "bar" | "line"; yAxisIndex: 0 | 1; data: (number | null)[] };

// No `title` component (not registered in src/lib/echarts.ts): the page captions each chart with
// `UsageSeries.title` in HTML.
export type UsageChartOption = {
  legend: { top: number; right: number };
  tooltip: { trigger: "axis"; valueFormatter: (value: unknown) => string };
  xAxis: { type: "category"; data: string[]; name: string };
  yAxis: [AxisOption, AxisOption];
  series: [SeriesOption, SeriesOption];
  grid: { left: number; right: number; top: number; bottom: number };
};

/**
 * The ECharts option of one dimension's chart: the round's usage as bars on the left axis and the
 * cumulative total as a line on the right axis — two scales, so a growing total never flattens the
 * per-round bars. Both axes and the tooltip carry the dimension's unit.
 */
export function usageChartOption(labels: readonly string[], series: UsageSeries): UsageChartOption {
  return {
    legend: { top: 0, right: 0 },
    tooltip: { trigger: "axis", valueFormatter: (value) => withUnit(value, series.unit) },
    xAxis: { type: "category", data: [...labels], name: "round" },
    yAxis: [
      { type: "value", name: `per round (${series.unit})`, position: "left", alignTicks: true },
      { type: "value", name: `cumulative (${series.unit})`, position: "right", alignTicks: true },
    ],
    series: [
      { name: "round usage", type: "bar", yAxisIndex: 0, data: series.round },
      { name: "total usage (cumulative)", type: "line", yAxisIndex: 1, data: series.total },
    ],
    grid: { left: 72, right: 72, top: 40, bottom: 40 },
  };
}

export function formatUsage(usage: LoopUsage): string {
  const show = (value: number | null) => (value === null ? "—" : String(value));
  return `trials ${show(usage.trials)} · llm ${show(usage.llm_cost_units)} · compute ${show(usage.compute_seconds)}s`;
}
