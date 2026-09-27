import {
  checkResultText,
  eligibilityOf,
  EVIDENCE_SCOPE_TEXT,
  eligibilitySummary,
  sealedOosText,
  validationReportsOf,
} from "../lib/routerEligibility";

// Evidence mode (P10-ELIG) of a router paper run or router stop: the per-strategy eligibility
// checks against the actual validation reports. Renders nothing for a trust-mode payload (no
// `eligibility` key). View model and wording in src/lib/routerEligibility.ts (node --test).

function short(hash: string | null): string {
  return hash === null ? "—" : `${hash.slice(0, 12)}…`;
}

export function EligibilityEvidence({ payload }: { payload: Record<string, unknown> | undefined }) {
  const view = eligibilityOf(payload);
  if (view === null) return null;
  return (
    <>
      <h4>资格证据（证据模式）</h4>
      <p style={{ color: "#555", fontSize: 13 }}>
        每个可路由策略的生命周期声明都与其真实验证报告核对（哈希、subject、判定 PASS、G5 密封样本外、报告所用的
        Validation Profile，以及该 Profile 要求的 ADR-0060 G2 报告项：市场基准规则的 G2.market_benchmark 项、
        inverse_control_reported 时的 G2.inverse_control）：{eligibilitySummary(view)}。{EVIDENCE_SCOPE_TEXT}
      </p>
      {view.malformed > 0 && (
        <p style={{ color: "#664d03", fontSize: 13 }}>
          警告：eligibility 中有 {view.malformed} 条记录无法解析，未显示。
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>strategy</th>
            <th>声明的 lifecycle</th>
            <th>report_hash</th>
            <th>subject</th>
            <th>判定</th>
            <th>G5（密封样本外）</th>
            <th>结果</th>
            <th>detail</th>
          </tr>
        </thead>
        <tbody>
          {view.checks.map((check) => (
            <tr key={check.strategy}>
              <td>{check.strategy}</td>
              <td>{check.lifecycle}</td>
              <td>
                <code title={check.report_hash ?? undefined}>{short(check.report_hash)}</code>
              </td>
              <td>{check.subject ?? "—"}</td>
              <td>{check.verdict ?? "—"}</td>
              <td>{sealedOosText(check)}</td>
              <td style={{ color: check.refusal === null ? "#166534" : "#b91c1c" }}>{checkResultText(check)}</td>
              <td style={{ color: "#555", fontSize: 13 }}>{check.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/** A paper run's optional validation report binding (strategy -> report hash); nothing if absent. */
export function ValidationReportBinding({ payload }: { payload: Record<string, unknown> | undefined }) {
  const rows = validationReportsOf(payload);
  if (rows === null) return null;
  return (
    <>
      <h4>验证报告绑定（validation_reports）</h4>
      <table>
        <thead>
          <tr>
            <th>strategy</th>
            <th>validation_report</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.strategy}>
              <td>{row.strategy}</td>
              <td>
                <code title={row.report_hash}>{short(row.report_hash)}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
