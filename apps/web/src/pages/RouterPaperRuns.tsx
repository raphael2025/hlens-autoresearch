import { useEffect, useMemo, useRef } from "react";
import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { EligibilityEvidence, ValidationReportBinding } from "../components/EligibilityEvidence";
import { ReportBrowser } from "../components/ReportBrowser";
import { echarts } from "../lib/echarts";

// Payload shape written by research/reports/router.py (write_router_paper_run), read back opaquely
// by apps/api/store.py — hand-typed the same way ValidationReports.tsx types Gate (no per-kind
// OpenAPI schema; ReportEnvelope.payload is `dict[str, Any]`).
type RouterDecision = {
  at: string;
  state: string | null;
  weights: Record<string, string>;
  turnover: string;
  switching_cost: string;
};

type RouterCharge = {
  decision_time: string;
  turnover: string;
  rate: string;
  equity_base: string;
  amount: string;
  charged_at: string | null;
};

type RouterEquityPoint = {
  time: string;
  cash: string;
  equity: string;
  gross_exposure: string;
};

type RouterPayload = {
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

function asRouterPayload(payload: Record<string, unknown> | undefined): RouterPayload | null {
  if (payload === undefined || !Array.isArray(payload.decisions)) return null;
  return payload as unknown as RouterPayload;
}

function asNumber(value: string | undefined): number {
  const n = Number(value ?? "0");
  return Number.isFinite(n) ? n : 0;
}

function shortTime(iso: string): string {
  // "2026-01-01T00:01:00+00:00" -> "00:01:00" when same day is implied by context; keep it simple
  // and just drop the timezone suffix so the axis stays readable without losing information.
  return iso.replace("T", " ").replace(/\+00:00$/, "Z");
}

function WeightsTimelineChart({ payload }: { payload: RouterPayload }) {
  const chartRef = useRef<HTMLDivElement | null>(null);
  const strategyKeys = useMemo(
    () =>
      Array.from(new Set(payload.decisions.flatMap((decision) => Object.keys(decision.weights)))).sort(),
    [payload],
  );

  useEffect(() => {
    if (chartRef.current === null || payload.decisions.length === 0) return;
    const chart = echarts.init(chartRef.current);
    const categories = payload.decisions.map((decision) => shortTime(decision.at));
    const weightSeries = strategyKeys.map((key) => ({
      name: key,
      type: "line" as const,
      stack: "weights",
      areaStyle: {},
      data: payload.decisions.map((decision) => asNumber(decision.weights[key])),
    }));
    const turnoverSeries = {
      name: "turnover (switch)",
      type: "bar" as const,
      yAxisIndex: 1,
      data: payload.decisions.map((decision) => asNumber(decision.turnover)),
      itemStyle: { color: "#c2410c", opacity: 0.5 },
    };
    chart.setOption({
      tooltip: { trigger: "axis" },
      legend: { top: 0 },
      grid: { left: 56, right: 56, top: 40, bottom: 48 },
      xAxis: { type: "category", data: categories, name: "decision time" },
      yAxis: [
        { type: "value", name: "weight" },
        { type: "value", name: "turnover" },
      ],
      series: [...weightSeries, turnoverSeries],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [payload, strategyKeys]);

  return <div ref={chartRef} style={{ width: "100%", height: 280, margin: "16px 0" }} />;
}

function EquityChart({ payload }: { payload: RouterPayload }) {
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (chartRef.current === null || payload.gross_equity_curve.length === 0) return;
    const chart = echarts.init(chartRef.current);
    const categories = payload.gross_equity_curve.map((point) => shortTime(point.time));
    chart.setOption({
      tooltip: { trigger: "axis" },
      legend: { top: 0 },
      grid: { left: 64, right: 24, top: 40, bottom: 48 },
      xAxis: { type: "category", data: categories, name: "time" },
      yAxis: { type: "value", name: "equity", scale: true },
      series: [
        {
          name: "gross (before switching cost)",
          type: "line",
          data: payload.gross_equity_curve.map((point) => asNumber(point.equity)),
          itemStyle: { color: "#64748b" },
        },
        {
          name: "net (after switching cost)",
          type: "line",
          data: payload.net_equity_curve.map((point) => asNumber(point.equity)),
          itemStyle: { color: "#1d4ed8" },
        },
      ],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [payload]);

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

function runLabel(run: ReportEnvelope): string {
  const payload = asRouterPayload(run.payload);
  return payload !== null ? `${payload.router} (pnl ${payload.pnl})` : run.id;
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
        label={runLabel}
        renderDetail={(detail) => <RunDetail envelope={detail} />}
        listWidth={260}
      />
    </section>
  );
}
