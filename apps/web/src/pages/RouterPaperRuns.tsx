import { useEffect, useMemo, useRef } from "react";
import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { EligibilityEvidence, ValidationReportBinding } from "../components/EligibilityEvidence";
import { ReportBrowser } from "../components/ReportBrowser";
import { echarts } from "../lib/echarts";
import {
  asRouterPayload,
  equityChartOption,
  equitySeries,
  runLabel,
  shortTime,
  weightsChartOption,
  weightSeries,
  type RouterPayload,
} from "../lib/routerPaperRun";

// Parsing and the chart series live in src/lib/routerPaperRun.ts (node-tested over the fixture).

function WeightsTimelineChart({ payload }: { payload: RouterPayload }) {
  const chartRef = useRef<HTMLDivElement | null>(null);
  const series = useMemo(() => weightSeries(payload), [payload]);

  useEffect(() => {
    if (chartRef.current === null || payload.decisions.length === 0) return;
    const chart = echarts.init(chartRef.current);
    chart.setOption(weightsChartOption(series));
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [payload, series]);

  return <div ref={chartRef} style={{ width: "100%", height: 280, margin: "16px 0" }} />;
}

function EquityChart({ payload }: { payload: RouterPayload }) {
  const chartRef = useRef<HTMLDivElement | null>(null);
  const series = useMemo(() => equitySeries(payload), [payload]);

  useEffect(() => {
    if (chartRef.current === null || payload.gross_equity_curve.length === 0) return;
    const chart = echarts.init(chartRef.current);
    chart.setOption(equityChartOption(series));
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [payload, series]);

  return <div ref={chartRef} style={{ width: "100%", height: 280, margin: "16px 0" }} />;
}

function RunDetail({ envelope }: { envelope: ReportEnvelope }) {
  const payload = asRouterPayload(envelope.payload);
  if (payload === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }

  return (
    <>
      <p>
        <strong>{payload.router}</strong> · run_hash <code>{payload.run_hash.slice(0, 12)}…</code>
      </p>
      <p>
        initial_equity {payload.initial_equity} · final_equity {payload.final_equity} · pnl{" "}
        <strong>{payload.pnl}</strong> · total_switching_cost {payload.total_switching_cost}
      </p>
      <ValidationReportBinding payload={envelope.payload} />
      <EligibilityEvidence payload={envelope.payload} />
      <h3>Weights / switch timeline</h3>
      <WeightsTimelineChart payload={payload} />
      <h3>Equity: gross vs. net of switching cost</h3>
      <EquityChart payload={payload} />
      <h3>Decisions</h3>
      <table>
        <thead>
          <tr>
            <th>at</th>
            <th>state</th>
            <th>weights</th>
            <th>turnover</th>
            <th>switching_cost</th>
          </tr>
        </thead>
        <tbody>
          {payload.decisions.map((decision) => (
            <tr key={decision.at}>
              <td>{shortTime(decision.at)}</td>
              <td>{decision.state ?? "(none)"}</td>
              <td>
                {Object.entries(decision.weights)
                  .map(([key, value]) => `${key}: ${value}`)
                  .join(", ") || "—"}
              </td>
              <td>{decision.turnover}</td>
              <td>{decision.switching_cost}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Switching charges</h3>
      <table>
        <thead>
          <tr>
            <th>decision_time</th>
            <th>rate</th>
            <th>equity_base</th>
            <th>amount</th>
            <th>charged_at</th>
          </tr>
        </thead>
        <tbody>
          {payload.charges.map((charge) => (
            <tr key={charge.decision_time}>
              <td>{shortTime(charge.decision_time)}</td>
              <td>{charge.rate}</td>
              <td>{charge.equity_base}</td>
              <td>{charge.amount}</td>
              <td>{charge.charged_at === null ? "（未支付：其后无 mark）" : shortTime(charge.charged_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function RouterPaperRuns() {
  return (
    <section>
      <SimulatedBanner />
      <h2>路由纸面运行（Router Paper Runs）</h2>
      <p style={{ color: "#664d03", fontWeight: 600 }}>
        PAPER ONLY — 纯纸面模拟；没有任何下单、账户或转账能力（H10）。
      </p>
      <ReportBrowser
        kind="router_paper_run"
        empty="（无路由运行报告 — 未配置报告目录或目录为空）"
        prompt="选择一次运行查看详情。"
        label={(run) => runLabel(run.id, run.payload)}
        renderDetail={(detail) => <RunDetail envelope={detail} />}
        listWidth={260}
      />
    </section>
  );
}
