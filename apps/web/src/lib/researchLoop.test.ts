import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asNumber,
  formatUsage,
  outcomeText,
  PENDING_HUMAN_APPROVAL,
  replacementTriggerOf,
  replacementTriggers,
  roundRow,
  roundRows,
  USAGE_KEYS,
  usageChartOption,
  usageSeries,
  withUnit,
} from "./researchLoop.ts";
import { CANDIDATE, INCUMBENT, PROPOSAL_HASH, triggerSummary, withTrigger } from "./replacementTrigger.test-util.ts";

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

// ---- P12 replacement trigger audit (optional evolution-stage summary key) ----------------------

test("the committed round has no evolution stage: no trigger view, its row unchanged", () => {
  assert.equal(replacementTriggerOf(fixture), null);
  assert.deepEqual(replacementTriggers([fixture]), []);
});

test("an evolution stage without the key (a loop without the trigger) shows nothing", () => {
  const round = withTrigger(fixture, undefined);
  const stages = round.payload.stages as Record<string, unknown>[];
  stages[stages.length - 1] = { ...stages[stages.length - 1], summary: { stage: "evolution" } };
  assert.equal(replacementTriggerOf(round), null);
});

test("a not-due round: a view with due = false and no rows", () => {
  const view = replacementTriggerOf(withTrigger(fixture, { due: false }));
  assert.ok(view !== null);
  assert.equal(view.due, false);
  assert.deepEqual(view.rows, []);
  assert.deepEqual(view.problems, []);
});

test("a due round: summary and every row read, proposals pending human approval", () => {
  const view = replacementTriggerOf(withTrigger(fixture, triggerSummary()));
  assert.ok(view !== null);
  assert.equal(view.due, true);
  assert.equal(view.formatVersion, 2);
  assert.equal(view.formatKnown, true);
  assert.equal(view.status, PENDING_HUMAN_APPROVAL);
  assert.equal(view.proposedBy, "automation:loop:loop-x");
  assert.deepEqual([view.familyTrials, view.unsealingCount, view.unsealingBudget], [4, 2, 2]);
  assert.deepEqual(view.problems, []);
  assert.deepEqual(
    view.rows.map((row) => [row.phase, row.candidate, row.windowId, row.outcome]),
    [
      ["evaluate", CANDIDATE, "oos-2026q3", "proposed"],
      ["open", "strategy:trend_a_child@1.2.0", "oos-2026q4", "window_opened"],
      ["open", "strategy:trend_a_child@1.3.0", null, "refused"],
    ],
  );
  const [proposed, opened, refused] = view.rows;
  assert.equal(proposed.openingHash, "6".repeat(64));
  assert.equal(proposed.consumptionHash, "7".repeat(64));
  assert.equal(proposed.consumedReports, 1);
  assert.equal(proposed.windowRange, "[2026-07-01T00:00:00+00:00, 2026-10-01T00:00:00+00:00)");
  assert.deepEqual(proposed.proposals, [
    { proposalHash: PROPOSAL_HASH, incumbent: INCUMBENT, candidate: CANDIDATE, status: PENDING_HUMAN_APPROVAL },
  ]);
  assert.equal(opened.openedAt, "2026-01-01T00:00:00+00:00");
  assert.equal(opened.consumptionHash, null);
  assert.match(refused.refusal ?? "", /global unsealing budget/);
  assert.deepEqual(view.notEligible, [
    { candidate: "strategy:other@1.0.0", reason: "descends from no given incumbent" },
  ]);
  assert.match(outcomeText("proposed"), /PENDING_HUMAN_APPROVAL/);
  assert.equal(outcomeText("something_new"), "something_new");
});

test("a failed job row carries its error; an unknown format or a non-pending status is flagged", () => {
  const summary = triggerSummary();
  const rows = summary.triggers as Record<string, unknown>[];
  rows[0] = { ...rows[0], status: "failed", job: null, error: { type: "ValueError", message: "boom" } };
  const failed = replacementTriggerOf(withTrigger(fixture, summary));
  assert.equal(failed?.rows[0].error, "ValueError: boom");
  assert.deepEqual(failed?.problems, []);

  const future = replacementTriggerOf(withTrigger(fixture, triggerSummary({ format_version: 3 })));
  assert.equal(future?.formatKnown, false);
  assert.ok(future?.problems.some((p) => p.includes("unrecognised trigger format_version 3")));

  const approved = triggerSummary();
  const job = (approved.triggers as Record<string, unknown>[])[0].job as Record<string, unknown>;
  (job.recorded as Record<string, unknown>[])[0].status = "APPROVED";
  const flagged = replacementTriggerOf(withTrigger(fixture, approved));
  assert.ok(flagged?.problems.some((p) => p.includes("status is APPROVED, not PENDING_HUMAN_APPROVAL")));
});

test("trigger views follow round order", () => {
  const later = withTrigger(fixture, triggerSummary(), "later");
  later.payload.round_index = 2;
  const earlier = withTrigger(fixture, { due: false }, "earlier");
  assert.deepEqual(
    replacementTriggers([later, fixture, earlier]).map((view) => view.id),
    ["earlier", "later"],
  );
});
