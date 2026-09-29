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

test("state_diagnostics matches its writer payload shape and required DTO fields", () => {
  const [report] = fixtureEnvelopes("state_diagnostics");
  assert.equal("diagnostics_hash" in report.payload, false);
  assert.deepEqual(inspectReportDTO(report), { status: "supported", version: "1.1.0" });

  for (const field of ["schema_version", "kind", "state_space"] as const) {
    const payload = { ...report.payload };
    delete payload[field];
    assert.deepEqual(inspectReportDTO({ ...report, payload }), {
      status: "invalid",
      version: "1.1.0",
      reason: `missing ${field}`,
    });
  }
});

test("validation_report 2.4.0 (ADR-0088's bumped default Contract envelope) is supported with the 2.3.0 shape", () => {
  // validation_report is the one ReportKind whose payload is a direct Contract.model_dump(); every
  // other kind's schema_version is an independent, domain-specific number unrelated to
  // core.domain.base.CONTRACT_SCHEMA_VERSION (see apps/api/report_dto.py). ADR-0088 (composed
  // strategies, event bar spec, peak equity, synthetic effects, volatility-scaling barrier) does
  // not touch ValidationReport's own fields, so 2.4.0 has the same required fields as 2.3.0 --
  // built inline (schema_version bumped on the committed fixture), not a new fixture file.
  const [report] = fixtureEnvelopes("validation_report");
  const bumped = { ...report, payload: { ...report.payload, schema_version: "2.4.0" } };
  assert.deepEqual(inspectReportDTO(bumped), { status: "supported", version: "2.4.0" });
});

test("validation_report 2.3.0 legacy payloads remain supported after registering 2.4.0", () => {
  const [report] = fixtureEnvelopes("validation_report");
  const bumped = { ...report, payload: { ...report.payload, schema_version: "2.3.0" } };
  assert.deepEqual(inspectReportDTO(bumped), { status: "supported", version: "2.3.0" });
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

test("paper deviation 2.0.0 with a well-formed declared_scope (scope_hash nested only) is supported", () => {
  // Matches the real payload shape from research/router/deviation.py's to_payload():
  // scope_hash lives only inside declared_scope, never at the payload's top level.
  const [report] = fixtureEnvelopes("paper_deviation");
  const payload = { ...report.payload };
  payload.schema_version = "2.0.0";
  payload.declared_scope = {
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
  const inspected = inspectReportDTO({ ...report, payload });
  assert.equal(inspected.status, "supported");
});

test("paper deviation 2.0.0 declared_scope missing its nested scope_hash is invalid", () => {
  const [report] = fixtureEnvelopes("paper_deviation");
  const payload = { ...report.payload };
  payload.schema_version = "2.0.0";
  const declaredScope: Record<string, unknown> = {
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
  delete declaredScope.scope_hash;
  payload.declared_scope = declaredScope;
  const inspected = inspectReportDTO({ ...report, payload });
  assert.equal(inspected.status, "invalid");
});
