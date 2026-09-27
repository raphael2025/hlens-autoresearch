import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { EligibilityEvidence } from "../components/EligibilityEvidence";
import { ReportBrowser } from "../components/ReportBrowser";
import { asRouterStopPayload, reasonText, routerStopLabel, strategyRows } from "../lib/routerStop";

// A router stop is a paper run that did NOT happen (research/router/paper.py's RouterStop): the
// page shows why and on which inputs — never a result, never a flat equity curve. Payload types and
// the pure helpers live in src/lib/routerStop.ts (tested with node --test).

function short(hash: string | null): string {
  return hash === null ? "—" : `${hash.slice(0, 12)}…`;
}

function StopDetail({ envelope }: { envelope: ReportEnvelope }) {
  const stop = asRouterStopPayload(envelope.payload);
  if (stop === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  return (
    <>
      <p>
        router <strong>{stop.router}</strong> · spec <code>{short(stop.router_spec_hash)}</code> ·
        state result <code>{short(stop.state_result_hash)}</code> · stop_hash{" "}
        <code>{short(stop.stop_hash)}</code>
      </p>
      <p>
        <strong>停止原因：</strong>
        {reasonText(stop.reason)}
      </p>
      <p style={{ color: "#555" }}>{stop.detail}</p>
      <h4>策略</h4>
      <table>
        <thead>
          <tr>
            <th>strategy</th>
            <th>lifecycle</th>
            <th>result_hash</th>
            <th>validation_report</th>
          </tr>
        </thead>
        <tbody>
          {strategyRows(stop).map((row) => (
            <tr key={row.strategy}>
              <td>{row.strategy}</td>
              <td>{row.lifecycle ?? "—"}</td>
              <td>
                <code>{short(row.result_hash)}</code>
              </td>
              <td>
                <code>{short(row.validation_report)}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {stop.validation_reports === null && (
        <p style={{ color: "#555", fontSize: 13 }}>未提供验证报告绑定（validation_reports = null）。</p>
      )}
      <EligibilityEvidence payload={envelope.payload} />
    </>
  );
}

function stopLabel(report: ReportEnvelope): string {
  const stop = asRouterStopPayload(report.payload);
  return stop !== null ? routerStopLabel(stop) : report.id;
}

export function RouterStops() {
  return (
    <section>
      <SimulatedBanner />
      <h2>Router Stops（路由停止记录）</h2>
      <p style={{ color: "#555", fontSize: 14 }}>
        路由器拒绝运行时的记录：没有模拟任何纸面运行，也没有任何下单、账户或转账能力（H10）。
      </p>
      <ReportBrowser
        kind="router_stop"
        empty="（无路由停止记录 — 未配置报告目录或目录为空）"
        prompt="选择一条停止记录查看原因与输入。"
        label={stopLabel}
        renderDetail={(detail) => <StopDetail envelope={detail} />}
        listWidth={260}
      />
    </section>
  );
}
