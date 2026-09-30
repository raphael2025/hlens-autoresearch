// View model of the Router Paper Runs page. Payload shape written by research/reports/router.py
// (write_router_paper_run), identity-checked by apps/api/store.py (`run_hash`) — hand-typed since
// /reports/{kind} has no per-kind OpenAPI schema (ReportEnvelope.payload is `dict[str, Any]`).
// The optional `validation_reports` / `eligibility` keys are read by src/lib/routerEligibility.ts.
// Pure: tested by routerPaperRun.test.ts with `node --test` over apps/web/fixtures.
import type { EChartsOption } from "echarts";

export type RouterDecision = {
  at: string;
  state: string | null;
  weights: Record<string, string>;
  turnover: string;
  switching_cost: string;
};

export type RouterCharge = {
  decision_time: string;
  turnover: string;
  rate: string;
  equity_base: string;
  amount: string;
  charged_at: string | null;
};

export type RouterEquityPoint = {
  time: string;
  cash: string;
  equity: string;
  gross_exposure: string;
};

export type RouterPayload = {
  router: string;
  router_spec_hash: string;
  state_result_hash: string;
  strategy_result_hashes: Record<string, string>;
  decisions: RouterDecision[];
  charges: RouterCharge[];
  total_switching_cost: string;
  request_hash: string;
  gross_result_hash: string;
  result_hash: string;
  initial_equity: string;
  final_equity: string;
  pnl: string;
  gross_equity_curve: RouterEquityPoint[];
  net_equity_curve: RouterEquityPoint[];
  run_hash: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isDecision(value: unknown): boolean {
  return isRecord(value) && typeof value.at === "string" && isRecord(value.weights);
}

function isPoint(value: unknown): boolean {
  return isRecord(value) && typeof value.time === "string";
}

/** The payload as a router paper run (hashes, decisions, charges and both curves), else `null`. */
export function asRouterPayload(payload: Record<string, unknown> | undefined): RouterPayload | null {
  if (
    payload === undefined ||
    typeof payload.router !== "string" ||
    typeof payload.run_hash !== "string" ||
    !Array.isArray(payload.decisions) ||
    !payload.decisions.every(isDecision) ||
    !Array.isArray(payload.charges) ||
    !payload.charges.every(isRecord) ||
    !Array.isArray(payload.gross_equity_curve) ||
    !payload.gross_equity_curve.every(isPoint) ||
    !Array.isArray(payload.net_equity_curve) ||
    !payload.net_equity_curve.every(isPoint)
  ) {
    return null;
  }
  return payload as unknown as RouterPayload;
}

/** A decimal string as a chart number; absent / unparseable is 0 (a weight not held). */
export function chartNumber(value: string | undefined): number {
  const n = Number(value ?? "0");
  return Number.isFinite(n) ? n : 0;
}

/** "2026-01-01T00:01:00+00:00" -> "2026-01-01 00:01:00Z" (readable axis, nothing dropped). */
export function shortTime(iso: string): string {
  return iso.replace("T", " ").replace(/\+00:00$/, "Z");
}

/** Every strategy key any decision weights, sorted (one stacked series each). */
export function strategyKeys(run: RouterPayload): string[] {
  return Array.from(new Set(run.decisions.flatMap((decision) => Object.keys(decision.weights)))).sort();
}

/** Stacked weight series per strategy and the turnover bars, in decision order. */
export function weightSeries(run: RouterPayload): {
  categories: string[];
  weights: { key: string; data: number[] }[];
  turnover: number[];
} {
  return {
    categories: run.decisions.map((decision) => shortTime(decision.at)),
    weights: strategyKeys(run).map((key) => ({
      key,
      data: run.decisions.map((decision) => chartNumber(decision.weights[key])),
    })),
    turnover: run.decisions.map((decision) => chartNumber(decision.turnover)),
  };
}

/** Gross (before switching cost) and net equity per mark. */
export function equitySeries(run: RouterPayload): { categories: string[]; gross: number[]; net: number[] } {
  return {
    categories: run.gross_equity_curve.map((point) => shortTime(point.time)),
    gross: run.gross_equity_curve.map((point) => chartNumber(point.equity)),
    net: run.net_equity_curve.map((point) => chartNumber(point.equity)),
  };
}

/** ECharts option used by the Router Paper Runs decision-weight / turnover chart. */
export function weightsChartOption(series: ReturnType<typeof weightSeries>): EChartsOption {
  return {
    tooltip: { trigger: "axis" as const },
    legend: { top: 0 },
    grid: { left: 56, right: 56, top: 40, bottom: 48 },
    xAxis: { type: "category" as const, data: series.categories, name: "decision time" },
    yAxis: [
      { type: "value" as const, name: "weight" },
      { type: "value" as const, name: "turnover" },
    ],
    series: [
      ...series.weights.map((weight) => ({
        name: weight.key,
        type: "line" as const,
        stack: "weights",
        areaStyle: {},
        data: weight.data,
      })),
      {
        name: "turnover (switch)",
        type: "bar" as const,
        yAxisIndex: 1,
        data: series.turnover,
        itemStyle: { color: "#c2410c", opacity: 0.5 },
      },
    ],
  };
}

/** ECharts option used by the Router Paper Runs gross / net equity chart. */
export function equityChartOption(series: ReturnType<typeof equitySeries>): EChartsOption {
  return {
    tooltip: { trigger: "axis" as const },
    legend: { top: 0 },
    grid: { left: 64, right: 24, top: 40, bottom: 48 },
    xAxis: { type: "category" as const, data: series.categories, name: "time" },
    yAxis: { type: "value" as const, name: "equity", scale: true },
    series: [
      {
        name: "gross (before switching cost)",
        type: "line" as const,
        data: series.gross,
        itemStyle: { color: "#64748b" },
      },
      {
        name: "net (after switching cost)",
        type: "line" as const,
        data: series.net,
        itemStyle: { color: "#1d4ed8" },
      },
    ],
  };
}

/** The list button text: `<router> (pnl <pnl>)`, or the id when the payload does not parse. */
export function runLabel(id: string, payload: Record<string, unknown> | undefined): string {
  const run = asRouterPayload(payload);
  return run === null ? id : `${run.router} (pnl ${run.pnl})`;
}
