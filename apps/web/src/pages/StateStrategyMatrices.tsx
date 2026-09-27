import { useEffect, useMemo, useRef } from "react";
import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import { echarts } from "../lib/echarts";
import {
  asMatrixPayload,
  heatmapGrid,
  matrixLabel,
  METRICS,
  stateLabel,
  totalSamples,
} from "../lib/stateStrategyMatrix";

// Parsing and the heatmap values live in src/lib/stateStrategyMatrix.ts (node-tested over the
// current and legacy readable 2.0.0 fixtures).

function MatrixDetail({ envelope }: { envelope: ReportEnvelope }) {
  const chartRef = useRef<HTMLDivElement | null>(null);
  const matrix = asMatrixPayload(envelope.payload);

  const grid = useMemo(
    () => (matrix === null ? { raw: [] as (number | null)[][], normalized: [] as (number | null)[][] } : heatmapGrid(matrix)),
    [matrix],
  );

  useEffect(() => {
    if (chartRef.current === null || matrix === null || matrix.cells.length === 0) return;
    const chart = echarts.init(chartRef.current);
    const categories = matrix.cells.map((cell) => stateLabel(cell.state));
    const data: [number, number, number | null][] = [];
    METRICS.forEach((_metric, row) => {
      grid.normalized[row].forEach((value, col) => {
        data.push([col, row, value]);
      });
    });
    chart.setOption({
      tooltip: {
        position: "top",
        formatter: (params: { value: [number, number, number | null] }) => {
          const [col, row] = params.value;
          const raw = grid.raw[row][col];
          return `${categories[col]} · ${METRICS[row].label}: ${raw ?? "—"}`;
        },
      },
      grid: { left: 90, right: 24, top: 16, bottom: 48 },
      xAxis: { type: "category", data: categories, splitArea: { show: true } },
      yAxis: { type: "category", data: METRICS.map((m) => m.label), splitArea: { show: true } },
      visualMap: {
        min: 0,
        max: 1,
        calculable: false,
        orient: "horizontal",
        left: "center",
        bottom: 0,
        text: ["high (row-relative)", "low"],
        inRange: { color: ["#f0f4ff", "#1d4ed8"] },
      },
      series: [
        {
          type: "heatmap",
          data,
          label: { show: false },
          emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,0.3)" } },
        },
      ],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [matrix, grid]);

  if (matrix === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }

  return (
    <>
      <p>
        <strong>{matrix.strategy}</strong> × <strong>{matrix.state}</strong> · matrix_hash{" "}
        <code>{matrix.matrix_hash.slice(0, 12)}…</code>
      </p>
      <p>
        样本总数（sample count）：<strong>{totalSamples(matrix)}</strong> · top_k {matrix.top_k} · best_state_share{" "}
        {matrix.best_state_share ?? "—"} · top_k_share_in_best_state{" "}
        {matrix.top_k_share_in_best_state ?? "—"}
      </p>
      {matrix.backtest_result_hash === null && matrix.state_result_hash === null && (
        <p style={{ color: "#664d03" }}>
          （无绑定的 P5 backtest / P2 state result 哈希 — 一个不带溯源的原始收益 x 状态矩阵）
        </p>
      )}
      <div ref={chartRef} style={{ width: "100%", height: 220, margin: "16px 0" }} />
      <table>
        <thead>
          <tr>
            <th>state</th>
            <th>count</th>
            <th>total</th>
            <th>mean</th>
            <th>hit_rate</th>
            <th>top_returns</th>
          </tr>
        </thead>
        <tbody>
          {matrix.cells.map((cell) => (
            <tr key={stateLabel(cell.state)}>
              <td>{stateLabel(cell.state)}</td>
              <td>{cell.count}</td>
              <td>{cell.total}</td>
              <td>{cell.mean ?? "—"}</td>
              <td>{cell.hit_rate ?? "—"}</td>
              <td>{cell.top_returns.join(", ") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function StateStrategyMatrices() {
  return (
    <section>
      <SimulatedBanner />
      <h2>状态 × 策略矩阵（State × Strategy Matrices）</h2>
      <ReportBrowser
        kind="state_strategy_matrix"
        empty="（无矩阵报告 — 未配置报告目录或目录为空）"
        prompt="选择一个矩阵查看详情。"
        label={(report) => matrixLabel(report.id, report.payload)}
        renderDetail={(detail) => <MatrixDetail envelope={detail} />}
        listWidth={260}
      />
    </section>
  );
}
