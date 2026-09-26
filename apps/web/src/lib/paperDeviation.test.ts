import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asPaperDeviationPayload,
  chartSeries,
  decimalText,
  deviationLabel,
  percent,
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
