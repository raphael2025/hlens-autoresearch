import { useEffect, useMemo, useRef } from "react";
import { listReports, type ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView, InvalidReports } from "../components/States";
import { echarts } from "../lib/echarts";
import {
  formatUsage,
  roundRows,
  usageChartOption,
  usageSeries,
  type RoundRow,
  type UsageSeries,
} from "../lib/researchLoop";
import { useApi } from "../lib/useApi";

// Reads the real LoopRoundRecord fields (ADR-0050; src/lib/researchLoop.ts): round status, the
// stages that did not complete (with their error), charged round usage and cumulative total usage.
// Usage is charted per dimension (trials / llm_cost_units / compute_seconds): each its own small
// chart with its unit, round usage (bars, left axis) and cumulative total (line, right axis).

function UsageDimensionChart({ labels, series }: { labels: string[]; series: UsageSeries }) {
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (chartRef.current === null) return;
    const instance = echarts.init(chartRef.current);
    instance.setOption(usageChartOption(labels, series));
    const onResize = () => instance.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      instance.dispose();
    };
  }, [labels, series]);

  return (
    <figure style={{ margin: "8px 0 12px" }}>
      <figcaption style={{ fontSize: 13, color: "#555" }}>
        {series.title}：每轮用量（柱，左轴）· 累计用量（线，右轴）
      </figcaption>
      <div ref={chartRef} data-usage={series.key} style={{ width: "100%", height: 200 }} />
    </figure>
  );
}

function UsageCharts({ rows }: { rows: RoundRow[] }) {
  const chart = useMemo(() => usageSeries(rows), [rows]);
  return (
    <>
      {chart.series.map((series) => (
        <UsageDimensionChart key={series.key} labels={chart.labels} series={series} />
      ))}
    </>
  );
}

function RoundsTable({ rows }: { rows: RoundRow[] }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>loop / round</th>
            <th>as_of</th>
            <th>status</th>
            <th>stages run / skipped</th>
            <th>problem stages</th>
            <th>round usage</th>
            <th>total usage</th>
            <th>overrun</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td title={row.id}>
                {row.loopId} #{row.roundIndex ?? "?"}
              </td>
              <td>{row.asOf}</td>
              <td style={row.status !== "COMPLETED" ? { color: "crimson", fontWeight: 600 } : undefined}>
                {row.status}
              </td>
              <td>
                {row.stagesRun} / {row.stagesSkipped}
              </td>
              <td>
                {row.problems.length === 0
                  ? "—"
                  : row.problems.map((p) => (
                      <div key={p.name}>
                        {p.name}: {p.status}
                        {p.error !== null && <code> {p.error}</code>}
                      </div>
                    ))}
              </td>
              <td>{formatUsage(row.roundUsage)}</td>
              <td>{formatUsage(row.totalUsage)}</td>
              <td>{row.overrunStage ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Rounds({ reports }: { reports: ReportEnvelope[] }) {
  const rows = useMemo(() => roundRows(reports), [reports]);
  return (
    <>
      <UsageCharts rows={rows} />
      <RoundsTable rows={rows} />
    </>
  );
}

export function ResearchLoop() {
  const listing = useApi(() => listReports("research_loop_round"), []);

  return (
    <section>
      <SimulatedBanner />
      <h2>研究循环（Research Loop）</h2>
      {listing.status === "ok" && <InvalidReports invalid={listing.data.invalid} />}
      <AsyncView
        state={listing}
        what="round 记录"
        isEmpty={(data) => data.reports.length === 0}
        empty="（无 round 记录 — 未配置报告目录或目录为空）"
      >
        {(data) => <Rounds reports={data.reports} />}
      </AsyncView>
    </section>
  );
}
