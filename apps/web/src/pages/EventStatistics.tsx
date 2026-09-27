import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import {
  asEventStatisticsPayload,
  statisticsLabel,
  statisticView,
  type StatisticView,
} from "../lib/eventStatistics";

// Phase 3 event statistics (research/events/stats.py): frequency, co-occurrence, lead-lag and
// overlap diagnostics bound to the event runs they describe. Descriptive only — no validation
// threshold, not a Validation Profile input. Payload types and helpers: src/lib/eventStatistics.ts.

function StatisticBlock({ view }: { view: StatisticView }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <h4 style={{ marginBottom: 4 }}>{view.title}</h4>
      <table>
        <tbody>
          {view.fields.map(([key, value]) => (
            <tr key={key}>
              <td>{key}</td>
              <td>{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {view.histogram.length > 0 && (
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>{view.kind === "lead_lag" ? "lag bin [edge, edge + bin)，B − A" : "bucket start"}</th>
              <th>count</th>
            </tr>
          </thead>
          <tbody>
            {view.histogram.map((row, index) => (
              <tr key={`${row.label}:${index}`}>
                <td>{row.label}</td>
                <td>{row.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function StatisticsDetail({ envelope }: { envelope: ReportEnvelope }) {
  const report = asEventStatisticsPayload(envelope.payload);
  if (report === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  return (
    <>
      <p>
        {report.status} · schema {report.schema_version} · report_hash{" "}
        <code>{report.report_hash.slice(0, 12)}…</code>
      </p>
      <p style={{ color: "#555" }}>{report.note}</p>
      <h4>来源事件运行（EventResult.result_hash）</h4>
      <ul>
        {report.source_result_hashes.map((hash) => (
          <li key={hash}>
            <code>{hash}</code>
          </li>
        ))}
      </ul>
      {report.statistics.map((stat, index) => (
        <StatisticBlock key={`${stat.kind}:${index}`} view={statisticView(stat)} />
      ))}
      <p style={{ color: "#555", fontSize: 13 }}>“—” 表示未定义（分母为零），不是 0。</p>
    </>
  );
}

function label(report: ReportEnvelope): string {
  const payload = asEventStatisticsPayload(report.payload);
  return payload !== null ? statisticsLabel(payload) : report.id;
}

export function EventStatistics() {
  return (
    <section>
      <SimulatedBanner />
      <h2>Event Statistics（事件统计）</h2>
      <ReportBrowser
        kind="event_statistics"
        empty="（无事件统计报告 — 未配置报告目录或目录为空）"
        prompt="选择一份事件统计报告查看频率、共现、领先-滞后与重叠诊断。"
        label={label}
        renderDetail={(detail) => <StatisticsDetail envelope={detail} />}
        listWidth={240}
      />
    </section>
  );
}
