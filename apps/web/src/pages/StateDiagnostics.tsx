import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import {
  asStateDiagnosticsPayload,
  cell,
  diagnosticsLabel,
  flickerSummary,
  stateRows,
  transitionRows,
} from "../lib/stateDiagnostics";

// Phase 2 state stability diagnostics (research/states/diagnostics.py): distribution, run
// durations, transition matrix and flicker. Descriptive only — no threshold, no "good state"
// verdict. Payload types and the pure helpers live in src/lib/stateDiagnostics.ts.

function DiagnosticsDetail({ envelope }: { envelope: ReportEnvelope }) {
  const report = asStateDiagnosticsPayload(envelope.payload);
  if (report === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  const flicker = flickerSummary(report);
  return (
    <>
      <p>
        evaluations {report.evaluations} · not_computable {report.not_computable} · schema{" "}
        {report.schema_version} · diagnostics_hash <code>{envelope.id.slice(0, 12)}…</code>
      </p>

      <h4>分布与持续时间（steps）</h4>
      <table>
        <thead>
          <tr>
            <th>state</th>
            <th>count</th>
            <th>share</th>
            <th>runs</th>
            <th>mean_steps</th>
            <th>max_steps</th>
          </tr>
        </thead>
        <tbody>
          {stateRows(report).map((row) => (
            <tr key={row.state}>
              <td>{row.state}</td>
              <td>{cell(row.count)}</td>
              <td>{cell(row.share)}</td>
              <td>{row.runs}</td>
              <td>{cell(row.mean_steps)}</td>
              <td>{cell(row.max_steps)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>转移矩阵（行 = from，count / probability）</h4>
      <table>
        <thead>
          <tr>
            <th>from \ to</th>
            {report.state_space.map((to) => (
              <th key={to}>{to}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {transitionRows(report).map((row) => (
            <tr key={row.from}>
              <td>{row.from}</td>
              {row.cells.map((c) => (
                <td key={c.to}>
                  {cell(c.count)} / {cell(c.probability)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Flicker</h4>
      <p>
        min_run {flicker.min_run}（调用方给定的参数）· 短 run {flicker.short_runs} / {flicker.runs} ·
        short_run_share {cell(flicker.short_run_share)} · switch_rate {cell(flicker.switch_rate)}
      </p>

      <h4>Runs</h4>
      <table>
        <thead>
          <tr>
            <th>state</th>
            <th>start</th>
            <th>end</th>
            <th>steps</th>
          </tr>
        </thead>
        <tbody>
          {report.runs.map((run, index) => (
            <tr key={`${run.start}:${index}`}>
              <td>{run.state}</td>
              <td>{run.start}</td>
              <td>{run.end}</td>
              <td>{run.steps}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p style={{ color: "#555", fontSize: 13 }}>
        “—” 表示未定义（例如没有可计算的标签），不是 0。本页只描述序列，不判断状态好坏。
      </p>
    </>
  );
}

function label(report: ReportEnvelope): string {
  const payload = asStateDiagnosticsPayload(report.payload);
  return payload !== null ? diagnosticsLabel(payload) : report.id;
}

export function StateDiagnostics() {
  return (
    <section>
      <SimulatedBanner />
      <h2>State Diagnostics（状态稳定性诊断）</h2>
      <ReportBrowser
        kind="state_diagnostics"
        empty="（无状态诊断报告 — 未配置报告目录或目录为空）"
        prompt="选择一份诊断报告查看分布、转移矩阵与 flicker。"
        label={label}
        renderDetail={(detail) => <DiagnosticsDetail envelope={detail} />}
        listWidth={260}
      />
    </section>
  );
}
