import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import { asValidationReportPayload, gateRows, shownNumber, validationLabel } from "./validationReport.ts";

// apps/web/fixtures/validation_report/: current 2.6.0, retained 2.5.0 / 2.4.0 / 2.2.0, and legacy
// readable 2.1.0 / 2.0.0 reports (README "Legacy readable fixtures").
const fixtures = fixtureEnvelopes("validation_report");
const byVersion = (version: string) => {
  const found = fixtures.find((envelope) => envelope.payload.schema_version === version);
  assert.ok(found !== undefined, `a ${version} validation_report fixture`);
  return found;
};
const current = byVersion("2.6.0");
const legacy21 = byVersion("2.1.0");
const legacy = byVersion("2.0.0");

function payloadOf(payload: Record<string, unknown>) {
  const report = asValidationReportPayload(payload);
  assert.ok(report !== null, "the fixture is a validation report payload");
  return report;
}

test("every committed fixture parses: a PASS verdict and their gates", () => {
  assert.equal(fixtures.length, 6);
  for (const envelope of fixtures) {
    const report = payloadOf(envelope.payload);
    assert.equal(report.verdict, "PASS");
    assert.ok(report.gates.length > 0);
    assert.equal(validationLabel(envelope.id, envelope.payload), `${envelope.id.slice(0, 12)}… [PASS]`);
  }
});

test("2.1.0+: an exact gate shows value_exact / threshold_exact, never the derived float", () => {
  for (const envelope of [current, legacy21]) {
    const rows = gateRows(payloadOf(envelope.payload));
    const exact = rows.find((row) => row.representation === "exact");
    assert.ok(exact !== undefined);
    assert.equal(exact.gateId, "G3.adjusted_p_value");
    assert.deepEqual(exact.value, { text: "0.0300000000000000001", exact: true });
    assert.deepEqual(exact.threshold, { text: "0.05", exact: true });
    assert.equal(exact.thresholdSource, "significance.multiple_testing_threshold_exact");
    // the float on the wire is only float(exact): showing it would drop the last digit
    const wire = (envelope.payload.gates as Record<string, unknown>[]).find((gate) => "value_exact" in gate);
    assert.equal(wire?.value, 0.03);
    assert.notEqual(exact.value.text, String(wire?.value));
  }
});

test("2.1.0+: a float-only gate still shows its floats", () => {
  for (const envelope of [current, legacy21]) {
    const rows = gateRows(payloadOf(envelope.payload));
    const float = rows.find((row) => row.representation === "float");
    assert.ok(float !== undefined);
    assert.deepEqual(float.value, { text: "10", exact: false });
    assert.deepEqual(float.threshold, { text: "5", exact: false });
  }
});

test("2.0.0 legacy: no exact keys, the floats are shown", () => {
  const rows = gateRows(payloadOf(legacy.payload));
  assert.deepEqual(
    rows.map((row) => [row.gateId, row.value.text, row.threshold.text, row.representation, row.verdict]),
    [["G2.effective_sample_size", "10", "5", "float", "PASS"]],
  );
});

test("a gate without a threshold shows —; missing fields degrade to —, never to 0", () => {
  const edited = clone(current.payload);
  edited.gates = [{ gate_id: "G0.report_only", metric: "m", value: 1.5, threshold: null, verdict: "PASS" }, {}];
  const [reportOnly, empty] = gateRows(payloadOf(edited));
  assert.deepEqual(reportOnly.threshold, { text: "—", exact: false });
  assert.equal(reportOnly.thresholdSource, "—");
  assert.deepEqual(empty.value, { text: "—", exact: false });
  assert.equal(empty.gateId, "—");
  assert.deepEqual(shownNumber("", 0.5), { text: "0.5", exact: false });
  assert.deepEqual(shownNumber(undefined, Number.NaN), { text: "—", exact: false });
});

test("a payload that is not a validation report is not parsed as one", () => {
  assert.equal(asValidationReportPayload(undefined), null);
  assert.equal(asValidationReportPayload({ verdict: "PASS" }), null);
  assert.equal(asValidationReportPayload({ verdict: "PASS", gates: [1] }), null);
  assert.equal(asValidationReportPayload({ gates: [] }), null);
  const [matrix] = fixtureEnvelopes("state_strategy_matrix");
  assert.equal(asValidationReportPayload(matrix.payload), null);
  assert.equal(validationLabel("abc", { verdict: 1 }), "abc");
});
