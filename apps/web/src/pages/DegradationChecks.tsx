import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import {
  asDegradationCheckPayload,
  checkSummary,
  degradationLabel,
  directionText,
  metricRows,
  metricStatus,
  statusText,
} from "../lib/degradationCheck";

// Phase 11 degradation checks (apps/worker/degradation.py, ADR-0049): recent metrics vs the
// validation baseline, with each threshold's source. Evidence only — a check never changes
// lifecycle state (ACTIVE -> DEGRADED is a Control Plane transition). Payload types and the pure
// helpers live in src/lib/degradationCheck.ts.

const STATUS_COLOR = { breached: "#b91c1c", missing: "#92400e", within: "#166534" } as const;

function CheckDetail({ envelope }: { envelope: ReportEnvelope }) {
  const check = asDegradationCheckPayload(envelope.payload);
  if (check === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  return (
    <>
      <p>
        subject <strong>{check.subject}</strong> · window {check.window} · check_hash{" "}
        <code>{`${check.check_hash.slice(0, 12)}…`}</code>
      </p>
      <p>
        <strong>{checkSummary(check)}</strong>
      </p>
      <p style={{ color: "#555", fontSize: 13 }}>
        {check.status} — {check.note}
      </p>
      <table>
        <thead>
          <tr>
            <th>metric</th>
            <th>status</th>
            <th>direction</th>
            <th>baseline</th>
            <th>recent</th>
            <th>decline</th>
            <th>max_decline</th>
            <th>threshold source</th>
          </tr>
        </thead>
        <tbody>
          {metricRows(check).map((metric) => {
            const status = metricStatus(metric);
            return (
              <tr key={metric.metric}>
                <td>{metric.metric}</td>
                <td style={{ color: STATUS_COLOR[status], fontWeight: 600 }}>{statusText(status)}</td>
                <td>{directionText(metric.direction)}</td>
                <td>{metric.baseline}</td>
                <td>{metric.recent ?? "—"}</td>
                <td>{metric.decline ?? "—"}</td>
                <td>{metric.max_decline}</td>
                <td>
                  <code>{metric.threshold_source}</code>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

function checkLabel(report: ReportEnvelope): string {
  const check = asDegradationCheckPayload(report.payload);
  return check !== null ? degradationLabel(check) : report.id;
}

export function DegradationChecks() {
  return (
    <section>
      <SimulatedBanner />
      <h2>Degradation Checks（退化检查）</h2>
      <p style={{ color: "#555", fontSize: 14 }}>
        近期指标与验证基线的对比；阈值只来自所列来源（Validation Profile 或显式映射）。只作证据：不改变生命周期状态，
        缺少近期值表示证据不足而不是健康；没有任何下单、账户或转账能力（H10）。
      </p>
      <ReportBrowser
        kind="degradation_check"
        empty="（无退化检查报告 — 未配置报告目录或目录为空）"
        prompt="选择一份检查查看每个指标。"
        label={checkLabel}
        renderDetail={(detail) => <CheckDetail envelope={detail} />}
        listWidth={300}
      />
    </section>
  );
}
