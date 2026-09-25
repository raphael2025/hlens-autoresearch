import { useEffect, useMemo, useRef, useState } from "react";
import { listReports, type ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { echarts } from "../lib/echarts";

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function ResearchLoop() {
  const [rounds, setRounds] = useState<ReportEnvelope[]>([]);
  const [error, setError] = useState<string | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    listReports("research_loop_round")
      .then((items) => setRounds([...items].sort((a, b) => a.created.localeCompare(b.created))))
      .catch((err) => setError(String(err)));
  }, []);

  const budgetSeries = useMemo(
    () =>
      rounds.map((round) => ({
        id: round.id,
        budget: asNumber(round.payload?.budget_used) ?? 0,
      })),
    [rounds],
  );

  useEffect(() => {
    if (chartRef.current === null) return;
    const chart = echarts.init(chartRef.current);
    chart.setOption({
      xAxis: { type: "category", data: budgetSeries.map((s) => s.id) },
      yAxis: { type: "value", name: "budget used" },
      series: [{ type: "bar", data: budgetSeries.map((s) => s.budget) }],
      tooltip: { trigger: "axis" },
      grid: { left: 48, right: 16, top: 16, bottom: 48 },
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [budgetSeries]);

  return (
    <section>
      <SimulatedBanner />
      <h2>研究循环（Research Loop）</h2>
      {error && <p style={{ color: "crimson" }}>{error}</p>}
      {rounds.length === 0 && <p>（无 round 记录 — 未配置报告目录或目录为空）</p>}
      {rounds.length > 0 && (
        <div ref={chartRef} style={{ width: "100%", height: 280, margin: "16px 0" }} />
      )}
      <table>
        <thead>
          <tr>
            <th>round id</th>
            <th>created</th>
            <th>budget used</th>
            <th>failures</th>
          </tr>
        </thead>
        <tbody>
          {rounds.map((round) => {
            const failures = asArray(round.payload?.failures);
            return (
              <tr key={round.id}>
                <td>{round.id}</td>
                <td>{round.created}</td>
                <td>{String(round.payload?.budget_used ?? "—")}</td>
                <td>{failures.length}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
