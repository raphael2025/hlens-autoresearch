import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asEventStatisticsPayload,
  formatMicros,
  statisticsLabel,
  statisticView,
} from "./eventStatistics.ts";

const [fixture] = fixtureEnvelopes("event_statistics");

function payloadOf(payload: Record<string, unknown>) {
  const report = asEventStatisticsPayload(payload);
  assert.ok(report !== null, "the fixture is an event statistics payload");
  return report;
}

function field(view: ReturnType<typeof statisticView>, name: string): string | undefined {
  return view.fields.find(([key]) => key === name)?.[1];
}

// The fixture is the four statistics of tests/research/events/test_event_stats.py (hand-checked
// there) bound to two event runs.
test("the real fixture parses: four statistics bound to two runs by report_hash", () => {
  const report = payloadOf(fixture.payload);
  assert.equal(report.report_hash, fixture.id);
  assert.equal(statisticsLabel(report), "4 statistics · 2 runs");
  assert.deepEqual(
    report.statistics.map((stat) => statisticView(stat).kind),
    ["event_frequency", "co_occurrence", "lead_lag", "overlap_diagnostics"],
  );
});

test("frequency buckets and lead-lag bins become histogram rows; durations are readable", () => {
  const [frequency, coOccurrence, leadLag, overlap] = payloadOf(fixture.payload).statistics.map(statisticView);
  assert.deepEqual(frequency.histogram.map((row) => row.count), [2, 2, 1]);
  assert.equal(field(frequency, "per_day"), "120");
  assert.equal(field(coOccurrence, "window"), "2min");
  assert.equal(field(coOccurrence, "lift"), "2");
  assert.equal(field(leadLag, "max_lag"), "5min");
  assert.deepEqual(
    leadLag.histogram.filter((row) => row.count > 0).map((row) => `${row.label}=${row.count}`),
    ["1min=1", "2min=1"],
  );
  assert.equal(leadLag.histogram[0].label, "-5min");
  assert.equal(field(overlap, "independent_count"), "4");
  assert.equal(field(overlap, "horizon"), "5min");
});

test("undefined ratios are a dash, never 0; unknown statistic kinds are shown, not dropped", () => {
  const edited = clone(fixture.payload);
  const report = payloadOf(edited);
  const co = report.statistics[1] as { lift: string | null };
  co.lift = null;
  assert.equal(field(statisticView(report.statistics[1]), "lift"), "—");
  const unknown = statisticView({ kind: "new_statistic", n: 3, nested: { x: 1 }, gap: null });
  assert.equal(unknown.title, "new_statistic");
  assert.deepEqual(unknown.fields, [
    ["n", "3"],
    ["nested", '{"x":1}'],
    ["gap", "—"],
  ]);
});

test("formatMicros picks the largest exact unit", () => {
  assert.equal(formatMicros(0), "0s");
  assert.equal(formatMicros(300_000_000), "5min");
  assert.equal(formatMicros(-60_000_000), "-1min");
  assert.equal(formatMicros(86_400_000_000), "1d");
  assert.equal(formatMicros(1_500_000), "1500ms");
  assert.equal(formatMicros(7), "7µs");
});

test("a payload that is not an event statistics report is not parsed as one", () => {
  assert.equal(asEventStatisticsPayload(undefined), null);
  assert.equal(asEventStatisticsPayload({ ...clone(fixture.payload), kind: "state_diagnostics" }), null);
  assert.equal(asEventStatisticsPayload({ ...clone(fixture.payload), statistics: {} }), null);
});
