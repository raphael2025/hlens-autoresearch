import assert from "node:assert/strict";
import { test } from "node:test";
import { api, count, escaped, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { formatUsage, roundRow } from "../lib/researchLoop.ts";
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
