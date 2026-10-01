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

type AxisOption = {
  type: "value";
  name: string;
  position: "left" | "right";
  alignTicks: boolean;
  nameLocation?: "middle";
  nameRotate?: number;
  nameGap?: number;
};
type SeriesOption = { name: string; type: "bar" | "line"; yAxisIndex: 0 | 1; data: (number | null)[] };

// No `title` component (not registered in src/lib/echarts.ts): the page captions each chart with
// `UsageSeries.title` in HTML.
export type UsageChartOption = {
  legend: { top: number; right: number };
  tooltip: { trigger: "axis"; valueFormatter: (value: unknown) => string };
  xAxis: { type: "category"; data: string[]; name: string; nameLocation: "middle"; nameGap: number };
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
    xAxis: { type: "category", data: [...labels], name: "round", nameLocation: "middle", nameGap: 24 },
    yAxis: [
      { type: "value", name: `per round (${series.unit})`, position: "left", alignTicks: true },
      {
        type: "value",
        name: `cumulative (${series.unit})`,
        position: "right",
        alignTicks: true,
        nameLocation: "middle",
        nameRotate: 90,
        nameGap: 36,
      },
    ],
    series: [
      { name: "round usage", type: "bar", yAxisIndex: 0, data: series.round },
      { name: "total usage (cumulative)", type: "line", yAxisIndex: 1, data: series.total },
    ],
    grid: { left: 72, right: 72, top: 40, bottom: 40 },
  };
}

// ---- P12 replacement trigger audit (ADR-0100 item 7; optional, default off) ---------------------
//
// research/loop/replacement.py `ReplacementTriggerStage.run`: when a loop runs with the optional
// trigger, the `evolution` stage summary carries `replacement_trigger` — `{"due": false}` in a
// round the trigger is not due, else the trigger summary (format_version 2): `status`
// (always PENDING_HUMAN_APPROVAL), `proposed_by`, `triggers` (one row per eligible candidate:
// phase open / evaluate, trial, window, opening, consumption, outcome `status` refused /
// window_opened / proposed / not_proposed / failed with its `refusal` / `error`, and the job
// payload whose `recorded` proposals are always PENDING_HUMAN_APPROVAL), `not_eligible`,
// `already_triggered`, `family_trials`, `unsealing_count`, `unsealing_budget`. A loop without the
// trigger has no such key: nothing is shown. Additive to the LoopRoundRecord DTO (stage summaries
// are free-form JSON in ADR-0050), so every older round reads exactly as before.

export const REPLACEMENT_TRIGGER_KEY = "replacement_trigger";
/** research/loop/replacement.py TRIGGER_FORMAT values this console reads. */
export const KNOWN_TRIGGER_FORMATS: readonly number[] = [2];
export const PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL";

export type TriggerProposal = {
  proposalHash: string | null;
  incumbent: string | null;
  candidate: string | null;
  status: string | null;
};

export type TriggerRow = {
  phase: string;
  candidate: string;
  candidateState: string | null;
  incumbents: string[];
  trial: string | null;
  trialHash: string | null;
  windowId: string | null;
  windowRange: string | null;
  windowProfile: string | null;
  openingHash: string | null;
  openedAt: string | null;
  consumptionHash: string | null;
  consumedReports: number | null;
  /** the row's outcome code: refused / window_opened / proposed / not_proposed / failed */
  outcome: string;
  refusal: string | null;
  error: string | null;
  proposals: TriggerProposal[];
  alreadyProposed: number;
  jobRefusals: { incumbent: string | null; candidate: string | null; reason: string | null }[];
};

export type ReplacementTriggerView = {
  id: string;
  loopId: string;
  roundIndex: number | null;
  asOf: string;
  due: boolean;
  formatVersion: number | null;
  formatKnown: boolean;
  status: string | null;
  proposedBy: string | null;
  rows: TriggerRow[];
  notEligible: { candidate: string | null; reason: string | null }[];
  alreadyTriggered: { candidate: string | null; hypothesis: string | null }[];
  familyTrials: number | null;
  unsealingCount: number | null;
  unsealingBudget: number | null;
  /** what does not read as this console expects (never hidden: shown as a warning) */
  problems: string[];
  /** the raw trigger summary, for the page's raw-JSON disclosure */
  raw: unknown;
};

function optText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function optInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function triggerRow(value: unknown, problems: string[]): TriggerRow {
  const row = record(value);
  const trial = record(row.trial);
  const window = typeof row.window === "object" && row.window !== null ? record(row.window) : null;
  const opening =
    typeof row.window_opening === "object" && row.window_opening !== null ? record(row.window_opening) : null;
  const consumption =
    typeof row.window_consumption === "object" && row.window_consumption !== null
      ? record(row.window_consumption)
      : null;
  const job = typeof row.job === "object" && row.job !== null ? record(row.job) : null;
  const error = typeof row.error === "object" && row.error !== null ? record(row.error) : null;
  const proposals = records(job?.recorded).map((proposal) => ({
    proposalHash: optText(proposal.proposal_hash),
    incumbent: optText(proposal.incumbent),
    candidate: optText(proposal.candidate),
    status: optText(proposal.status),
  }));
  const candidate = text(row.candidate, "?");
  for (const proposal of proposals) {
    if (proposal.status !== PENDING_HUMAN_APPROVAL) {
      problems.push(`${candidate}: proposal ${proposal.proposalHash ?? "?"} status is ${proposal.status ?? "missing"}, not ${PENDING_HUMAN_APPROVAL}`);
    }
  }
  if (job !== null && job.status !== PENDING_HUMAN_APPROVAL) {
    problems.push(`${candidate}: job status is ${optText(job.status) ?? "missing"}, not ${PENDING_HUMAN_APPROVAL}`);
  }
  const consumed = consumption === null ? null : consumption.report_hashes;
  const alreadyProposed = job === null ? null : job.already_proposed;
  const start = optText(window?.start);
  const end = optText(window?.end);
  const profileRef = optText(window?.profile_ref);
  return {
    phase: text(row.phase),
    candidate,
    candidateState: optText(row.candidate_state),
    incumbents: Array.isArray(row.incumbents) ? row.incumbents.filter((i): i is string => typeof i === "string") : [],
    trial: optText(trial.hypothesis),
    trialHash: optText(trial.hypothesis_hash),
    windowId: optText(window?.window_id) ?? optText(opening?.window_id),
    windowRange: start === null && end === null ? null : `[${start ?? "—"}, ${end ?? "—"})`,
    windowProfile: profileRef,
    openingHash: optText(row.window_opening_hash),
    openedAt: optText(opening?.opened_at),
    consumptionHash: optText(row.window_consumption_hash),
    consumedReports: Array.isArray(consumed) ? consumed.length : null,
    outcome: text(row.status),
    refusal: optText(row.refusal),
    error: error === null ? null : `${text(error.type, "Error")}: ${text(error.message, "")}`,
    proposals,
    alreadyProposed: Array.isArray(alreadyProposed) ? alreadyProposed.length : 0,
    jobRefusals: records(job?.refused).map((refused) => ({
      incumbent: optText(refused.incumbent),
      candidate: optText(refused.candidate),
      reason: optText(refused.reason),
    })),
  };
}

/**
 * The round's replacement-trigger summary, or `null` when the round carries none (no `evolution`
 * stage, or no `replacement_trigger` key in its summary: a loop without the trigger).
 */
export function replacementTriggerOf(envelope: ReportEnvelope): ReplacementTriggerView | null {
  const payload = record(envelope.payload);
  const stages = Array.isArray(payload.stages) ? payload.stages.map(record) : [];
  const evolution = stages.find((stage) => stage.name === "evolution");
  if (evolution === undefined) return null;
  const summary = record(evolution.summary);
  if (!(REPLACEMENT_TRIGGER_KEY in summary)) return null;
  const raw = summary[REPLACEMENT_TRIGGER_KEY];
  const trigger = record(raw);
  const problems: string[] = [];
  const due = trigger.due === true;
  if (typeof trigger.due !== "boolean") problems.push("replacement_trigger.due is not a boolean");
  const formatVersion = optInt(trigger.format_version);
  const formatKnown = formatVersion !== null && KNOWN_TRIGGER_FORMATS.includes(formatVersion);
  if (due && !formatKnown) {
    problems.push(`unrecognised trigger format_version ${String(trigger.format_version ?? "missing")}`);
  }
  const status = optText(trigger.status);
  if (due && status !== PENDING_HUMAN_APPROVAL) {
    problems.push(`trigger status is ${status ?? "missing"}, not ${PENDING_HUMAN_APPROVAL}`);
  }
  if (due && !Array.isArray(trigger.triggers)) problems.push("replacement_trigger.triggers is not a list");
  const rows = due ? (Array.isArray(trigger.triggers) ? trigger.triggers : []).map((row) => triggerRow(row, problems)) : [];
  return {
    id: envelope.id,
    loopId: text(payload.loop_id),
    roundIndex: typeof payload.round_index === "number" ? payload.round_index : null,
    asOf: text(payload.as_of),
    due,
    formatVersion,
    formatKnown,
    status,
    proposedBy: optText(trigger.proposed_by),
    rows,
    notEligible: records(trigger.not_eligible).map((item) => ({
      candidate: optText(item.candidate),
      reason: optText(item.reason),
    })),
    alreadyTriggered: records(trigger.already_triggered).map((item) => ({
      candidate: optText(item.candidate),
      hypothesis: optText(item.hypothesis),
    })),
    familyTrials: optInt(trigger.family_trials),
    unsealingCount: optInt(trigger.unsealing_count),
    unsealingBudget: optInt(trigger.unsealing_budget),
    problems,
    raw,
  };
}

/** Every round that carries a trigger summary, in round order (as `roundRows`). */
export function replacementTriggers(reports: readonly ReportEnvelope[]): ReplacementTriggerView[] {
  const order = new Map(roundRows(reports).map((row, index) => [row.id, index]));
  return reports
    .map(replacementTriggerOf)
    .filter((view): view is ReplacementTriggerView => view !== null)
    .sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
}

const OUTCOME_TEXT: Record<string, string> = {
  refused: "拒绝（refused）",
  window_opened: "已开封窗口（window_opened）— 本轮不使用证据",
  proposed: `已提出替换提案（proposed）— ${PENDING_HUMAN_APPROVAL}`,
  not_proposed: "未提出提案（not_proposed）",
  failed: "作业失败（failed）— 窗口保持已消耗",
};

/** The outcome code in words; an unknown code is shown verbatim. */
export function outcomeText(outcome: string): string {
  return OUTCOME_TEXT[outcome] ?? outcome;
}

export function formatUsage(usage: LoopUsage): string {
  const show = (value: number | null) => (value === null ? "—" : String(value));
  return `trials ${show(usage.trials)} · llm ${show(usage.llm_cost_units)} · compute ${show(usage.compute_seconds)}s`;
}
