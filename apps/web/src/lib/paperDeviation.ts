// View model of the Paper Deviation page. Payload shape written by research/reports/deviation.py
// (write_paper_deviation), i.e. research/router/deviation.py's PaperDeviation.to_payload(): the
// router's net paper result vs a reference backtest the caller declared, mark by mark, plus
// summary statistics. Decimals as exact text (`null` where not computable, never 0), times as
// ISO-8601 UTC. Descriptive only — no threshold, no verdict. Hand-typed since /reports/{kind} has
// no per-kind OpenAPI schema. Pure: tested by paperDeviation.test.ts with `node --test`.
import type { EChartsOption } from "echarts";

export type DeviationMarkPayload = {
  time: string;
  paper_equity: string;
  reference_equity: string;
  equity_difference: string;
  paper_return: string | null;
  reference_return: string | null;
  return_difference: string | null;
};

export type DeviationSummaryPayload = {
  marks: number;
  initial_equity: string;
  final_equity_difference: string;
  mean_equity_difference: string;
  max_abs_equity_difference: string;
  max_abs_equity_difference_at: string;
  paper_total_return: string;
  reference_total_return: string;
  total_return_difference: string;
  return_marks: number;
  mean_return_difference: string | null;
  mean_abs_return_difference: string | null;
  tracking_error: string | null;
};

export type RunBindingPayload = {
  router_spec_hash: string;
  router_strategy_spec_hash: string;
  experiment_hash: string;
  bars_hash: string;
  window_start: string;
  window_end: string;
  cost_model_hash: string;
  reference_request_hash: string;
};

export type DeclaredScopePayload = {
  scope_schema_version: "1.0.0" | "1.1.0";
  validation_profile: string;
  validation_profile_hash: string;
  validation_report_hash: string;
  venue: string;
  symbol: string;
  timeframe: string;
  research_class: string;
  /** Scope 1.1.0 only (ADR-0104): what the deviation is bound to besides the P8 scope. */
  run_binding?: RunBindingPayload;
  scope_hash: string;
};

export type PaperDeviationPayload = {
  kind: string;
  schema_version: string;
  status: string;
  note: string;
  router: string;
  run_hash: string;
  paper_result_hash: string;
  reference_result_hash: string;
  reference_request_hash: string;
  reference_provider: string;
  declared_scope?: DeclaredScopePayload;
  instruments: string[];
  marks: DeviationMarkPayload[];
  summary: DeviationSummaryPayload;
  deviation_hash: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const HASH = /^[0-9a-f]{64}$/;
const isHash = (value: unknown): boolean => typeof value === "string" && HASH.test(value);
const RUN_BINDING_HASHES = [
  "router_spec_hash",
  "router_strategy_spec_hash",
  "experiment_hash",
  "bars_hash",
  "cost_model_hash",
  "reference_request_hash",
] as const;

/** Payload version -> the declared-scope version it carries (ADR-0079 / ADR-0104). */
export const DEVIATION_SCOPE_VERSIONS: Record<string, string> = { "2.0.0": "1.0.0", "2.1.0": "1.1.0" };

/** Shape of a scope 1.1.0 `run_binding`; the console does not re-derive or re-hash it. */
export function isRunBinding(value: unknown, referenceRequestHash: unknown): boolean {
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

/**
 * Whether `payload.declared_scope` is a well-formed scope for the payload `version` (2.0.0 -> scope
 * 1.0.0, no run binding; 2.1.0 -> scope 1.1.0 with a run binding). Shape only: the hashes are
 * verified server-side by apps/api.
 */
export function isDeclaredScopeFor(payload: Record<string, unknown>, version: string): boolean {
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

/**
 * What the report is bound to (ADR-0104): `run_bound` (payload 2.1.0, scope 1.1.0: router spec,
 * experiment, bars, window, cost model, reference request), `scope_only` (2.0.0: only the P8
 * scope; legacy, not comparable evidence) or `unscoped` (1.0.0: descriptive, no scope at all).
 */
export type BindingKind = "run_bound" | "scope_only" | "unscoped";

export function bindingKind(report: PaperDeviationPayload): BindingKind {
  if (report.schema_version === "2.1.0") return "run_bound";
  return report.schema_version === "2.0.0" ? "scope_only" : "unscoped";
}

export const BINDING_LABELS: Record<BindingKind, string> = {
  run_bound: "带运行绑定",
  scope_only: "scope-only（legacy，无运行绑定，不可作为可比证据）",
  unscoped: "无范围绑定（legacy 1.0.0，仅描述）",
};

export type BindingRow = { label: string; value: string };

/** The run binding as label / exact value rows (`[]` unless the report is run-bound). */
export function runBindingRows(report: PaperDeviationPayload): BindingRow[] {
  const binding = report.declared_scope?.run_binding;
  if (bindingKind(report) !== "run_bound" || binding === undefined) return [];
  return [
    { label: "router_spec_hash", value: binding.router_spec_hash },
    { label: "router_strategy_spec_hash", value: binding.router_strategy_spec_hash },
    { label: "experiment_hash", value: binding.experiment_hash },
    { label: "bars_hash", value: binding.bars_hash },
    { label: "window", value: `${binding.window_start} → ${binding.window_end}` },
    { label: "cost_model_hash", value: binding.cost_model_hash },
    { label: "reference_request_hash", value: binding.reference_request_hash },
  ];
}

export function asPaperDeviationPayload(
  payload: Record<string, unknown> | undefined,
): PaperDeviationPayload | null {
  if (
    payload === undefined ||
    payload.kind !== "paper_deviation" ||
    !["1.0.0", "2.0.0", "2.1.0"].includes(String(payload.schema_version)) ||
    typeof payload.deviation_hash !== "string" ||
    !Array.isArray(payload.marks) ||
    !isRecord(payload.summary)
  ) {
    return null;
  }
  const version = String(payload.schema_version);
  if (version in DEVIATION_SCOPE_VERSIONS && !isDeclaredScopeFor(payload, version)) return null;
  return payload as unknown as PaperDeviationPayload;
}

/**
 * Exact decimal text for display: a Python `Decimal` string such as "0E-18" (a quantized zero) or
 * "-10.000000000000000000" is shown without its exponent or trailing zeros ("0", "-10"); `null`
 * (not computable) is "—". Never rounds: the digits shown are the payload's.
 */
export function decimalText(value: string | null): string {
  if (value === null) return "—";
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:E([+-]?\d+))?$/i.exec(value);
  if (match === null) return value; // not a plain decimal: shown verbatim
  const [, sign, whole, fraction = "", exponentText = "0"] = match;
  const exponent = Number(exponentText);
  let digits = whole + fraction;
  let point = whole.length + exponent; // position of the decimal point within `digits`
  if (point <= 0) {
    digits = "0".repeat(1 - point) + digits;
    point = 1;
  } else if (point > digits.length) {
    digits = digits + "0".repeat(point - digits.length);
  }
  const integer = digits.slice(0, point).replace(/^0+(?=\d)/, "");
  const decimals = digits.slice(point).replace(/0+$/, "");
  const text = decimals === "" ? integer : `${integer}.${decimals}`;
  return /^0(\.0*)?$/.test(text) ? "0" : `${sign}${text}`;
}

/** A return (a fraction) as a percentage with `places` decimals, for labels only; "—" if null. */
export function percent(value: string | null, places = 4): string {
  if (value === null) return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(places)}%` : value;
}

export type SummaryRow = { label: string; value: string };

/** The summary as label / exact value rows, in a fixed reading order. */
export function summaryRows(summary: DeviationSummaryPayload): SummaryRow[] {
  return [
    { label: "marks", value: String(summary.marks) },
    { label: "initial_equity", value: decimalText(summary.initial_equity) },
    { label: "final_equity_difference（paper − reference）", value: decimalText(summary.final_equity_difference) },
    { label: "mean_equity_difference", value: decimalText(summary.mean_equity_difference) },
    {
      label: "max_abs_equity_difference",
      value: `${decimalText(summary.max_abs_equity_difference)} @ ${summary.max_abs_equity_difference_at}`,
    },
    { label: "paper_total_return", value: decimalText(summary.paper_total_return) },
    { label: "reference_total_return", value: decimalText(summary.reference_total_return) },
    { label: "total_return_difference", value: decimalText(summary.total_return_difference) },
    { label: "return_marks", value: String(summary.return_marks) },
    { label: "mean_return_difference", value: decimalText(summary.mean_return_difference) },
    { label: "mean_abs_return_difference", value: decimalText(summary.mean_abs_return_difference) },
    { label: "tracking_error（样本标准差，n − 1）", value: decimalText(summary.tracking_error) },
  ];
}

export type ChartSeries = {
  times: string[];
  paper: number[];
  reference: number[];
  difference: number[];
};

/** Numbers for the chart only (the tables keep the exact text). */
export function chartSeries(report: PaperDeviationPayload): ChartSeries {
  const number = (text: string) => {
    const value = Number(text);
    return Number.isFinite(value) ? value : Number.NaN;
  };
  return {
    times: report.marks.map((mark) => mark.time),
    paper: report.marks.map((mark) => number(mark.paper_equity)),
    reference: report.marks.map((mark) => number(mark.reference_equity)),
    difference: report.marks.map((mark) => number(mark.equity_difference)),
  };
}

/** ECharts option used by the Paper Deviation page's mark-by-mark chart. */
export function deviationChartOption(report: PaperDeviationPayload): EChartsOption {
  const series = chartSeries(report);
  return {
    tooltip: { trigger: "axis" as const },
    legend: { top: 0 },
    grid: { left: 64, right: 64, top: 40, bottom: 48 },
    xAxis: { type: "category" as const, data: series.times, name: "time" },
    yAxis: [
      { type: "value" as const, name: "equity", scale: true },
      { type: "value" as const, name: "paper − reference" },
    ],
    series: [
      { name: "paper (router, net)", type: "line" as const, data: series.paper, itemStyle: { color: "#1d4ed8" } },
      { name: "reference", type: "line" as const, data: series.reference, itemStyle: { color: "#64748b" } },
      {
        name: "difference",
        type: "bar" as const,
        yAxisIndex: 1,
        data: series.difference,
        itemStyle: { color: "#c2410c", opacity: 0.5 },
      },
    ],
  };
}

export function deviationLabel(report: PaperDeviationPayload): string {
  return `${report.router} vs ${report.reference_provider} (Δ final ${decimalText(
    report.summary.final_equity_difference,
  )})`;
}
