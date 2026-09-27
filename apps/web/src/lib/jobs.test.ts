import assert from "node:assert/strict";
import { test } from "node:test";
import type { JobList, JobView } from "../api";
import { compactJson, jobRow, jobRows, statusCounts } from "./jobs.ts";

// apps/web/fixtures holds report files only (one directory per ReportKind, guarded by
// tests/apps/test_console_fixtures.py), so the GET /jobs body is built inline here, with the exact
// shape tests/apps/test_api_jobs.py asserts for a real JobRunner journal.
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

test("rows show the result of a success and the error of a failure", () => {
  const [ok, bad, open] = jobRows(LIST);
  assert.equal(ok.shortId, "aaaaaaaaaaaa");
  assert.equal(ok.outcome, '{"doubled":42}');
  assert.equal(ok.attempts, "1");
  assert.equal(ok.lines, "1–2");
  assert.equal(bad.outcome, "ValueError: bad input");
  assert.equal(bad.attempts, "2");
  assert.equal(open.status, "interrupted");
  assert.equal(open.attempts, "—");
  assert.equal(open.outcome, "—");
  assert.equal(open.lines, "5");
  assert.match(open.statusLabel, /^interrupted/);
});

test("status counts include zeros and filtering keeps journal order", () => {
  assert.deepEqual(statusCounts(LIST), { succeeded: 1, failed: 1, interrupted: 1 });
  assert.deepEqual(statusCounts({ ...LIST, jobs: [] }), { succeeded: 0, failed: 0, interrupted: 0 });
  assert.deepEqual(
    jobRows(LIST, "failed").map((row) => row.name),
    ["boom"],
  );
  assert.deepEqual(jobRows({ ...LIST, jobs: [] }), []);
});

test("a failed job without an error message shows a placeholder, never a result", () => {
  assert.equal(jobRow({ ...failed, error: null, result: { leaked: true } }).outcome, "—");
});

test("long JSON values are truncated for the table", () => {
  const text = compactJson({ values: "x".repeat(200) }, 20);
  assert.equal(text.length, 20);
  assert.ok(text.endsWith("…"));
  assert.equal(compactJson(undefined), "—");
});
