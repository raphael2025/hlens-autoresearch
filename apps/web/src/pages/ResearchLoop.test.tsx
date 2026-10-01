import assert from "node:assert/strict";
import { test } from "node:test";
import { api, count, escaped, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { formatUsage, roundRow } from "../lib/researchLoop.ts";
import {
  CANDIDATE,
  INCUMBENT,
  PROPOSAL_HASH,
  triggerSummary,
  withTrigger,
} from "../lib/replacementTrigger.test-util.ts";
import { ResearchLoop } from "./ResearchLoop.tsx";

// The Research Loop page reads the real LoopRoundRecord fixture (ADR-0050) directly from the list.

const KIND = "research_loop_round" as const;
const reports = fixtureEnvelopes(KIND);
const [fixture] = reports;

test("first paint: banner, loading, nothing fetched yet", () => {
  const { html, requests } = renderInitial(<ResearchLoop />);
  assert.deepEqual(requests, []);
  assert.ok(html.includes("SIMULATED / NOT_VALIDATED"));
  assert.ok(html.includes("加载round 记录中…"));
});

test("loaded: one row per round with status, stages and usage; invalid warning", async () => {
  const { html, requests } = await renderSettled(<ResearchLoop />, {
    routes: [api.listing(KIND, reports, [{ id: "r-bad", reason: "record_hash mismatch" }])],
  });
  assert.deepEqual(requests, [`GET /api/reports/${KIND}`]);
  const row = roundRow(fixture);
  assert.ok(html.includes(`<td title="${fixture.id}">${row.loopId} #${row.roundIndex}</td>`));
  assert.ok(html.includes(`<td>${row.status}</td>`));
  assert.ok(html.includes(`<td>${row.stagesRun} / ${row.stagesSkipped}</td>`));
  assert.ok(html.includes(`<td>${escaped(formatUsage(row.roundUsage))}</td>`));
  assert.ok(html.includes("r-bad: record_hash mismatch"));
});

test("loaded: one usage chart per dimension, each captioned with its unit (no shared axis)", async () => {
  const { html } = await renderSettled(<ResearchLoop />, { routes: [api.listing(KIND, reports)] });
  assert.equal(count(html, "<figure"), 3);
  for (const [key, title] of [
    ["trials", "trials (count)"],
    ["llm_cost_units", "llm_cost_units (cost units)"],
    ["compute_seconds", "compute_seconds (s)"],
  ]) {
    assert.ok(html.includes(`${escaped(title)}：每轮用量（柱，左轴）· 累计用量（线，右轴）`), title);
    assert.ok(html.includes(`<div data-usage="${key}" style="width:100%;height:200px"></div>`), key);
  }
  assert.ok(!html.includes("charged usage"), "the old single shared axis is gone");
});

test("empty: the empty text, no table", async () => {
  const { html } = await renderSettled(<ResearchLoop />, { routes: [api.listing(KIND, [])] });
  assert.ok(html.includes("（无 round 记录 — 未配置报告目录或目录为空）"));
  assert.ok(!html.includes("<table>"));
});

test("error: the server detail in an alert", async () => {
  const { html } = await renderSettled(<ResearchLoop />, {
    routes: [api.fail("GET", `/reports/${KIND}`, 503, "reports root not configured")],
  });
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes("后端未配置该数据源（HTTP 503）：reports root not configured"));
});

// P12 replacement trigger audit: the committed round has no trigger (section hidden); the
// trigger rounds are built inline in the writer's shape (src/lib/replacementTrigger.test-util.ts).

const AUDIT = 'aria-label="Replacement trigger audit"';

test("no trigger in any round: the audit section is hidden", async () => {
  const { html } = await renderSettled(<ResearchLoop />, { routes: [api.listing(KIND, reports)] });
  assert.ok(!html.includes(AUDIT));
  assert.ok(!html.includes("PENDING_HUMAN_APPROVAL"));
});

test("a due trigger round: read-only table with trial, window, opening / consumption, outcome and pending proposal", async () => {
  const round = withTrigger(fixture, triggerSummary(), "round-trigger");
  round.payload.round_index = 1;
  const notDue = withTrigger(fixture, { due: false }, "round-not-due");
  const { html } = await renderSettled(<ResearchLoop />, {
    routes: [api.listing(KIND, [...reports, notDue, round])],
  });
  assert.ok(html.includes(AUDIT));
  assert.ok(html.includes(escaped("替换提案触发审计（P12 replacement trigger，ADR-0100）")));
  assert.ok(html.includes(escaped("1 轮触发器未到期（due = false）。")));
  assert.equal(count(html, "data-trigger-round="), 1);
  assert.ok(html.includes('data-trigger-round="round-trigger"'));
  assert.ok(!html.includes("data-trigger-problems"), "a well-formed summary lists no problems");
  assert.equal(count(html, 'data-trigger-outcome="proposed"'), 1);
  assert.equal(count(html, 'data-trigger-outcome="window_opened"'), 1);
  assert.equal(count(html, 'data-trigger-outcome="refused"'), 1);
  for (const text of [
    "format_version 2",
    "automation:loop:loop-x",
    CANDIDATE,
    "oos-2026q3",
    "[2026-07-01T00:00:00+00:00, 2026-10-01T00:00:00+00:00)",
    "666666666666…",
    "777777777777…",
    "1 report(s)",
    "eeeeeeeeeeee…",
    `${INCUMBENT} → ${CANDIDATE}`,
    "已提出替换提案（proposed）— PENDING_HUMAN_APPROVAL",
    "拒绝（refused）",
    "the global unsealing budget (2) is used up",
    "strategy:other@1.0.0（descends from no given incumbent）",
  ]) {
    assert.ok(html.includes(escaped(text)), text);
  }
  assert.ok(html.includes(`<strong>PENDING_HUMAN_APPROVAL</strong>`));
  assert.ok(PROPOSAL_HASH.startsWith("eeeeeeeeeeee"));
  // read-only: no controls that could change state
  assert.ok(!html.includes("<button") && !html.includes("<form") && !html.includes("<input"));
});

test("a trigger summary of an unknown format is flagged and shown raw, never hidden", async () => {
  const round = withTrigger(fixture, triggerSummary({ format_version: 3 }), "round-future");
  const { html } = await renderSettled(<ResearchLoop />, { routes: [api.listing(KIND, [round])] });
  assert.ok(html.includes('data-trigger-problems="1"'));
  assert.ok(html.includes(escaped("unrecognised trigger format_version 3")));
  assert.ok(html.includes(escaped("原始 replacement_trigger JSON")));
});
