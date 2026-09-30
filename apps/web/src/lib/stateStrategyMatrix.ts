// View model of the State × Strategy Matrices page. Payload shape written by
// research/reports/matrix.py (write_state_strategy_matrix), DTO-shape checked by apps/api/store.py
// — hand-typed since /reports/{kind} has no per-kind OpenAPI schema (ReportEnvelope.payload is
// `dict[str, Any]`). Pure: tested by stateStrategyMatrix.test.ts with `node --test` over
// apps/web/fixtures (the current report and the legacy readable 2.0.0 one).
import type { EChartsOption, TooltipComponentFormatterCallbackParams } from "echarts";

export type MatrixCell = {
  state: string | null;
  count: number;
  total: string;
  mean: string | null;
  hit_rate: string | null;
  top_returns: string[];
};

export type MatrixPayload = {
  strategy: string;
  state: string;
  cells: MatrixCell[];
  best_state_share: string | null;
  top_k_share_in_best_state: string | null;
  top_k: number;
  backtest_result_hash: string | null;
  state_result_hash: string | null;
  matrix_hash: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCell(value: unknown): boolean {
  return (
    isRecord(value) &&
    (typeof value.state === "string" || value.state === null) &&
    typeof value.count === "number" &&
    Array.isArray(value.top_returns)
  );
}

/** The payload as a matrix (strategy / state / matrix_hash and well-shaped cells), else `null`. */
export function asMatrixPayload(payload: Record<string, unknown> | undefined): MatrixPayload | null {
  if (
    payload === undefined ||
    typeof payload.strategy !== "string" ||
    typeof payload.state !== "string" ||
    typeof payload.matrix_hash !== "string" ||
    !Array.isArray(payload.cells) ||
    !payload.cells.every(isCell)
  ) {
    return null;
  }
  return payload as unknown as MatrixPayload;
}

/** A decimal string as a number (`null` when absent or not finite). */
export function asNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value.trim() === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function stateLabel(state: string | null): string {
  return state ?? "(none)";
}

// One row per metric, normalized 0..1 within the row (min-max) so metrics on very different
// scales (an integer count vs. a fractional mean / hit rate) share one heatmap color scale; the
// tooltip always shows the raw value, never the normalized one.
export const METRICS: readonly { key: "count" | "mean" | "hit_rate" | "total"; label: string }[] = [
  { key: "count", label: "count" },
  { key: "mean", label: "mean" },
  { key: "hit_rate", label: "hit_rate" },
  { key: "total", label: "total" },
];

export type MetricKey = (typeof METRICS)[number]["key"];

export function metricValue(cell: MatrixCell, key: MetricKey): number | null {
  if (key === "count") return cell.count;
  return asNumber(cell[key]);
}

export function normalizeRow(values: readonly (number | null)[]): (number | null)[] {
  const finite = values.filter((v): v is number => v !== null);
  if (finite.length === 0) return values.map(() => null);
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  if (min === max) return values.map((v) => (v === null ? null : 0.5));
  return values.map((v) => (v === null ? null : (v - min) / (max - min)));
}

/** Raw (per METRICS row, per cell) and row-normalized heatmap values. */
export function heatmapGrid(matrix: MatrixPayload): { raw: (number | null)[][]; normalized: (number | null)[][] } {
  const raw = METRICS.map((metric) => matrix.cells.map((cell) => metricValue(cell, metric.key)));
  return { raw, normalized: raw.map(normalizeRow) };
}

/** ECharts option used by the State × Strategy Matrices page heatmap. */
export function heatmapChartOption(
  matrix: MatrixPayload,
  grid: ReturnType<typeof heatmapGrid>,
): EChartsOption {
  const categories = matrix.cells.map((cell) => stateLabel(cell.state));
  const data: [number, number, number | null][] = [];
  METRICS.forEach((_metric, row) => {
    grid.normalized[row].forEach((value, col) => {
      data.push([col, row, value]);
    });
  });
  return {
    tooltip: {
      position: "top",
      formatter: (params: TooltipComponentFormatterCallbackParams) => {
        const item = Array.isArray(params) ? params[0] : params;
        if (!Array.isArray(item?.value) || item.value.length < 3) return "";
        const [col, row] = item.value as [number, number, number | null];
        const label = categories[col];
        const metric = METRICS[row];
        if (label === undefined || metric === undefined) return "";
        const raw = grid.raw[row]?.[col];
        return `${label} · ${metric.label}: ${raw ?? "—"}`;
      },
    },
    grid: { left: 90, right: 24, top: 16, bottom: 72 },
    xAxis: { type: "category" as const, data: categories, splitArea: { show: true } },
    yAxis: { type: "category" as const, data: METRICS.map((m) => m.label), splitArea: { show: true } },
    visualMap: {
      min: 0,
      max: 1,
      calculable: false,
      orient: "horizontal" as const,
      left: "center",
      bottom: 0,
      text: ["high (row-relative)", "low"],
      inRange: { color: ["#f0f4ff", "#1d4ed8"] },
    },
    series: [
      {
        type: "heatmap" as const,
        data,
        label: { show: false },
        emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,0.3)" } },
      },
    ],
  };
}

export function totalSamples(matrix: MatrixPayload): number {
  return matrix.cells.reduce((sum, cell) => sum + cell.count, 0);
}

/** The list button text: `<strategy> × <state> (n=<samples>)`, or the id when unparsed. */
export function matrixLabel(id: string, payload: Record<string, unknown> | undefined): string {
  const matrix = asMatrixPayload(payload);
  return matrix === null ? id : `${matrix.strategy} × ${matrix.state} (n=${totalSamples(matrix)})`;
}
