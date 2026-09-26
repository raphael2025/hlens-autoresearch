import assert from "node:assert/strict";
import { test } from "node:test";
import type { JobList, JobView } from "../api";
import { api, count, escaped, renderInitial, renderSettled } from "../components/render.test-util.tsx";
import { Jobs } from "./Jobs.tsx";

// apps/web/fixtures holds report files only, so the GET /jobs body is inline (the same shape as
// src/lib/jobs.test.ts), typed by the OpenAPI schema.
const succeeded: JobView = {
  job_id: "a".repeat(64),
  name: "double",
  params: { x: 21 },
  status: "succeeded",
  attempts: 1,
  result: { doubled: 42 },
  error: null,
  starts: 1,
  reruns: 0,
  first_seq: 1,
  last_seq: 2,
};
const failed: JobView = {
  ...succeeded,
  job_id: "b".repeat(64),
  name: "boom",
  params: {},
  status: "failed",
  attempts: 2,
  result: null,
  error: "ValueError: bad input",
  first_seq: 3,
  last_seq: 4,
};
const interrupted: JobView = {
  ...succeeded,
  job_id: "c".repeat(64),
  name: "work",
  status: "interrupted",
  attempts: null,
  result: null,
  error: null,
  first_seq: 5,
  last_seq: 5,
};
const LIST: JobList = { jobs: [succeeded, failed, interrupted], head_hash: "f".repeat(64), lines: 5 };
const FILTER_BUTTONS = 4;

test("first paint: list loading, detail prompt, nothing fetched yet, no write controls", () => {
  const { html, requests } = renderInitial(<Jobs />);
  assert.deepEqual(requests, []);
  assert.ok(html.includes("加载任务列表中…"));
  assert.ok(html.includes("选择一个任务查看详情。"));
  assert.ok(!/提交|重试|取消|submit|retry|cancel/i.test(html));
});

test("loaded: journal summary, status counts, one row per job", async () => {
  const { html, requests } = await renderSettled(<Jobs />, { routes: [api.jobs(LIST)] });
  assert.deepEqual(requests, ["GET /api/jobs"]);
  assert.ok(html.includes(`5 行日志 · head_hash <code>${"f".repeat(12)}…</code>`));
  assert.ok(html.includes("succeeded 1 · failed 1 · interrupted 1"));
  assert.equal(count(html, "<button"), FILTER_BUTTONS + LIST.jobs.length);
  for (const job of LIST.jobs) assert.ok(html.includes(`<td>${job.name}</td>`), job.name);
  assert.ok(html.includes(escaped('{"doubled":42}')));
  assert.ok(html.includes("ValueError: bad input"));
  assert.ok(!/提交|重试|取消|submit|retry|cancel/i.test(html));
});

test("selected job: detail with params and result from GET /jobs/{job_id}", async () => {
  const { html, requests } = await renderSettled(<Jobs />, {
    routes: [api.jobs(LIST), api.job(succeeded)],
    selected: succeeded.job_id,
  });
  assert.deepEqual(requests.sort(), ["GET /api/jobs", `GET /api/jobs/${succeeded.job_id}`].sort());
  assert.ok(html.includes(`<code>${succeeded.job_id}</code>`));
  assert.ok(html.includes(`<pre>${escaped(JSON.stringify(succeeded.params, null, 2))}</pre>`));
  assert.ok(html.includes(`<pre>${escaped(JSON.stringify(succeeded.result, null, 2))}</pre>`));
  assert.ok(!html.includes("选择一个任务查看详情。"));
});

test("selected failed job: the error, never a result", async () => {
  const { html } = await renderSettled(<Jobs />, {
    routes: [api.jobs(LIST), api.job(failed)],
    selected: failed.job_id,
  });
  assert.ok(html.includes('<pre style="color:crimson">ValueError: bad input</pre>'));
  assert.ok(!html.includes("<h4>result</h4>"));
});

test("empty journal: the empty text", async () => {
  const { html } = await renderSettled(<Jobs />, {
    routes: [api.jobs({ jobs: [], head_hash: "0".repeat(64), lines: 0 })],
  });
  assert.ok(html.includes("（结果日志中还没有任务）"));
});

test("errors: 503 unconfigured journal and 500 tampered journal are shown, never partial data", async () => {
  const unconfigured = await renderSettled(<Jobs />, {
    routes: [api.fail("GET", "/jobs", 503, "no jobs journal configured")],
  });
  assert.ok(unconfigured.html.includes("后端未配置该数据源（HTTP 503）：no jobs journal configured"));

  const tampered = await renderSettled(<Jobs />, {
    routes: [api.fail("GET", "/jobs", 500, "journal hash chain broken at line 3")],
  });
  assert.ok(tampered.html.includes("服务端校验失败（HTTP 500）：journal hash chain broken at line 3"));
  assert.ok(!tampered.html.includes("<table>"));
});

test("detail error: the list stays, the detail shows the 404", async () => {
  const { html } = await renderSettled(<Jobs />, {
    routes: [api.jobs(LIST), api.fail("GET", `/jobs/${failed.job_id}`, 404, "unknown job")],
    selected: failed.job_id,
  });
  assert.equal(count(html, "<button"), FILTER_BUTTONS + LIST.jobs.length);
  assert.ok(html.includes("不存在（HTTP 404）：unknown job"));
});
