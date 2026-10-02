import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asPaperDeviationPayload,
  BINDING_LABELS,
  bindingKind,
  chartSeries,
  decimalText,
  deviationLabel,
  percent,
  runBindingRows,
  summaryRows,
} from "./paperDeviation.ts";

const [fixture] = fixtureEnvelopes("paper_deviation");

function payloadOf(payload: Record<string, unknown>) {
  const report = asPaperDeviationPayload(payload);
  assert.ok(report !== null, "the fixture is a paper deviation payload");
  return report;
}

test("the real fixture parses: named by its deviation_hash, one mark per equity point", () => {
  const report = payloadOf(fixture.payload);
  assert.equal(report.deviation_hash, fixture.id);
  assert.equal(report.kind, "paper_deviation");
  assert.deepEqual(report.instruments, ["BTC"]);
  assert.equal(report.marks.length, report.summary.marks);
  assert.equal(report.router, "vol_router@1.0.0");
  // the written fixture's hand-checked marks (tests/research/router/test_paper_deviation.py)
  const [first, second, third] = report.marks;
  assert.equal(decimalText(first.equity_difference), "0");
  assert.equal(decimalText(second.paper_equity), "990");
  assert.equal(decimalText(second.equity_difference), "-10");
  assert.equal(decimalText(third.reference_equity), "1100");
});

test("decimalText shows the exact digits without exponent or trailing zeros", () => {
  assert.equal(decimalText("0E-18"), "0");
  assert.equal(decimalText("-0E-18"), "0");
  assert.equal(decimalText("-10.000000000000000000"), "-10");
  assert.equal(decimalText("0.101010101010101010"), "0.10101010101010101");
  assert.equal(decimalText("1.5E-3"), "0.0015");
  assert.equal(decimalText("1E+2"), "100");
  assert.equal(decimalText("-0.010000000000000000"), "-0.01");
  assert.equal(decimalText(null), "—");
  assert.equal(decimalText("NaN"), "NaN"); // not a plain decimal: verbatim
});

test("percent is a label only; null stays not computable", () => {
  assert.equal(percent("-0.01"), "-1.0000%");
  assert.equal(percent("0.1", 1), "10.0%");
  assert.equal(percent(null), "—");
});

test("summary rows keep every statistic, in reading order, with exact text", () => {
  const report = payloadOf(fixture.payload);
  const rows = summaryRows(report.summary);
  assert.equal(rows.length, 12);
  assert.equal(rows[0].value, String(report.summary.marks));
  const final = rows.find((row) => row.label.startsWith("final_equity_difference"));
  assert.equal(final?.value, decimalText(report.summary.final_equity_difference));
  const widest = rows.find((row) => row.label === "max_abs_equity_difference");
  assert.ok(widest?.value.endsWith(`@ ${report.summary.max_abs_equity_difference_at}`));
  const tracking = rows.find((row) => row.label.startsWith("tracking_error"));
  assert.equal(tracking?.value, decimalText(report.summary.tracking_error));
});

test("a not-computable statistic is shown as —, never 0", () => {
  const edited = clone(fixture.payload);
  const report = payloadOf(edited);
  report.summary.tracking_error = null;
  report.summary.mean_return_difference = null;
  const rows = summaryRows(report.summary);
  assert.equal(rows.find((row) => row.label.startsWith("tracking_error"))?.value, "—");
  assert.equal(rows.find((row) => row.label === "mean_return_difference")?.value, "—");
});

test("chart series align with the marks", () => {
  const report = payloadOf(fixture.payload);
  const series = chartSeries(report);
  assert.equal(series.times.length, report.marks.length);
  assert.deepEqual(series.times, report.marks.map((mark) => mark.time));
  series.paper.forEach((value, index) => {
    // float approximations of exact decimals: equal up to rounding
    assert.ok(Math.abs(value - series.reference[index] - series.difference[index]) < 1e-9);
  });
  assert.match(deviationLabel(report), /^vol_router@1\.0\.0 vs .+ \(Δ final -?[\d.]+\)$/);
});

test("a payload that is not a paper deviation is not parsed as one", () => {
  assert.equal(asPaperDeviationPayload(undefined), null);
  assert.equal(asPaperDeviationPayload({ kind: "paper_deviation" }), null);
  const noSummary = clone(fixture.payload);
  noSummary.summary = [];
  assert.equal(asPaperDeviationPayload(noSummary), null);
  const [run] = fixtureEnvelopes("router_paper_run");
  assert.equal(asPaperDeviationPayload(run.payload), null);
});

// --- ADR-0104: scope-only (legacy) vs run-bound reports ----------------------------------------

const byVersion = (version: string) =>
  fixtureEnvelopes("paper_deviation").filter((item) => item.payload.schema_version === version);

test("the committed fixtures cover every generation: unscoped 1.0.0, scope-only 2.0.0, run-bound 2.1.0", () => {
  assert.equal(byVersion("1.0.0").length, 2);
  assert.equal(byVersion("2.0.0").length, 4);
  // one run-bound report per contract generation: 2.1.0, 2.2.0, 2.4.0, 2.5.0 and current 2.6.0
  assert.equal(byVersion("2.1.0").length, 5);
  for (const [version, kind] of [["1.0.0", "unscoped"], ["2.0.0", "scope_only"], ["2.1.0", "run_bound"]] as const) {
    for (const item of byVersion(version)) {
      assert.equal(bindingKind(payloadOf(item.payload)), kind, `${version} ${item.id}`);
    }
  }
});

test("a run-bound report shows its binding rows; the other generations show none", () => {
  for (const item of byVersion("2.1.0")) {
    const report = payloadOf(item.payload);
    const rows = runBindingRows(report);
    assert.deepEqual(
      rows.map((row) => row.label),
      ["router_spec_hash", "router_strategy_spec_hash", "experiment_hash", "bars_hash", "window",
        "cost_model_hash", "reference_request_hash"],
    );
    const binding = report.declared_scope?.run_binding;
    assert.ok(binding !== undefined);
    assert.equal(rows.find((row) => row.label === "reference_request_hash")?.value, report.reference_request_hash);
    assert.equal(rows.find((row) => row.label === "window")?.value, `${binding.window_start} → ${binding.window_end}`);
  }
  for (const item of [...byVersion("2.0.0"), ...byVersion("1.0.0")]) {
    assert.deepEqual(runBindingRows(payloadOf(item.payload)), []);
  }
});

test("the three generations are labelled apart, and scope-only is never presented as run-bound", () => {
  assert.equal(new Set(Object.values(BINDING_LABELS)).size, 3);
  assert.match(BINDING_LABELS.scope_only, /scope-only/);
  assert.match(BINDING_LABELS.scope_only, /不可作为可比证据/);
  assert.match(BINDING_LABELS.run_bound, /带运行绑定/);
});

test("a 2.1.0 payload without a (well-formed) run binding is not parsed as a deviation", () => {
  const [current] = byVersion("2.1.0");
  const good = clone(current.payload);
  assert.ok(asPaperDeviationPayload(good) !== null);
  const dropped = clone(current.payload);
  delete (dropped.declared_scope as Record<string, unknown>).run_binding;
  assert.equal(asPaperDeviationPayload(dropped), null);
  const badHash = clone(current.payload);
  ((badHash.declared_scope as Record<string, unknown>).run_binding as Record<string, unknown>).bars_hash = "x";
  assert.equal(asPaperDeviationPayload(badHash), null);
  const otherRequest = clone(current.payload);
  otherRequest.reference_request_hash = "0".repeat(64);
  assert.equal(asPaperDeviationPayload(otherRequest), null);
  const backwards = clone(current.payload);
  const binding = (backwards.declared_scope as Record<string, unknown>).run_binding as Record<string, unknown>;
  [binding.window_start, binding.window_end] = [binding.window_end, binding.window_start];
  assert.equal(asPaperDeviationPayload(backwards), null);
  const wrongScope = clone(current.payload);
  (wrongScope.declared_scope as Record<string, unknown>).scope_schema_version = "1.0.0";
  assert.equal(asPaperDeviationPayload(wrongScope), null);
});

test("a scope-only 2.0.0 payload stays readable and cannot carry a run binding", () => {
  const [legacy] = byVersion("2.0.0");
  assert.ok(asPaperDeviationPayload(clone(legacy.payload)) !== null);
  const smuggled = clone(legacy.payload);
  (smuggled.declared_scope as Record<string, unknown>).run_binding = {};
  assert.equal(asPaperDeviationPayload(smuggled), null);
});
