import assert from "node:assert/strict";
import { test } from "node:test";
import { REPORT_KINDS, type Health } from "../api";
import { api, escaped, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { Dashboard } from "./Dashboard.tsx";

// The Dashboard counts every ReportKind over the real fixtures; each cell shows its own state.

const HEALTH: Health = { status: "ok", api_version: "0.9.0" };
const CONTRACTS = ["Experiment", "ValidationReport", "Strategy"];

function row(kind: string, cell: string): string {
  return `<tr><td>${kind}</td><td>${escaped(cell)}</td></tr>`;
}

test("first paint: every cell is loading and nothing is fetched yet", () => {
  const { html, requests } = renderInitial(<Dashboard />);
  assert.deepEqual(requests, []);
  assert.ok(html.includes("SIMULATED / NOT_VALIDATED"));
  assert.ok(html.includes("加载健康检查中…"));
  assert.ok(html.includes("加载契约清单中…"));
  for (const kind of REPORT_KINDS) assert.ok(html.includes(row(kind, "…")), kind);
});

test("loaded: health, contract count and a per-kind count over the fixtures", async () => {
  const [first, ...rest] = REPORT_KINDS;
  const { html, requests } = await renderSettled(<Dashboard />, {
    routes: [
      api.health(HEALTH),
      api.contracts(CONTRACTS),
      api.listing(first, fixtureEnvelopes(first), [{ id: "bad", reason: "invalid" }]),
      ...rest.map((kind) => api.listing(kind, fixtureEnvelopes(kind))),
    ],
  });
  assert.equal(requests.length, 2 + REPORT_KINDS.length);
  assert.ok(html.includes("ok (v0.9.0)"));
  assert.ok(html.includes("<td>3</td>"));
  assert.ok(html.includes(row(first, `${fixtureEnvelopes(first).length}（另有 1 个无效文件）`)));
  for (const kind of rest) {
    assert.ok(html.includes(row(kind, String(fixtureEnvelopes(kind).length))), kind);
  }
  assert.ok(!html.includes('role="alert"'));
});

test("one failing kind is named in its own cell and does not hide the others", async () => {
  const [failing, ...rest] = REPORT_KINDS;
  const { html } = await renderSettled(<Dashboard />, {
    routes: [
      api.health(HEALTH),
      api.contracts(CONTRACTS),
      api.fail("GET", `/reports/${failing}`, 500, "store check failed"),
      ...rest.map((kind) => api.listing(kind, [])),
    ],
  });
  assert.ok(
    html.includes(
      `<tr><td>${failing}</td><td style="color:crimson">${escaped("错误：服务端校验失败（HTTP 500）：store check failed")}</td></tr>`,
    ),
  );
  for (const kind of rest) assert.ok(html.includes(row(kind, "0")), kind);
});

test("health / contracts errors are alerts, not a silent 'down'", async () => {
  const { html } = await renderSettled(<Dashboard />, {
    routes: [
      api.unreachable("GET", "/health", "connect ECONNREFUSED 127.0.0.1:8000"),
      api.fail("GET", "/contracts", 503, "not configured"),
      ...REPORT_KINDS.map((kind) => api.listing(kind, [])),
    ],
  });
  assert.ok(html.includes("无法连接 API：connect ECONNREFUSED 127.0.0.1:8000"));
  assert.ok(html.includes("后端未配置该数据源（HTTP 503）：not configured"));
});
