import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { ReportEnvelope, ReportKind } from "../api";
import { api, count, escaped, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { checkSummary, asDegradationCheckPayload, degradationLabel } from "../lib/degradationCheck.ts";
import { asEventStatisticsPayload, statisticsLabel } from "../lib/eventStatistics.ts";
import { fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { asCalibrationPayload } from "../lib/gateCalibration.ts";
import { asPaperDeviationPayload, deviationLabel, summaryRows } from "../lib/paperDeviation.ts";
import { asRouterStopPayload, reasonText, routerStopLabel, strategyRows } from "../lib/routerStop.ts";
import { asStateDiagnosticsPayload, diagnosticsLabel } from "../lib/stateDiagnostics.ts";
import { asValidationReportPayload, gateRows } from "../lib/validationReport.ts";
import { DegradationChecks } from "./DegradationChecks.tsx";
import { EventStatistics } from "./EventStatistics.tsx";
import { GateCalibration } from "./GateCalibration.tsx";
import { PaperDeviations } from "./PaperDeviations.tsx";
import { RouterPaperRuns } from "./RouterPaperRuns.tsx";
import { RouterStops } from "./RouterStops.tsx";
import { StateDiagnostics } from "./StateDiagnostics.tsx";
import { StateStrategyMatrices } from "./StateStrategyMatrices.tsx";
import { ValidationReports } from "./ValidationReports.tsx";

// Every ReportBrowser page, rendered over its real apps/web/fixtures/<kind>/ report: first paint,
// loaded list (with its payload-derived labels and an invalid-file warning), the selected report's
// detail (parsed, never the raw-JSON fallback), empty, list error and detail error.

const SIMULATED = "SIMULATED / NOT_VALIDATED";

function must<T>(value: T | null, what: string): T {
  assert.ok(value !== null, `${what}: the fixture payload parses`);
  return value;
}

function hash12(hash: string): string {
  return `${hash.slice(0, 12)}…`;
}

type Case = {
  Page: () => JSX.Element;
  kind: ReportKind;
  heading: string;
  empty: string;
  prompt: string;
  /** The list button text the page derives from the payload. */
  label: (report: ReportEnvelope) => string;
  /** Text the detail pane must show for this report (compared escaped). */
  detail: (report: ReportEnvelope) => string[];
  banners?: string[];
};

type Gate = { gate_id: string; metric: string };

const CASES: Case[] = [
  {
    Page: ValidationReports,
    kind: "validation_report",
    heading: "验证报告（Validation Reports）",
    empty: "（无报告 — 未配置报告目录或目录为空）",
    prompt: "选择一个报告查看详情。",
    label: (r) => `${r.id.slice(0, 12)}… [${String(r.payload.verdict)}]`,
    detail: (r) => [
      `整体判定：`,
      String(r.payload.verdict),
      hash12(r.content_hash),
      `contract ${String(r.payload.schema_version)}`,
      ...(r.payload.gates as Gate[]).flatMap((gate) => [gate.gate_id, gate.metric]),
      // the authoritative exact text when present (2.1.0), else the float (2.0.0)
      ...gateRows(must(asValidationReportPayload(r.payload), "validation_report")).flatMap((row) => [
        row.value.text,
        row.representation,
      ]),
    ],
  },
  {
    Page: StateStrategyMatrices,
    kind: "state_strategy_matrix",
    heading: "状态 × 策略矩阵（State × Strategy Matrices）",
    empty: "（无矩阵报告 — 未配置报告目录或目录为空）",
    prompt: "选择一个矩阵查看详情。",
    label: (r) => {
      const cells = r.payload.cells as { count: number }[];
      const n = cells.reduce((sum, cell) => sum + cell.count, 0);
      return `${String(r.payload.strategy)} × ${String(r.payload.state)} (n=${n})`;
    },
    detail: (r) => [
      "样本总数（sample count）：",
      hash12(String(r.payload.matrix_hash)),
      ...(r.payload.cells as { state: string | null }[]).map((cell) => cell.state ?? "(none)"),
    ],
  },
  {
    Page: RouterPaperRuns,
    kind: "router_paper_run",
    heading: "路由纸面运行（Router Paper Runs）",
    empty: "（无路由运行报告 — 未配置报告目录或目录为空）",
    prompt: "选择一次运行查看详情。",
    label: (r) => `${String(r.payload.router)} (pnl ${String(r.payload.pnl)})`,
    detail: (r) => [
      hash12(String(r.payload.run_hash)),
      `total_switching_cost ${String(r.payload.total_switching_cost)}`,
      "Weights / switch timeline",
      "Equity: gross vs. net of switching cost",
      "Switching charges",
    ],
    banners: ["PAPER ONLY"],
  },
  {
    Page: RouterStops,
    kind: "router_stop",
    heading: "Router Stops（路由停止记录）",
    empty: "（无路由停止记录 — 未配置报告目录或目录为空）",
    prompt: "选择一条停止记录查看原因与输入。",
    label: (r) => routerStopLabel(must(asRouterStopPayload(r.payload), "router_stop")),
    detail: (r) => {
      const stop = must(asRouterStopPayload(r.payload), "router_stop");
      return [
        reasonText(stop.reason),
        stop.detail,
        hash12(stop.stop_hash),
        ...strategyRows(stop).map((row) => row.strategy),
      ];
    },
  },
  {
    Page: PaperDeviations,
    kind: "paper_deviation",
    heading: "Paper Deviation（纸面偏差）",
    empty: "（无纸面偏差报告 — 未配置报告目录或目录为空）",
    prompt: "选择一份偏差报告查看逐 mark 对比。",
    label: (r) => deviationLabel(must(asPaperDeviationPayload(r.payload), "paper_deviation")),
    detail: (r) => {
      const report = must(asPaperDeviationPayload(r.payload), "paper_deviation");
      return [
        "汇总（描述性，无阈值）",
        "逐 mark 偏差",
        report.reference_provider,
        ...summaryRows(report.summary).flatMap((row) => [row.label, row.value]),
        ...report.marks.map((mark) => mark.time),
      ];
    },
  },
  {
    Page: GateCalibration,
    kind: "gate_calibration",
    heading: "Gate Calibration（校准证据）",
    empty: "（无校准报告 — 未配置报告目录或目录为空）",
    prompt: "选择一份校准报告查看 FPR / power 明细。",
    label: (r) => {
      const report = must(asCalibrationPayload(r.payload), "gate_calibration");
      return `${report.inputs.detector?.name ?? r.id} (${report.candidates.length} candidates)`;
    },
    detail: (r) => {
      const report = must(asCalibrationPayload(r.payload), "gate_calibration");
      return [
        hash12(report.report_hash),
        "Pipeline-level (FPR / power)",
        "Per-gate",
        ...report.candidates.flatMap((c) => [c.profile, hash12(c.profile_hash)]),
      ];
    },
    banners: ["EVIDENCE ONLY — NOT A PROFILE DECISION"],
  },
  {
    Page: StateDiagnostics,
    kind: "state_diagnostics",
    heading: "State Diagnostics（状态稳定性诊断）",
    empty: "（无状态诊断报告 — 未配置报告目录或目录为空）",
    prompt: "选择一份诊断报告查看分布、转移矩阵与 flicker。",
    label: (r) => diagnosticsLabel(must(asStateDiagnosticsPayload(r.payload), "state_diagnostics")),
    detail: (r) => {
      const report = must(asStateDiagnosticsPayload(r.payload), "state_diagnostics");
      return [
        `evaluations ${report.evaluations} · not_computable ${report.not_computable}`,
        hash12(r.id),
        "分布与持续时间（steps）",
        "转移矩阵（行 = from，count / probability）",
        "Flicker",
        ...report.state_space,
      ];
    },
  },
  {
    Page: EventStatistics,
    kind: "event_statistics",
    heading: "Event Statistics（事件统计）",
    empty: "（无事件统计报告 — 未配置报告目录或目录为空）",
    prompt: "选择一份事件统计报告查看频率、共现、领先-滞后与重叠诊断。",
    label: (r) => statisticsLabel(must(asEventStatisticsPayload(r.payload), "event_statistics")),
    detail: (r) => {
      const report = must(asEventStatisticsPayload(r.payload), "event_statistics");
      return [
        hash12(report.report_hash),
        "来源事件运行（EventResult.result_hash）",
        report.note,
        ...report.source_result_hashes,
      ];
    },
  },
  {
    Page: DegradationChecks,
    kind: "degradation_check",
    heading: "Degradation Checks（退化检查）",
    empty: "（无退化检查报告 — 未配置报告目录或目录为空）",
    prompt: "选择一份检查查看每个指标。",
    label: (r) => degradationLabel(must(asDegradationCheckPayload(r.payload), "degradation_check")),
    detail: (r) => {
      const check = must(asDegradationCheckPayload(r.payload), "degradation_check");
      return [
        checkSummary(check),
        check.subject,
        hash12(check.check_hash),
        ...check.metrics.flatMap((m) => [m.metric, m.threshold_source]),
      ];
    },
  },
];

function assertIncludes(html: string, texts: string[], where: string): void {
  for (const text of texts) {
    assert.ok(html.includes(escaped(text)), `${where}: expected ${JSON.stringify(text)}`);
  }
}

for (const c of CASES) {
  const reports = fixtureEnvelopes(c.kind);
  const [fixture] = reports;
  const banners = [SIMULATED, ...(c.banners ?? [])];

  describe(`${c.Page.name} (${c.kind})`, () => {
    test("first paint: banners, heading, list loading, no request yet", () => {
      assert.ok(reports.length > 0, `apps/web/fixtures/${c.kind}/ has a fixture`);
      const { html, requests } = renderInitial(<c.Page />);
      assert.deepEqual(requests, []);
      assertIncludes(html, [...banners, c.heading, "加载报告列表中…", c.prompt], "initial");
    });

    test("loaded list: payload-derived labels and the invalid-file warning", async () => {
      const { html, requests } = await renderSettled(<c.Page />, {
        routes: [api.listing(c.kind, reports, [{ id: "stale", reason: "schema_version mismatch" }])],
      });
      assert.deepEqual(requests, [`GET /api/reports/${c.kind}`]);
      assert.equal(count(html, "<button"), reports.length);
      assertIncludes(
        html,
        [...banners, ...reports.map(c.label), "1 个报告文件无效，未展示：", "stale: schema_version mismatch", c.prompt],
        "list",
      );
    });

    // every fixture of the kind: the current (2.1.0) one and, where kept, the legacy 2.0.0 one
    test("each selected fixture: the detail pane parses the real payload (no raw-JSON fallback)", async () => {
      for (const report of reports) {
        const { html, requests } = await renderSettled(<c.Page />, {
          routes: [api.listing(c.kind, reports), api.report(report)],
          selected: report.id,
        });
        assert.ok(requests.includes(`GET /api/reports/${c.kind}/${report.id}`));
        assert.ok(!html.includes("<pre>{"), `${report.id}: the detail is not the raw-payload fallback`);
        assert.ok(!html.includes('role="alert"'));
        assert.ok(!html.includes(escaped(c.prompt)));
        assertIncludes(html, c.detail(report), `detail ${report.id}`);
      }
    });

    test("empty listing: the page's empty text", async () => {
      const { html } = await renderSettled(<c.Page />, { routes: [api.listing(c.kind, [])] });
      assertIncludes(html, [c.empty], "empty");
      assert.equal(count(html, "<button"), 0);
    });

    test("listing error: the status meaning and server detail in an alert", async () => {
      const { html } = await renderSettled(<c.Page />, {
        routes: [api.fail("GET", `/reports/${c.kind}`, 500, "report store failed validation")],
      });
      assertIncludes(html, ["服务端校验失败（HTTP 500）：report store failed validation"], "error");
      assert.ok(!html.includes(escaped(c.empty)));
    });

    test("detail error: the 404 in the detail pane, the list still shown", async () => {
      const { html } = await renderSettled(<c.Page />, {
        routes: [
          api.listing(c.kind, reports),
          api.fail("GET", `/reports/${c.kind}/${fixture.id}`, 404, "unknown report id"),
        ],
        selected: fixture.id,
      });
      assert.equal(count(html, "<button"), reports.length);
      assertIncludes(html, ["不存在（HTTP 404）：unknown report id"], "detail error");
    });
  });
}

describe("ValidationReports: exact gate values (ADR-0052 §1)", () => {
  const reports = fixtureEnvelopes("validation_report");
  const byVersion = (version: string): ReportEnvelope => {
    const found = reports.find((r) => r.payload.schema_version === version);
    assert.ok(found !== undefined, `a ${version} validation_report fixture`);
    return found;
  };

  async function detailOf(report: ReportEnvelope): Promise<string> {
    const { html } = await renderSettled(<ValidationReports />, {
      routes: [api.listing("validation_report", reports), api.report(report)],
      selected: report.id,
    });
    return html;
  }

  const EXACT = '<code title="exact decimal (ADR-0052)">';

  test("2.1.0: value_exact / threshold_exact are shown, not the derived float", async () => {
    const html = await detailOf(byVersion("2.1.0"));
    assert.ok(html.includes("contract 2.1.0"));
    assert.ok(html.includes(`<td>${EXACT}0.0300000000000000001</code></td><td>${EXACT}0.05</code></td><td>exact</td>`));
    assert.ok(!html.includes("<td>0.03</td>"), "the float of an exact gate is not shown");
    assert.ok(html.includes("<td>10</td><td>5</td><td>float</td>"), "a float-only gate keeps its floats");
  });

  test("2.0.0 legacy: no exact keys, the floats are shown", async () => {
    const html = await detailOf(byVersion("2.0.0"));
    assert.ok(html.includes("contract 2.0.0"));
    assert.ok(html.includes("<td>10</td><td>5</td><td>float</td>"));
    assert.ok(!html.includes("<td>exact</td>") && !html.includes(EXACT));
  });
});
