import { useEffect, useMemo, useRef } from "react";
import { listReports, type ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView, InvalidReports } from "../components/States";
import { echarts } from "../lib/echarts";
import {
  formatUsage,
  outcomeText,
  PENDING_HUMAN_APPROVAL,
  replacementTriggers,
  roundRows,
  usageChartOption,
  usageSeries,
  type ReplacementTriggerView,
  type RoundRow,
  type TriggerRow,
  type UsageSeries,
} from "../lib/researchLoop";
import { useApi } from "../lib/useApi";

// Reads the real LoopRoundRecord fields (ADR-0050; src/lib/researchLoop.ts): round status, the
// stages that did not complete (with their error), charged round usage and cumulative total usage.
// Usage is charted per dimension (trials / llm_cost_units / compute_seconds): each its own small
// chart with its unit, round usage (bars, left axis) and cumulative total (line, right axis).
// Rounds whose evolution stage carries the optional P12 `replacement_trigger` summary also get a
// read-only trigger audit table (absent in every loop without the trigger: section hidden).

function UsageDimensionChart({ labels, series }: { labels: string[]; series: UsageSeries }) {
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (chartRef.current === null) return;
    const instance = echarts.init(chartRef.current);
    instance.setOption(usageChartOption(labels, series));
    const onResize = () => instance.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      instance.dispose();
    };
  }, [labels, series]);

  return (
    <figure style={{ margin: "8px 0 12px" }}>
      <figcaption style={{ fontSize: 13, color: "#555" }}>
        {series.title}：每轮用量（柱，左轴）· 累计用量（线，右轴）
      </figcaption>
      <div ref={chartRef} data-usage={series.key} style={{ width: "100%", height: 200 }} />
    </figure>
  );
}

function UsageCharts({ rows }: { rows: RoundRow[] }) {
  const chart = useMemo(() => usageSeries(rows), [rows]);
  return (
    <>
      {chart.series.map((series) => (
        <UsageDimensionChart key={series.key} labels={chart.labels} series={series} />
      ))}
    </>
  );
}

function RoundsTable({ rows }: { rows: RoundRow[] }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>loop / round</th>
            <th>as_of</th>
            <th>status</th>
            <th>stages run / skipped</th>
            <th>problem stages</th>
            <th>round usage</th>
            <th>total usage</th>
            <th>overrun</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td title={row.id}>
                {row.loopId} #{row.roundIndex ?? "?"}
              </td>
              <td>{row.asOf}</td>
              <td style={row.status !== "COMPLETED" ? { color: "crimson", fontWeight: 600 } : undefined}>
                {row.status}
              </td>
              <td>
                {row.stagesRun} / {row.stagesSkipped}
              </td>
              <td>
                {row.problems.length === 0
                  ? "—"
                  : row.problems.map((p) => (
                      <div key={p.name}>
                        {p.name}: {p.status}
                        {p.error !== null && <code> {p.error}</code>}
                      </div>
                    ))}
              </td>
              <td>{formatUsage(row.roundUsage)}</td>
              <td>{formatUsage(row.totalUsage)}</td>
              <td>{row.overrunStage ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// P12 replacement trigger audit (ADR-0100 item 7; research/loop/replacement.py): shown only for
// rounds whose evolution stage summary carries `replacement_trigger`. Read-only: proposals are
// always PENDING_HUMAN_APPROVAL; nothing here approves, promotes or swaps anything.

function short(value: string | null, length = 12): string {
  return value === null ? "—" : value.length > length ? `${value.slice(0, length)}…` : value;
}

const OUTCOME_COLOR: Record<string, string> = {
  refused: "#92400e",
  failed: "#b91c1c",
  proposed: "#1d4ed8",
  window_opened: "#374151",
  not_proposed: "#374151",
};

function TriggerRowView({ row }: { row: TriggerRow }) {
  return (
    <tr data-trigger-outcome={row.outcome}>
      <td>{row.phase}</td>
      <td>
        <code>{row.candidate}</code>
        {row.candidateState !== null && <div style={{ fontSize: 12, color: "#555" }}>{row.candidateState}</div>}
      </td>
      <td title={row.trialHash ?? undefined}>
        <code>{row.trial ?? "—"}</code>
      </td>
      <td title={row.windowProfile ?? undefined}>
        <code>{row.windowId ?? "—"}</code>
        {row.windowRange !== null && <div style={{ fontSize: 12, color: "#555" }}>{row.windowRange}</div>}
      </td>
      <td title={row.openingHash ?? undefined}>
        {row.openingHash === null ? "—" : <code>{short(row.openingHash)}</code>}
        {row.openedAt !== null && <div style={{ fontSize: 12, color: "#555" }}>opened {row.openedAt}</div>}
      </td>
      <td title={row.consumptionHash ?? undefined}>
        {row.consumptionHash === null ? "—" : <code>{short(row.consumptionHash)}</code>}
        {row.consumedReports !== null && (
          <div style={{ fontSize: 12, color: "#555" }}>{row.consumedReports} report(s)</div>
        )}
      </td>
      <td style={{ color: OUTCOME_COLOR[row.outcome] ?? "#374151", fontWeight: 600 }}>
        {outcomeText(row.outcome)}
        {row.refusal !== null && <div style={{ fontWeight: 400, overflowWrap: "anywhere" }}><code>{row.refusal}</code></div>}
        {row.error !== null && <div style={{ fontWeight: 400, overflowWrap: "anywhere" }}><code>{row.error}</code></div>}
      </td>
      <td>
        {row.proposals.length === 0
          ? "—"
          : row.proposals.map((proposal, index) => (
              <div key={proposal.proposalHash ?? index} title={proposal.proposalHash ?? undefined}>
                <code>{short(proposal.proposalHash)}</code> {proposal.incumbent ?? "?"} → {proposal.candidate ?? "?"}{" "}
                <strong>{proposal.status ?? "—"}</strong>
              </div>
            ))}
        {row.alreadyProposed > 0 && <div style={{ fontSize: 12 }}>already proposed: {row.alreadyProposed}</div>}
        {row.jobRefusals.map((refused, index) => (
          <div key={index} style={{ fontSize: 12, color: "#92400e" }}>
            job refused {refused.incumbent ?? "?"}: {refused.reason ?? "—"}
          </div>
        ))}
      </td>
    </tr>
  );
}

function TriggerRound({ view }: { view: ReplacementTriggerView }) {
  return (
    <div data-trigger-round={view.id} style={{ margin: "12px 0" }}>
      <h4 style={{ margin: "8px 0 4px" }}>
        {view.loopId} #{view.roundIndex ?? "?"} · {view.asOf}
      </h4>
      <p style={{ margin: "2px 0", fontSize: 13, color: "#555" }}>
        format_version {view.formatVersion ?? "—"} · status <strong>{view.status ?? "—"}</strong> · proposed_by{" "}
        <code>{view.proposedBy ?? "—"}</code> · family trials {view.familyTrials ?? "—"} · unsealings{" "}
        {view.unsealingCount ?? "—"} / {view.unsealingBudget ?? "—"}
      </p>
      {view.problems.length > 0 && (
        <div
          role="note"
          data-trigger-problems={view.problems.length}
          style={{ background: "#fef3c7", border: "1px solid #92400e", color: "#92400e", padding: "6px 12px" }}
        >
          <strong>与预期格式不符（原样显示，见下方原始 JSON）：</strong>
          <ul style={{ margin: "4px 0 0" }}>
            {view.problems.map((problem, index) => (
              <li key={index}>{problem}</li>
            ))}
          </ul>
        </div>
      )}
      {view.rows.length === 0 ? (
        <p style={{ fontSize: 13 }}>本轮没有符合条件的候选。</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>phase</th>
                <th>candidate</th>
                <th>trial</th>
                <th>window</th>
                <th>opening</th>
                <th>consumption</th>
                <th>outcome / refusal</th>
                <th>proposals</th>
              </tr>
            </thead>
            <tbody>
              {view.rows.map((row, index) => (
                <TriggerRowView key={`${row.candidate}-${index}`} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {view.notEligible.length > 0 && (
        <p style={{ fontSize: 13 }}>
          not eligible:{" "}
          {view.notEligible.map((item) => `${item.candidate ?? "?"}（${item.reason ?? "—"}）`).join("；")}
        </p>
      )}
      {view.alreadyTriggered.length > 0 && (
        <p style={{ fontSize: 13 }}>
          already triggered: {view.alreadyTriggered.map((item) => item.candidate ?? "?").join(", ")}
        </p>
      )}
      <details>
        <summary>原始 replacement_trigger JSON</summary>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(view.raw, null, 2)}</pre>
      </details>
    </div>
  );
}

function ReplacementTriggerAudit({ reports }: { reports: ReportEnvelope[] }) {
  const views = useMemo(() => replacementTriggers(reports), [reports]);
  if (views.length === 0) return null;
  const due = views.filter((view) => view.due);
  const notDue = views.length - due.length;
  return (
    <section aria-label="Replacement trigger audit" style={{ marginTop: 20, borderTop: "1px solid #ccc", paddingTop: 12 }}>
      <h3>替换提案触发审计（P12 replacement trigger，ADR-0100）</h3>
      <div
        role="note"
        style={{ background: "#eff6ff", border: "1px solid #1d4ed8", padding: "8px 12px", fontSize: 13 }}
      >
        可选、默认关闭的循环内触发：只在预先登记的独立密封窗口上开封一次、消耗一次。所有提案状态恒为{" "}
        <code>{PENDING_HUMAN_APPROVAL}</code>：循环不批准、不晋升、不切换任何策略，OOS → PAPER 仍须人工批准。此处只读。
      </div>
      {notDue > 0 && (
        <p style={{ fontSize: 13, color: "#555" }}>{notDue} 轮触发器未到期（due = false）。</p>
      )}
      {due.map((view) => (
        <TriggerRound key={view.id} view={view} />
      ))}
    </section>
  );
}

function Rounds({ reports }: { reports: ReportEnvelope[] }) {
  const rows = useMemo(() => roundRows(reports), [reports]);
  return (
    <>
      <UsageCharts rows={rows} />
      <RoundsTable rows={rows} />
      <ReplacementTriggerAudit reports={reports} />
    </>
  );
}

export function ResearchLoop() {
  const listing = useApi(() => listReports("research_loop_round"), []);

  return (
    <section>
      <SimulatedBanner />
      <h2>研究循环（Research Loop）</h2>
      {listing.status === "ok" && <InvalidReports invalid={listing.data.invalid} />}
      <AsyncView
        state={listing}
        what="round 记录"
        isEmpty={(data) => data.reports.length === 0}
        empty="（无 round 记录 — 未配置报告目录或目录为空）"
      >
        {(data) => <Rounds reports={data.reports} />}
      </AsyncView>
    </section>
  );
}
