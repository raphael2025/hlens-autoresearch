import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import {
  asRetroAuditPayload,
  displayValue,
  retroAuditLabel,
  type RetroAuditFinding,
} from "../lib/retroAudit";

function FindingDetail({ finding }: { finding: RetroAuditFinding }) {
  return (
    <details style={{ margin: "12px 0", borderTop: "1px solid #ddd", paddingTop: 8 }}>
      <summary>
        {finding.subject} · {finding.action} · recorded {finding.recorded_verdict} → current{" "}
        {finding.current_verdict} · effective {finding.effective_verdict}
      </summary>
      <p>Lifecycle state: {finding.lifecycle_state}</p>
      <p>
        Recorded profile: <code>{displayValue(finding.recorded_profile_hash)}</code> · Current profile:{" "}
        <code>{displayValue(finding.current_profile_hash)}</code>
      </p>
      {finding.would_now_pass && (
        <p role="note" style={{ color: "#92400e" }}>
          当前规则会判为 PASS；若此前已拒绝，effective verdict 仍保持 FAIL，不会翻转历史决定。
        </p>
      )}
      {finding.gate_diffs.length === 0 ? (
        <p>没有 gate 差异。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>gate</th>
              <th>recorded</th>
              <th>current</th>
              <th>recorded value / threshold source</th>
              <th>current value / threshold source</th>
            </tr>
          </thead>
          <tbody>
            {finding.gate_diffs.map((diff, index) => (
              <tr key={`${displayValue(diff.gate_id)}:${index}`}>
                <td>{displayValue(diff.gate_id)}</td>
                <td>{displayValue(diff.recorded)}</td>
                <td>{displayValue(diff.current)}</td>
                <td>
                  {displayValue(diff.recorded_value)} / {displayValue(diff.recorded_threshold_source)}
                </td>
                <td>
                  {displayValue(diff.current_value)} / {displayValue(diff.current_threshold_source)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  );
}

function AuditDetail({ envelope }: { envelope: ReportEnvelope }) {
  const report = asRetroAuditPayload(envelope.payload);
  if (report === null) return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  return (
    <>
      <p>
        audited_at {report.audited_at} · schema {report.schema_version} · report_hash{" "}
        <code>{report.report_hash}</code>
      </p>
      <p>Rules: {report.rules}</p>
      <p>
        Subjects: {report.subjects} · flagged for revalidation: {report.flagged_for_revalidation} ·
        rejected that current rules would pass: {report.rejected_that_would_now_pass}
      </p>
      <p role="note" style={{ background: "#fff3cd", padding: 10 }}>
        仅为回溯差异报告，不执行生命周期转换。历史拒绝不会因新规则而转为通过；处理建议仍须由控制面按自身证据与审批流程决定。
      </p>
      <p style={{ color: "#555" }}>{report.status} — {report.note}</p>
      <h3>Findings</h3>
      {report.findings.length === 0 ? <p>本次没有审计对象。</p> : report.findings.map((finding, index) => (
        <FindingDetail key={`${finding.subject}:${index}`} finding={finding} />
      ))}
    </>
  );
}

function label(envelope: ReportEnvelope): string {
  const report = asRetroAuditPayload(envelope.payload);
  return report === null ? envelope.id : retroAuditLabel(report);
}

export function RetroAudits() {
  return (
    <section>
      <SimulatedBanner />
      <h2>Retro Audits（回溯审计）</h2>
      <p style={{ color: "#555", fontSize: 14 }}>
        展示调用方显式提交的审计结果和逐 gate 差异。此页面只读，不扫描历史对象、不重跑验证，也不改变生命周期。
      </p>
      <ReportBrowser
        kind="retro_audit"
        empty="（无回溯审计报告 — 未配置报告目录或目录为空）"
        prompt="选择一份回溯审计报告查看结果。"
        label={label}
        renderDetail={(detail) => <AuditDetail envelope={detail} />}
        listWidth={320}
      />
    </section>
  );
}
