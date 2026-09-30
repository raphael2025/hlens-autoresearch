import assert from "node:assert/strict";
import { test } from "node:test";
import { asRetroAuditPayload, displayValue, retroAuditLabel } from "./retroAudit.ts";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";

// The one committed fixture (apps/web/fixtures/retro_audit/): no lifecycle transition was
// performed, no findings (research/validation/retro_audit.py's report-only shape). It exercises
// the empty-findings path; a finding-bearing payload is built inline below (as
// routerEligibility.test.ts does for its own evidence-mode shape) rather than adding a new
// fixture file.
const fixtures = fixtureEnvelopes("retro_audit");
const fixture = fixtures[0];

function payloadOf(payload: Record<string, unknown>) {
  const report = asRetroAuditPayload(payload);
  assert.ok(report !== null, "the payload parses as a retro_audit report");
  return report;
}

test("the real fixture parses: report-only, no findings, named by its report_hash", () => {
  assert.equal(fixtures.length, 1);
  const report = payloadOf(fixture.payload);
  assert.equal(report.report_hash, fixture.id);
  assert.equal(report.kind, "retro_audit");
  assert.equal(report.schema_version, "1.1.0");
  assert.equal(report.subjects, 0);
  assert.equal(report.flagged_for_revalidation, 0);
  assert.equal(report.rejected_that_would_now_pass, 0);
  assert.deepEqual(report.findings, []);
  assert.equal(report.rules, "fixture-only; no lifecycle transition");
  assert.equal(report.note, "report only: no lifecycle transition was performed");
});

test("retroAuditLabel: audited_at, subject count, and a truncated report_hash", () => {
  const report = payloadOf(fixture.payload);
  assert.equal(
    retroAuditLabel(report),
    `${report.audited_at} · ${report.subjects} subjects · ${report.report_hash.slice(0, 12)}…`,
  );
});

test("a payload with findings parses (inline, not a new fixture file)", () => {
  const withFindings = clone(fixture.payload);
  withFindings.subjects = 1;
  withFindings.flagged_for_revalidation = 1;
  withFindings.findings = [
    {
      subject: "strategy:trend_a@1.0.0",
      lifecycle_state: "rejected",
      recorded_verdict: "FAIL",
      current_verdict: "PASS",
      effective_verdict: "FAIL",
      action: "flagged_for_revalidation",
      would_now_pass: true,
      recorded_profile_hash: "a".repeat(64),
      current_profile_hash: "b".repeat(64),
      gate_diffs: [
        {
          gate_id: "G2.market_benchmark",
          recorded_metric: "sharpe",
          current_metric: "sharpe",
          recorded: "FAIL",
          current: "PASS",
          recorded_value_exact: "0.03",
          current_value_exact: "0.07",
          recorded_threshold_exact: "0.05",
          current_threshold_exact: "0.05",
          recorded_threshold_source: "profile",
          current_threshold_source: "profile",
        },
      ],
    },
  ];
  const report = payloadOf(withFindings);
  assert.equal(report.findings.length, 1);
  const [finding] = report.findings;
  assert.equal(finding.subject, "strategy:trend_a@1.0.0");
  assert.equal(finding.would_now_pass, true);
  assert.equal(finding.gate_diffs.length, 1);
  // effective verdict never flips to the recomputed current verdict (docstring: historical
  // rejections are never silently reversed by this report)
  assert.equal(finding.effective_verdict, "FAIL");
  assert.notEqual(finding.effective_verdict, finding.current_verdict);
});

test("a payload that is not a retro audit report is not parsed as one", () => {
  assert.equal(asRetroAuditPayload(undefined), null);
  assert.equal(asRetroAuditPayload({ kind: "retro_audit" }), null);
  const wrongKind = clone(fixture.payload);
  wrongKind.kind = "degradation_check";
  assert.equal(asRetroAuditPayload(wrongKind), null);
  const [deviation] = fixtureEnvelopes("paper_deviation");
  assert.equal(asRetroAuditPayload(deviation.payload), null);
});

test("findings must be well-formed: a malformed entry rejects the whole payload (fail closed)", () => {
  const badFinding = clone(fixture.payload);
  badFinding.subjects = 1;
  badFinding.findings = [{ subject: "strategy:trend_a@1.0.0" }]; // missing every other required field
  assert.equal(asRetroAuditPayload(badFinding), null);

  const badGateDiffs = clone(fixture.payload);
  badGateDiffs.subjects = 1;
  badGateDiffs.findings = [
    {
      subject: "s",
      lifecycle_state: "rejected",
      recorded_verdict: "FAIL",
      current_verdict: "FAIL",
      effective_verdict: "FAIL",
      action: "no_change",
      would_now_pass: false,
      gate_diffs: "not-an-array",
    },
  ];
  assert.equal(asRetroAuditPayload(badGateDiffs), null);
});

test("displayValue: null/undefined as an em dash, objects as JSON, everything else as text", () => {
  assert.equal(displayValue(null), "—");
  assert.equal(displayValue(undefined), "—");
  assert.equal(displayValue({ a: 1 }), '{"a":1}');
  assert.equal(displayValue([1, 2]), "[1,2]");
  assert.equal(displayValue("x"), "x");
  assert.equal(displayValue(3), "3");
  assert.equal(displayValue(true), "true");
});
