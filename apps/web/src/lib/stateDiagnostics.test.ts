import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asStateDiagnosticsPayload,
  cell,
  diagnosticsLabel,
  flickerSummary,
  stateRows,
  transitionRows,
} from "./stateDiagnostics.ts";

const [fixture] = fixtureEnvelopes("state_diagnostics");

function payloadOf(payload: Record<string, unknown>) {
  const report = asStateDiagnosticsPayload(payload);
  assert.ok(report !== null, "the fixture is a state diagnostics payload");
  return report;
}

// The fixture is diagnose(a a b None b b a b, ("a", "b"), min_run=2) — hand-checked in
// tests/research/states/test_state_diagnostics.py.
test("per-state rows follow state_space and carry the exact decimal text", () => {
  const report = payloadOf(fixture.payload);
  assert.deepEqual(stateRows(report), [
    { state: "a", count: 3, share: "0.428571", mean_steps: "1.500000", max_steps: 2, runs: 2 },
    { state: "b", count: 4, share: "0.571429", mean_steps: "1.333333", max_steps: 2, runs: 3 },
  ]);
  assert.equal(diagnosticsLabel(report), "a / b · 8 evals · min_run 2");
});

test("the transition matrix is square in state_space order", () => {
  const rows = transitionRows(payloadOf(fixture.payload));
  assert.deepEqual(
    rows.map((row) => [row.from, row.cells.map((c) => `${c.to}:${c.count}:${c.probability}`)]),
    [
      ["a", ["a:1:0.333333", "b:2:0.666667"]],
      ["b", ["a:1:0.500000", "b:1:0.500000"]],
    ],
  );
});

test("flicker: runs shorter than min_run and the switch rate", () => {
  assert.deepEqual(flickerSummary(payloadOf(fixture.payload)), {
    min_run: 2,
    runs: 5,
    short_runs: 3,
    short_run_share: "0.600000",
    switch_rate: "0.600000",
  });
});

test("undefined values stay undefined: a dash, never 0", () => {
  const edited = clone(fixture.payload);
  const report = payloadOf(edited);
  report.shares.a = null;
  report.mean_steps.b = null;
  report.switch_rate = null;
  const [a, b] = stateRows(report);
  assert.equal(a.share, null);
  assert.equal(cell(a.share), "—");
  assert.equal(cell(b.mean_steps), "—");
  assert.equal(cell(0), "0");
  assert.equal(flickerSummary(report).switch_rate, null);
  // a state missing from a per-state map is null, not a crash
  delete (report.transitions as Record<string, unknown>).b;
  assert.equal(transitionRows(report)[1].cells[0].count, null);
});

test("a payload that is not state diagnostics is not parsed as one", () => {
  assert.equal(asStateDiagnosticsPayload(undefined), null);
  assert.equal(asStateDiagnosticsPayload({ ...clone(fixture.payload), kind: "other" }), null);
  const [matrix] = fixtureEnvelopes("state_strategy_matrix");
  assert.equal(asStateDiagnosticsPayload(matrix.payload), null);
});
