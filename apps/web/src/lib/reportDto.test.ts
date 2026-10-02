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

test("virtual-version report kinds reject an in-payload schema_version", () => {
  for (const kind of ["research_loop_round", "router_paper_run", "router_stop"] as const) {
    const [report] = fixtureEnvelopes(kind);
    const inspected = inspectReportDTO({
      ...report,
      payload: { ...report.payload, schema_version: "1.0.0" },
    });
    assert.equal(inspected.status, "invalid", kind);
  }
});

test("validation_report 2.6.0 (ADR-0109's bumped default Contract envelope) is supported with the 2.3.0 shape", () => {
  // validation_report is the one ReportKind whose payload is a direct Contract.model_dump(); every
  // other kind's schema_version is an independent, domain-specific number unrelated to
  // core.domain.base.CONTRACT_SCHEMA_VERSION (see apps/api/report_dto.py). ADR-0094 (PIT
  // conflict evidence) and ADR-0109 (the v3 manifest's legacy Quality binding) do not change
  // ValidationReport's own fields, so 2.6.0 has the same required fields as 2.3.0 --
  // built inline (schema_version bumped on the committed fixture), not a new fixture file.
  const [report] = fixtureEnvelopes("validation_report");
  const bumped = { ...report, payload: { ...report.payload, schema_version: "2.6.0" } };
  assert.deepEqual(inspectReportDTO(bumped), { status: "supported", version: "2.6.0" });
});

test("validation_report 2.5.0 remains supported after registering 2.6.0", () => {
  const [report] = fixtureEnvelopes("validation_report");
  const bumped = { ...report, payload: { ...report.payload, schema_version: "2.5.0" } };
  assert.deepEqual(inspectReportDTO(bumped), { status: "supported", version: "2.5.0" });
});

test("validation_report 2.4.0 remains supported after registering 2.5.0 and 2.6.0", () => {
  const [report] = fixtureEnvelopes("validation_report");
  const bumped = { ...report, payload: { ...report.payload, schema_version: "2.4.0" } };
  assert.deepEqual(inspectReportDTO(bumped), { status: "supported", version: "2.4.0" });
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

// --- paper_deviation 2.1.0 (ADR-0104): scope 1.1.0 with a run binding -----------------------------

function runBoundPayload(): Record<string, unknown> {
  const [report] = fixtureEnvelopes("paper_deviation").filter((item) => item.payload.schema_version === "2.1.0");
  return JSON.parse(JSON.stringify(report.payload)) as Record<string, unknown>;
}

function inspected(payload: Record<string, unknown>) {
  const [report] = fixtureEnvelopes("paper_deviation");
  return inspectReportDTO({ ...report, payload });
}

test("paper deviation 2.1.0 with a well-formed run binding is supported", () => {
  assert.deepEqual(inspected(runBoundPayload()), { status: "supported", version: "2.1.0" });
});

test("paper deviation 2.1.0 must carry its scope-bound fields and a scope 1.1.0 with a run binding", () => {
  const noMarks = runBoundPayload();
  delete noMarks.marks;
  assert.equal(inspected(noMarks).status, "invalid");
  const noBinding = runBoundPayload();
  delete (noBinding.declared_scope as Record<string, unknown>).run_binding;
  assert.equal(inspected(noBinding).status, "invalid");
  const oldScope = runBoundPayload();
  (oldScope.declared_scope as Record<string, unknown>).scope_schema_version = "1.0.0";
  assert.equal(inspected(oldScope).status, "invalid");
  const badBinding = runBoundPayload();
  ((badBinding.declared_scope as Record<string, unknown>).run_binding as Record<string, unknown>).cost_model_hash = "1";
  const view = inspected(badBinding);
  assert.equal(view.status, "invalid");
  assert.ok(view.status === "invalid" && view.reason.includes("scope DTO 1.1.0"));
  const mismatched = runBoundPayload();
  mismatched.reference_request_hash = "0".repeat(64);
  assert.equal(inspected(mismatched).status, "invalid");
});

test("paper deviation 2.0.0 (scope-only) stays supported and cannot carry a run binding", () => {
  const legacy = fixtureEnvelopes("paper_deviation").find((item) => item.payload.schema_version === "2.0.0");
  assert.ok(legacy !== undefined);
  assert.deepEqual(inspectReportDTO(legacy), { status: "supported", version: "2.0.0" });
  const smuggled = JSON.parse(JSON.stringify(legacy.payload)) as Record<string, unknown>;
  (smuggled.declared_scope as Record<string, unknown>).run_binding = {};
  assert.equal(inspectReportDTO({ ...legacy, payload: smuggled }).status, "invalid");
});

test("paper deviation versions 1.0.0, 2.0.0 and 2.1.0 are supported; 2.2.0 is kept as unknown", () => {
  const [report] = fixtureEnvelopes("paper_deviation");
  const future = { ...report, payload: { ...report.payload, schema_version: "2.2.0" } };
  assert.deepEqual(inspectReportDTO(future), { status: "unknown-version", version: "2.2.0" });
});
