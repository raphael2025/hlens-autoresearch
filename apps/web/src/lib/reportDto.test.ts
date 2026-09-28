import assert from "node:assert/strict";
import { test } from "node:test";
import { fixtureEnvelopes } from "./fixtures.test-util.ts";
import { inspectReportDTO } from "./reportDto.ts";

test("all current and retained legacy report fixtures resolve to a supported DTO", () => {
  for (const kind of [
    "validation_report", "research_loop_round", "state_strategy_matrix", "router_paper_run",
    "gate_calibration", "router_stop", "state_diagnostics", "event_statistics",
    "paper_deviation", "degradation_check", "retro_audit",
  ] as const) {
    for (const report of fixtureEnvelopes(kind)) {
      assert.deepEqual(inspectReportDTO(report).status, "supported", `${kind}/${report.id}`);
    }
  }
});

test("unknown schema versions are preserved as raw read-only data", () => {
  const [report] = fixtureEnvelopes("paper_deviation");
  const future = { ...report, payload: { ...report.payload, schema_version: "99.0.0" } };
  assert.deepEqual(inspectReportDTO(future), { status: "unknown-version", version: "99.0.0" });
});

test("paper deviation 2.0.0 must carry its declared-scope DTO fields", () => {
  const [report] = fixtureEnvelopes("paper_deviation");
  const badPayload = { ...report.payload };
  badPayload.schema_version = "2.0.0";
  badPayload.declared_scope = {
    scope_schema_version: "1.0.0",
    validation_profile: "profile@1.0.0",
    validation_profile_hash: "a".repeat(64),
    validation_report_hash: "b".repeat(64),
    venue: "test",
    symbol: "BTC",
    timeframe: "1d",
    research_class: "test",
    scope_hash: "c".repeat(64),
  };
  delete badPayload.declared_scope;
  const inspected = inspectReportDTO({ ...report, payload: badPayload });
  assert.equal(inspected.status, "invalid");
});
