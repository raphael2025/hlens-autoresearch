import assert from "node:assert/strict";
import { test } from "node:test";
import type { LifecycleTransition } from "../api";
import { api, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { Lifecycle } from "./Lifecycle.tsx";

const TRANSITIONS: LifecycleTransition[] = [
  { from: "DRAFT", to: "CANDIDATE" },
  { from: "CANDIDATE", to: "REJECTED" },
];

test("first paint: loading, nothing fetched yet", () => {
  const { html, requests } = renderInitial(<Lifecycle />);
  assert.deepEqual(requests, []);
  assert.ok(html.includes("SIMULATED / NOT_VALIDATED"));
  assert.ok(html.includes("加载转移表中…"));
});

test("loaded: one row per allowed transition", async () => {
  const { html, requests } = await renderSettled(<Lifecycle />, {
    routes: [api.transitions(TRANSITIONS)],
  });
  assert.deepEqual(requests, ["GET /api/lifecycle/transitions"]);
  assert.ok(html.includes("<tr><td>DRAFT</td><td>CANDIDATE</td></tr>"));
  assert.ok(html.includes("<tr><td>CANDIDATE</td><td>REJECTED</td></tr>"));
});

test("empty: says the API returned no transition", async () => {
  const { html } = await renderSettled(<Lifecycle />, { routes: [api.transitions([])] });
  assert.ok(html.includes("（API 未返回任何允许的转移）"));
});

test("error: alert with the status meaning", async () => {
  const { html } = await renderSettled(<Lifecycle />, {
    routes: [api.unreachable("GET", "/lifecycle/transitions", "fetch failed")],
  });
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes("无法连接 API：fetch failed"));
});
