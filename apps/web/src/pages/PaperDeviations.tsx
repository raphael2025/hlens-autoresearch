import { useEffect, useRef } from "react";
import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import { echarts } from "../lib/echarts";
import {
  asPaperDeviationPayload,
  deviationChartOption,
  decimalText,
  deviationLabel,
  percent,
  summaryRows,
  type PaperDeviationPayload,
} from "../lib/paperDeviation";

// Phase 10 paper deviation (research/router/deviation.py): the router's net paper result vs a
// reference backtest the caller declared, mark by mark. Descriptive only — no threshold, no
// verdict. Payload types and the pure helpers live in src/lib/paperDeviation.ts.

function short(hash: string): string {
  return `${hash.slice(0, 12)}…`;
}

function DeviationChart({ report }: { report: PaperDeviationPayload }) {
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (chartRef.current === null || report.marks.length === 0) return;
    const chart = echarts.init(chartRef.current);
    chart.setOption(deviationChartOption(report));
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [report]);

  return <div ref={chartRef} style={{ width: "100%", height: 280, margin: "16px 0" }} />;
}

function DeviationDetail({ envelope }: { envelope: ReportEnvelope }) {
  const report = asPaperDeviationPayload(envelope.payload);
  if (report === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  return (
    <>
      <p>
        router <strong>{report.router}</strong> · run_hash <code>{short(report.run_hash)}</code> ·
        paper result <code>{short(report.paper_result_hash)}</code>
      </p>
      <p>
        reference <strong>{report.reference_provider}</strong> · result{" "}
        <code>{short(report.reference_result_hash)}</code> · request{" "}
        <code>{short(report.reference_request_hash)}</code> · instruments {report.instruments.join(", ")}
      </p>
      <p style={{ color: "#555", fontSize: 13 }}>
        {report.status} — {report.note}
      </p>
      <h4>汇总（描述性，无阈值）</h4>
      <table>
        <tbody>
          {summaryRows(report.summary).map((row) => (
            <tr key={row.label}>
              <td>{row.label}</td>
              <td>{row.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h4>权益：paper vs reference</h4>
      <DeviationChart report={report} />
      <h4>逐 mark 偏差</h4>
      <table>
        <thead>
          <tr>
            <th>time</th>
            <th>paper equity</th>
            <th>reference equity</th>
            <th>Δ equity</th>
            <th>paper return</th>
            <th>reference return</th>
            <th>Δ return</th>
          </tr>
        </thead>
        <tbody>
          {report.marks.map((mark) => (
            <tr key={mark.time}>
              <td>{mark.time}</td>
              <td>{decimalText(mark.paper_equity)}</td>
              <td>{decimalText(mark.reference_equity)}</td>
              <td>{decimalText(mark.equity_difference)}</td>
              <td title={mark.paper_return ?? "not computable"}>{percent(mark.paper_return)}</td>
              <td title={mark.reference_return ?? "not computable"}>{percent(mark.reference_return)}</td>
              <td title={mark.return_difference ?? "not computable"}>{percent(mark.return_difference)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function reportLabel(report: ReportEnvelope): string {
  const payload = asPaperDeviationPayload(report.payload);
  return payload !== null ? deviationLabel(payload) : report.id;
}

export function PaperDeviations() {
  return (
    <section>
      <SimulatedBanner />
      <h2>Paper Deviation（纸面偏差）</h2>
      <p style={{ color: "#555", fontSize: 14 }}>
        路由器净纸面结果与调用方声明的参照回测逐 mark 对比；只描述差异，不给阈值或结论，也没有任何下单、账户或转账能力（H10）。
      </p>
      <ReportBrowser
        kind="paper_deviation"
        empty="（无纸面偏差报告 — 未配置报告目录或目录为空）"
        prompt="选择一份偏差报告查看逐 mark 对比。"
        label={reportLabel}
        renderDetail={(detail) => <DeviationDetail envelope={detail} />}
        listWidth={300}
      />
    </section>
  );
}
