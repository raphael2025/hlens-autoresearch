import { useEffect, useState } from "react";
import { getReport, listReports, type ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";

type Gate = {
  gate_id?: string;
  metric?: string;
  value?: number;
  threshold?: number | null;
  verdict?: string;
};

export function ValidationReports() {
  const [reports, setReports] = useState<ReportEnvelope[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReportEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listReports("validation_report")
      .then(setReports)
      .catch((err) => setError(String(err)));
  }, []);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }
    getReport("validation_report", selectedId)
      .then(setDetail)
      .catch((err) => setError(String(err)));
  }, [selectedId]);

  const gates: Gate[] = Array.isArray(detail?.payload?.gates) ? (detail.payload.gates as Gate[]) : [];

  return (
    <section>
      <SimulatedBanner />
      <h2>验证报告（Validation Reports）</h2>
      {error && <p style={{ color: "crimson" }}>{error}</p>}
      <div style={{ display: "flex", gap: 24 }}>
        <ul style={{ minWidth: 220 }}>
          {reports.length === 0 && <li>（无报告 — 未配置报告目录或目录为空）</li>}
          {reports.map((report) => (
            <li key={report.id}>
              <button onClick={() => setSelectedId(report.id)}>
                {report.id}
                {typeof detail?.payload?.verdict === "string" && report.id === selectedId
                  ? ` [${detail.payload.verdict}]`
                  : ""}
              </button>
            </li>
          ))}
        </ul>
        <div style={{ flex: 1 }}>
          {detail === null && <p>选择一个报告查看详情。</p>}
          {detail !== null && (
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
          )}
        </div>
      </div>
    </section>
  );
}
