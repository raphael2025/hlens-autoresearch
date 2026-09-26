import assert from "node:assert/strict";
import { test } from "node:test";
import {
  asDegradationCheckPayload,
  checkSummary,
  degradationLabel,
  directionText,
  metricRows,
  metricStatus,
  statusText,
} from "./degradationCheck.ts";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";

const [fixture] = fixtureEnvelopes("degradation_check");

function payloadOf(payload: Record<string, unknown>) {
  const check = asDegradationCheckPayload(payload);
  assert.ok(check !== null, "the fixture is a degradation check payload");
  return check;
}

test("the real fixture parses: a degraded check named by its check_hash", () => {
  const check = payloadOf(fixture.payload);
  assert.equal(check.check_hash, fixture.id);
  assert.equal(check.subject, "strategy:trend_a@1.0.0");
  assert.equal(check.degraded, true);
  assert.deepEqual(check.missing, ["hit_rate"]);
  assert.match(degradationLabel(check), /^strategy:trend_a@1\.0\.0 — DEGRADED \(.+\)$/);
});

test("rows: breached, then missing, then within — each with its status", () => {
  const check = payloadOf(fixture.payload);
  const rows = metricRows(check);
  assert.deepEqual(
    rows.map((row) => [row.metric, metricStatus(row)]),
    [
      ["sharpe", "breached"],
      ["hit_rate", "missing"],
      ["max_drawdown", "within"],
    ],
  );
  const missing = rows[1];
  assert.equal(missing.recent, null);
  assert.equal(missing.decline, null);
  assert.match(statusText("missing"), /证据不足/);
  assert.match(statusText("missing"), /不是健康/);
  for (const row of rows) assert.ok(row.threshold_source.length > 0);
});

test("the summary names the breaches and always the missing metrics", () => {
  const check = payloadOf(fixture.payload);
  assert.equal(
    checkSummary(check),
    "退化：1 个指标超出允许下降（sharpe）；证据不足（无近期值）：hit_rate",
  );
  const healthy = payloadOf(clone(fixture.payload));
  healthy.degraded = false;
  healthy.breaches = [];
  assert.equal(checkSummary(healthy), "未发现超出允许下降的指标；证据不足（无近期值）：hit_rate");
  healthy.missing = [];
  assert.equal(checkSummary(healthy), "未发现超出允许下降的指标");
});

test("directions are described; an unknown one is shown verbatim", () => {
  assert.match(directionText("higher_is_better"), /baseline − recent/);
  assert.match(directionText("lower_is_better"), /recent − baseline/);
  assert.equal(directionText("sideways"), "sideways");
});

test("a payload that is not a degradation check is not parsed as one", () => {
  assert.equal(asDegradationCheckPayload(undefined), null);
  assert.equal(asDegradationCheckPayload({ kind: "degradation_check" }), null);
  const noFlag = clone(fixture.payload);
  noFlag.degraded = "yes";
  assert.equal(asDegradationCheckPayload(noFlag), null);
  const [deviation] = fixtureEnvelopes("paper_deviation");
  assert.equal(asDegradationCheckPayload(deviation.payload), null);
});
