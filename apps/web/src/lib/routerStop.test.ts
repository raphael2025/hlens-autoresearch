import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import { asRouterStopPayload, reasonText, routerStopLabel, strategyRows } from "./routerStop.ts";

const [fixture] = fixtureEnvelopes("router_stop");

function payloadOf(payload: Record<string, unknown>) {
  const stop = asRouterStopPayload(payload);
  assert.ok(stop !== null, "the fixture is a router stop payload");
  return stop;
}

test("the real fixture parses: an all-routes-flat stop named by its stop_hash", () => {
  const stop = payloadOf(fixture.payload);
  assert.equal(stop.reason, "all_routes_flat");
  assert.equal(stop.stop_hash, fixture.id);
  assert.equal(stop.validation_reports, null);
  assert.equal(routerStopLabel(stop), `${stop.router} — all_routes_flat`);
  assert.match(reasonText(stop.reason), /权重都为零（all_routes_flat）$/);
});

test("one row per strategy, sorted, lifecycle and result hash side by side", () => {
  const stop = payloadOf(fixture.payload);
  const rows = strategyRows(stop);
  assert.deepEqual(
    rows.map((row) => row.strategy),
    Object.keys(stop.lifecycle).sort(),
  );
  for (const row of rows) {
    assert.equal(row.lifecycle, stop.lifecycle[row.strategy]);
    assert.equal(row.result_hash, stop.strategy_result_hashes[row.strategy]);
    assert.equal(row.validation_report, null); // none supplied
  }
});

test("supplied validation reports and strategies missing a field show up, as null cells", () => {
  const edited = clone(fixture.payload);
  const stop = payloadOf(edited);
  const [first] = Object.keys(stop.lifecycle).sort();
  stop.validation_reports = { [first]: "f".repeat(64), "strategy:extra@1.0.0": "e".repeat(64) };
  const rows = strategyRows(stop);
  assert.equal(rows.find((row) => row.strategy === first)?.validation_report, "f".repeat(64));
  const extra = rows.find((row) => row.strategy === "strategy:extra@1.0.0");
  assert.deepEqual(extra, {
    strategy: "strategy:extra@1.0.0",
    lifecycle: null,
    result_hash: null,
    validation_report: "e".repeat(64),
  });
});

test("reasons: both known reasons are described; an unknown one is shown verbatim", () => {
  assert.match(reasonText("no_validated_candidate"), /（no_validated_candidate）$/);
  assert.equal(reasonText("something_new"), "something_new");
});

test("a payload that is not a router stop is not parsed as one", () => {
  assert.equal(asRouterStopPayload(undefined), null);
  assert.equal(asRouterStopPayload({ reason: "all_routes_flat" }), null);
  const noLifecycle = clone(fixture.payload);
  noLifecycle.lifecycle = ["not", "a", "map"];
  assert.equal(asRouterStopPayload(noLifecycle), null);
  // a router paper run is a different kind, never mistaken for a stop
  const [run] = fixtureEnvelopes("router_paper_run");
  assert.equal(asRouterStopPayload(run.payload), null);
});
