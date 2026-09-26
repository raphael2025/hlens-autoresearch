import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";

type Gate = {
  gate_id?: string;
  metric?: string;
  value?: number;
  threshold?: number | null;
  verdict?: string;
};

function ValidationDetail({ detail }: { detail: ReportEnvelope }) {
  const gates: Gate[] = Array.isArray(detail.payload.gates) ? (detail.payload.gates as Gate[]) : [];
  return (
    <>
      <p>
        <strong>{detail.id}</strong> · created {detail.created} · content_hash{" "}
        <code>{detail.content_hash.slice(0, 12)}…</code>
      </p>
      {typeof detail.payload.verdict === "string" && (
        <p>
          整体判定：<strong>{String(detail.payload.verdict)}</strong>
        </p>
      )}
      {gates.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>gate_id</th>
              <th>metric</th>
              <th>value</th>
              <th>threshold</th>
              <th>verdict</th>
            </tr>
          </thead>
          <tbody>
            {gates.map((gate, index) => (
              <tr key={gate.gate_id ?? index}>
                <td>{gate.gate_id ?? "—"}</td>
                <td>{gate.metric ?? "—"}</td>
                <td>{gate.value ?? "—"}</td>
                <td>{gate.threshold ?? "—"}</td>
                <td>{gate.verdict ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
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
        label={(report) =>
          typeof report.payload.verdict === "string"
            ? `${report.id.slice(0, 12)}… [${report.payload.verdict}]`
            : report.id
        }
        renderDetail={(detail) => <ValidationDetail detail={detail} />}
      />
    </section>
  );
}
