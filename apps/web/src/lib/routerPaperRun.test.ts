import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asRouterPayload,
  chartNumber,
  equitySeries,
  runLabel,
  shortTime,
  strategyKeys,
  weightSeries,
} from "./routerPaperRun.ts";

// apps/web/fixtures/router_paper_run/: the run of tests/research/router/test_paper.py, current (2.2.0)
// and the legacy readable 2.1.0 file (README "Legacy readable fixtures"; the run's bound hashes
// changed with the 2.2.0 envelope). It has no legacy 2.0.0 file.
const fixtures = fixtureEnvelopes("router_paper_run");
const [fixture] = fixtures;

function payloadOf(payload: Record<string, unknown>) {
  const run = asRouterPayload(payload);
  assert.ok(run !== null, "the fixture is a router paper run payload");
  return run;
}

test("every committed fixture parses, named by its run_hash", () => {
  assert.equal(fixtures.length, 2);
  for (const envelope of fixtures) {
    const run = payloadOf(envelope.payload);
    assert.equal(run.run_hash, envelope.id);
    assert.equal(runLabel(envelope.id, envelope.payload), `${run.router} (pnl ${run.pnl})`);
  }
});

test("weights: one stacked series per strategy, a missing weight is 0; turnover per decision", () => {
  const run = payloadOf(fixture.payload);
  assert.deepEqual(strategyKeys(run), ["strategy:revert_b@1.0.0", "strategy:trend_a@1.0.0"]);
  const series = weightSeries(run);
  assert.deepEqual(series.categories, [
    "2026-01-01 00:01:00Z",
    "2026-01-01 00:02:00Z",
    "2026-01-01 00:03:00Z",
    "2026-01-01 00:04:00Z",
    "2026-01-01 00:05:00Z",
  ]);
  assert.deepEqual(series.weights, [
    { key: "strategy:revert_b@1.0.0", data: [0, 0, 0.5, 0.5, 0] },
    { key: "strategy:trend_a@1.0.0", data: [1, 1, 0, 0, 0] },
  ]);
  assert.deepEqual(series.turnover, [1, 0, 1.5, 0, 0.5]);
});

test("equity: gross and net per mark, net ends at final_equity", () => {
  const run = payloadOf(fixture.payload);
  const series = equitySeries(run);
  assert.equal(series.gross.length, run.gross_equity_curve.length);
  assert.equal(series.net.length, run.net_equity_curve.length);
  assert.equal(series.gross[0], 1000);
  assert.equal(series.net.at(-1), Number(run.final_equity));
  assert.ok(series.net.every((net, index) => net <= series.gross[index]), "net never above gross");
});

test("helpers: time labels keep the date, unparseable numbers chart as 0", () => {
  assert.equal(shortTime("2026-01-01T00:01:00+00:00"), "2026-01-01 00:01:00Z");
  assert.equal(chartNumber(undefined), 0);
  assert.equal(chartNumber("abc"), 0);
  assert.equal(chartNumber("0E-18"), 0);
});

test("a payload that is not a router paper run is not parsed as one", () => {
  assert.equal(asRouterPayload(undefined), null);
  assert.equal(asRouterPayload({ decisions: [] }), null); // no router / run_hash / curves
  const noCurve = clone(fixture.payload);
  delete noCurve.net_equity_curve;
  assert.equal(asRouterPayload(noCurve), null);
  const badDecision = clone(fixture.payload);
  badDecision.decisions = [{ at: "t" }]; // no weights
  assert.equal(asRouterPayload(badDecision), null);
  const [stop] = fixtureEnvelopes("router_stop");
  assert.equal(asRouterPayload(stop.payload), null);
  assert.equal(runLabel("abc", {}), "abc");
});
