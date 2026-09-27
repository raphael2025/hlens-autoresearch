import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asNumber,
  formatUsage,
  roundRow,
  roundRows,
  USAGE_KEYS,
  usageChartOption,
  usageSeries,
  withUnit,
} from "./researchLoop.ts";

const [fixture] = fixtureEnvelopes("research_loop_round");

test("a real LoopRoundRecord fixture maps to its status, stages and usage", () => {
  const row = roundRow(fixture);
  assert.equal(row.id, fixture.id);
  assert.equal(row.loopId, "loop-x");
  assert.equal(row.roundIndex, 0);
  assert.equal(row.status, "COMPLETED");
  assert.equal(row.stagesRun, 6);
  assert.equal(row.stagesSkipped, 0);
  assert.deepEqual(row.problems, []);
  assert.equal(row.transitions, 0);
  // decimals are strings in the audit record ("0.5", "2"): parsed, never read as 0
  assert.deepEqual(row.roundUsage, { trials: 1, llm_cost_units: 0.5, compute_seconds: 2 });
  assert.deepEqual(row.totalUsage, { trials: 1, llm_cost_units: 0.5, compute_seconds: 2 });
  assert.equal(row.overrunStage, null);
  assert.equal(formatUsage(row.roundUsage), "trials 1 · llm 0.5 · compute 2s");
});

test("the fixture has no budget_used / failures keys (the fields the page used to read)", () => {
  assert.equal("budget_used" in fixture.payload, false);
  assert.equal("failures" in fixture.payload, false);
});

test("a failed round lists the failing stage with its error, and skipped stages", () => {
  const failed = clone(fixture);
  const stages = failed.payload.stages as Record<string, unknown>[];
  stages[3] = { ...stages[3], status: "FAILED", error: "RuntimeError: boom", summary: null };
  stages[4] = { ...stages[4], status: "SKIPPED" };
  stages[5] = { ...stages[5], status: "SKIPPED" };
  failed.payload.status = "FAILED";
  failed.payload.overrun = { stage: "experiment", amount: {} };
  const row = roundRow(failed);
  assert.equal(row.status, "FAILED");
  assert.deepEqual(row.problems, [{ name: "experiment", status: "FAILED", error: "RuntimeError: boom" }]);
  assert.equal(row.stagesRun, 4);
  assert.equal(row.stagesSkipped, 2);
  assert.equal(row.overrunStage, "experiment");
});

test("a payload missing fields degrades to placeholders, not to fake zeros", () => {
  const row = roundRow({ ...fixture, payload: {} });
  assert.equal(row.status, "—");
  assert.equal(row.roundIndex, null);
  assert.deepEqual(row.roundUsage, { trials: null, llm_cost_units: null, compute_seconds: null });
  assert.equal(row.overrunStage, null);
  assert.equal(formatUsage(row.roundUsage), "trials — · llm — · compute —s");
  assert.equal(asNumber("abc"), null);
  assert.equal(asNumber(""), null);
  assert.equal(asNumber(Number.NaN), null);
});

test("rows are ordered by loop then round index, and the chart series follow that order", () => {
  const later = clone(fixture);
  later.id = "later";
  later.payload.round_index = 1;
  (later.payload.round_usage as Record<string, unknown>).trials = 3;
  const rows = roundRows([later, fixture]);
  assert.deepEqual(
    rows.map((row) => row.id),
    [fixture.id, "later"],
  );
  const chart = usageSeries(rows);
  assert.deepEqual(chart.labels, ["loop-x#0", "loop-x#1"]);
  assert.deepEqual(chart.series.find((s) => s.key === "trials")?.round, [1, 3]);
});

test("one series per usage dimension, each with its own unit, round and cumulative total", () => {
  const later = clone(fixture);
  later.id = "later";
  later.payload.round_index = 1;
  later.payload.round_usage = { trials: 2, llm_cost_units: "0.25", compute_seconds: "30" };
  later.payload.total_usage = { trials: 3, llm_cost_units: "0.75", compute_seconds: "32" };
  const chart = usageSeries(roundRows([fixture, later]));
  assert.deepEqual(
    chart.series.map((s) => [s.key, s.unit, s.title]),
    [
      ["trials", "count", "trials (count)"],
      ["llm_cost_units", "cost units", "llm_cost_units (cost units)"],
      ["compute_seconds", "s", "compute_seconds (s)"],
    ],
  );
  assert.deepEqual(USAGE_KEYS, ["trials", "llm_cost_units", "compute_seconds"]);
  const compute = chart.series[2];
  assert.deepEqual(compute.round, [2, 30]);
  assert.deepEqual(compute.total, [2, 32]);
});

test("a dimension's chart: round bars on the left axis, cumulative line on the right, units everywhere", () => {
  const later = clone(fixture);
  later.id = "later";
  later.payload.round_index = 1;
  later.payload.total_usage = { trials: 2, llm_cost_units: "1", compute_seconds: "4" };
  const chart = usageSeries(roundRows([fixture, later]));
  for (const series of chart.series) {
    const option = usageChartOption(chart.labels, series);
    assert.deepEqual(option.xAxis.data, chart.labels);
    // two value axes: a growing total never flattens the per-round bars
    assert.deepEqual(
      option.yAxis.map((axis) => [axis.position, axis.name]),
      [
        ["left", `per round (${series.unit})`],
        ["right", `cumulative (${series.unit})`],
      ],
    );
    const [round, total] = option.series;
    assert.deepEqual([round.type, round.yAxisIndex, round.data], ["bar", 0, series.round]);
    assert.deepEqual([total.type, total.yAxisIndex, total.data], ["line", 1, series.total]);
    assert.equal(option.tooltip.valueFormatter(2), `2 ${series.unit}`);
    assert.equal(option.tooltip.valueFormatter(null), "—");
  }
  // never one numeric axis shared by incommensurable dimensions
  assert.equal(new Set(chart.series.map((s) => usageChartOption(chart.labels, s).yAxis[0].name)).size, 3);
});

test("withUnit: a number with its unit, anything else a placeholder", () => {
  assert.equal(withUnit(0.5, "cost units"), "0.5 cost units");
  assert.equal(withUnit(Number.NaN, "s"), "—");
  assert.equal(withUnit("2", "s"), "—");
});
