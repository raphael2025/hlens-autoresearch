import assert from "node:assert/strict";
import { test } from "node:test";
import type { ReportListing } from "../api";
import { fixtureEnvelopes } from "./fixtures.test-util.ts";
import { dataOf, invalidWarnings, listingSummary, reportsOf, type LoadState } from "./loadState.ts";

const reports = fixtureEnvelopes("validation_report");
const clean: ReportListing = { kind: "validation_report", reports, invalid: [] };
const withInvalid: ReportListing = {
  ...clean,
  invalid: [{ id: "broken", reason: "unreadable or not well-formed JSON" }],
};

test("a clean listing reports its count", () => {
  const state: LoadState<ReportListing> = { status: "ok", data: clean };
  assert.equal(listingSummary(state), String(reports.length));
  assert.deepEqual(reportsOf(state), reports);
  assert.equal(dataOf(state), clean);
});

test("invalid files are called out, never silently dropped", () => {
  const state: LoadState<ReportListing> = { status: "ok", data: withInvalid };
  assert.equal(listingSummary(state), `${reports.length}（另有 1 个无效文件）`);
  assert.deepEqual(invalidWarnings(withInvalid.invalid), ["broken: unreadable or not well-formed JSON"]);
  assert.deepEqual(invalidWarnings([]), []);
});

test("loading and error states are distinct from an empty listing", () => {
  assert.equal(listingSummary({ status: "loading" }), "…");
  assert.equal(listingSummary({ status: "idle" }), "…");
  assert.equal(listingSummary({ status: "error", error: "后端未配置该数据源（HTTP 503）：x" }), "错误：后端未配置该数据源（HTTP 503）：x");
  assert.equal(listingSummary({ status: "ok", data: { ...clean, reports: [] } }), "0");
  assert.deepEqual(reportsOf({ status: "error", error: "x" }), []);
  assert.equal(dataOf({ status: "loading" }), null);
});
