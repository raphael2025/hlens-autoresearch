import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import { asValidationReportPayload, gateRows, validationLabel, type Shown } from "../lib/validationReport";

// Parsing lives in src/lib/validationReport.ts (node-tested over the 2.1.0 and legacy 2.0.0
// fixtures). value / threshold show `value_exact` / `threshold_exact` when present (ADR-0052 §1:
// authoritative), else the float of a 2.0.0 report.

function ShownCell({ shown }: { shown: Shown }) {
  return <td>{shown.exact ? <code title="exact decimal (ADR-0052)">{shown.text}</code> : shown.text}</td>;
}

function ValidationDetail({ detail }: { detail: ReportEnvelope }) {
  const report = asValidationReportPayload(detail.payload);
  const rows = report === null ? [] : gateRows(report);
  return (
    <>
      <p>
        <strong>{detail.id}</strong> · created {detail.created} · content_hash{" "}
        <code>{detail.content_hash.slice(0, 12)}…</code>
        {typeof report?.schema_version === "string" && <> · contract {report.schema_version}</>}
      </p>
      {report !== null && (
        <p>
          整体判定：<strong>{report.verdict}</strong>
        </p>
      )}
      {rows.length > 0 ? (
        <>
          <table>
            <thead>
              <tr>
                <th>gate_id</th>
                <th>metric</th>
                <th>value</th>
                <th>threshold</th>
                <th>representation</th>
                <th>threshold_source</th>
                <th>verdict</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={`${row.gateId}-${index}`}>
                  <td>{row.gateId}</td>
                  <td>{row.metric}</td>
                  <ShownCell shown={row.value} />
                  <ShownCell shown={row.threshold} />
                  <td>{row.representation}</td>
                  <td>{row.thresholdSource}</td>
                  <td>{row.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p style={{ fontSize: 13, color: "#555" }}>
            exact：显示 <code>value_exact</code> / <code>threshold_exact</code>（契约 2.1.0，判定依据）；
            float：无精确值（如 2.0.0 报告），显示浮点 <code>value</code> / <code>threshold</code>。
          </p>
        </>
      ) : (
        <pre>{JSON.stringify(detail.payload, null, 2)}</pre>
      )}
    </>
  );
}

export function ValidationReports() {
  return (
    <section>
      <SimulatedBanner />
      <h2>验证报告（Validation Reports）</h2>
      <ReportBrowser
        kind="validation_report"
        empty="（无报告 — 未配置报告目录或目录为空）"
        prompt="选择一个报告查看详情。"
        label={(report) => validationLabel(report.id, report.payload)}
        renderDetail={(detail) => <ValidationDetail detail={detail} />}
      />
    </section>
  );
}
