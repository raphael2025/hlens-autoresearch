import assert from "node:assert/strict";
import { test } from "node:test";
import {
  asDegradationCheckPayload,
  checkStatus,
  checkSummary,
  degradationLabel,
  directionText,
  metricRows,
  metricStatus,
  statusText,
  authorityOf,
  evidenceStrength,
  evidenceStrengthLabel,
  evidenceStrengthText,
} from "./degradationCheck.ts";
import { authorityBlock, callerDeclaredEvidence, withEvidence } from "./degradationAuthority.test-util.ts";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";

// Two real fixtures: the degraded check (one breached, one within, one missing) and the
// insufficient-evidence variant (every metric missing; `"insufficient_evidence": true`).
const fixtures = fixtureEnvelopes("degradation_check");
const fixture = byState(false);
const insufficient = byState(true);

function byState(insufficientEvidence: boolean) {
  const found = fixtures.filter((envelope) => (envelope.payload.insufficient_evidence === true) === insufficientEvidence);
  assert.equal(found.length, 1, `one ${insufficientEvidence ? "insufficient-evidence" : "degraded"} fixture`);
  return found[0];
}

function payloadOf(payload: Record<string, unknown>) {
  const check = asDegradationCheckPayload(payload);
  assert.ok(check !== null, "the fixture is a degradation check payload");
  return check;
}

test("the real fixture parses: a degraded check named by its check_hash", () => {
  assert.equal(fixtures.length, 2);
  const check = payloadOf(fixture.payload);
  assert.equal(check.check_hash, fixture.id);
  assert.equal(check.subject, "strategy:trend_a@1.0.0");
  assert.equal(check.degraded, true);
  assert.equal(checkStatus(check), "degraded");
  assert.deepEqual(check.missing, ["hit_rate"]);
  assert.match(degradationLabel(check), /^strategy:trend_a@1\.0\.0 — DEGRADED \(.+\)$/);
});

test("the insufficient-evidence fixture: its own state, never healthy", () => {
  const check = payloadOf(insufficient.payload);
  assert.equal(check.check_hash, insufficient.id);
  assert.equal(check.insufficient_evidence, true);
  assert.equal(check.degraded, false);
  assert.equal(checkStatus(check), "insufficient_evidence");
  assert.match(degradationLabel(check), /^strategy:trend_a@1\.0\.0 — INSUFFICIENT EVIDENCE \(.+\)$/);
  assert.ok(!degradationLabel(check).includes(" — ok ("), "never labelled ok");
  const summary = checkSummary(check);
  assert.equal(
    summary,
    "证据不足：全部 3 个指标都没有近期值（hit_rate, max_drawdown, sharpe） — 无法判断是否退化，不是健康",
  );
  assert.ok(!summary.includes("未发现超出允许下降的指标"), "never the healthy verdict line");
  // every row is missing (evidence insufficient), none within
  assert.deepEqual(
    metricRows(check).map((row) => [row.metric, metricStatus(row)]),
    [
      ["hit_rate", "missing"],
      ["max_drawdown", "missing"],
      ["sharpe", "missing"],
    ],
  );
});

test("every metric missing without the flag (an older payload) is still insufficient evidence", () => {
  const older = clone(insufficient.payload);
  delete older.insufficient_evidence;
  const check = payloadOf(older);
  assert.equal(checkStatus(check), "insufficient_evidence");
  assert.match(checkSummary(check), /^证据不足：全部 3 个指标都没有近期值/);
  assert.match(degradationLabel(check), / — INSUFFICIENT EVIDENCE \(/);
});

test("partial evidence stays as it was: not degraded, the missing metrics listed as 证据不足", () => {
  const partial = payloadOf(clone(fixture.payload));
  partial.degraded = false;
  partial.breaches = [];
  partial.metrics = partial.metrics.map((metric) => ({ ...metric, breached: false }));
  assert.equal(checkStatus(partial), "not_degraded");
  assert.equal(checkSummary(partial), "未发现超出允许下降的指标；证据不足（无近期值）：hit_rate");
  assert.match(degradationLabel(partial), / — ok \(/);
});

test("an insufficient_evidence key that is not true, or that comes with degraded, is not read", () => {
  const notTrue = clone(insufficient.payload);
  notTrue.insufficient_evidence = false;
  assert.equal(asDegradationCheckPayload(notTrue), null);
  const contradictory = clone(fixture.payload);
  contradictory.insufficient_evidence = true;
  assert.equal(asDegradationCheckPayload(contradictory), null);
});

test("empty metrics is not a check: fail closed, never vacuously insufficient evidence", () => {
  const noFlag = clone(insufficient.payload);
  delete noFlag.insufficient_evidence;
  noFlag.metrics = [];
  noFlag.missing = [];
  assert.equal(asDegradationCheckPayload(noFlag), null);
  const flagged = clone(insufficient.payload);
  flagged.metrics = [];
  flagged.missing = [];
  assert.equal(asDegradationCheckPayload(flagged), null);
  const notArray = clone(insufficient.payload);
  notArray.metrics = null;
  assert.equal(asDegradationCheckPayload(notArray), null);
});

test("insufficient_evidence true with a metric that is not missing is not read", () => {
  const mixed = clone(insufficient.payload);
  const metrics = mixed.metrics as Record<string, unknown>[];
  metrics[0] = { ...metrics[0], missing: false };
  assert.equal(asDegradationCheckPayload(mixed), null);
  // the degraded fixture (one breached, one within, one missing) with the flag forced on
  const partial = clone(fixture.payload);
  partial.degraded = false;
  partial.breaches = [];
  partial.insufficient_evidence = true;
  assert.equal(asDegradationCheckPayload(partial), null);
});

test("insufficient_evidence true with a missing list that contradicts the metrics is not read", () => {
  const fewer = clone(insufficient.payload);
  fewer.missing = (fewer.missing as string[]).slice(1);
  assert.equal(asDegradationCheckPayload(fewer), null);
  const renamed = clone(insufficient.payload);
  renamed.missing = ["hit_rate", "max_drawdown", "not_a_metric"];
  assert.equal(asDegradationCheckPayload(renamed), null);
  const extra = clone(insufficient.payload);
  extra.missing = [...(extra.missing as string[]), "extra"];
  assert.equal(asDegradationCheckPayload(extra), null);
  const notStrings = clone(insufficient.payload);
  notStrings.missing = [1, 2, 3];
  assert.equal(asDegradationCheckPayload(notStrings), null);
  // the order of the names does not matter
  const reordered = clone(insufficient.payload);
  reordered.missing = [...(reordered.missing as string[])].reverse();
  assert.notEqual(asDegradationCheckPayload(reordered), null);
});

test("an older all-missing payload without the flag is still derived as insufficient evidence", () => {
  const older = clone(insufficient.payload);
  delete older.insufficient_evidence;
  const check = payloadOf(older);
  assert.equal("insufficient_evidence" in check, false);
  assert.equal(check.metrics.length, 3);
  assert.ok(check.metrics.every((metric) => metric.missing));
  assert.equal(checkStatus(check), "insufficient_evidence");
  assert.match(degradationLabel(check), / — INSUFFICIENT EVIDENCE \(/);
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

// ---- evidence strength: caller-declared (ADR-0067 / legacy 1.0.0) vs authority (ADR-0098) -----

function withAuthority(authority: unknown, evidence = callerDeclaredEvidence()) {
  return payloadOf(withEvidence(fixture.payload, { ...evidence, authority }));
}

test("legacy 1.0.0 fixtures: caller-declared, no authority, no hash-bound evidence", () => {
  for (const envelope of fixtures) {
    const check = payloadOf(envelope.payload);
    assert.equal(check.schema_version, "1.0.0");
    assert.equal(authorityOf(check), null);
    assert.equal(evidenceStrength(check), "caller_declared");
    assert.match(evidenceStrengthText(check), /^schema 1\.0\.0：没有哈希绑定的证据记录/);
  }
  assert.equal(evidenceStrengthLabel("caller_declared"), "CALLER-DECLARED（调用方声明）");
});

test("1.1.0 without authority: caller-declared (ADR-0067), still parsed", () => {
  const check = payloadOf(withEvidence(fixture.payload, callerDeclaredEvidence()));
  assert.equal(authorityOf(check), null);
  assert.equal(evidenceStrength(check), "caller_declared");
  assert.match(evidenceStrengthText(check), /由调用方声明（ADR-0067）/);
});

test("1.1.0 with a bound, known-format authority: authority-resolved, every field read", () => {
  const check = withAuthority(authorityBlock());
  assert.equal(evidenceStrength(check), "authority_resolved");
  const view = authorityOf(check);
  assert.ok(view !== null);
  assert.deepEqual(view.problems, []);
  assert.equal(view.format, "hlens.p11.authority-provenance@1.2.0");
  assert.equal(view.formatKnown, true);
  assert.equal(view.headRecordCount, 3);
  assert.equal(view.headLastRecordHash, "5".repeat(64));
  assert.equal(view.anchor, "absent");
  assert.equal(view.recordHashCount, 2);
  assert.equal(view.datasetId, "ds-btcusdt-1h");
  assert.equal(view.manifestHash, "8".repeat(64));
  assert.equal(view.baselineSetHash, "b".repeat(64));
  assert.equal(view.asOf, "2026-02-02T00:00:00Z");
  assert.deepEqual(view.definitions, [
    {
      metric: "sharpe",
      definition: "p11.window_validation.sharpe@1.0.0",
      gateId: "G1.sharpe",
      windowScope: "observation_window",
      evidence: "window_validation",
    },
  ]);
  const text = evidenceStrengthText(check);
  assert.match(text, /anchor absent：未用外部 anchor 核验，无法检测整段尾部记录的回滚/);
  assert.match(text, /仍然只是证据，不改变生命周期状态/);
});

test("an anchored head says so", () => {
  const block = authorityBlock();
  block.lifecycle = { ...(block.lifecycle as Record<string, unknown>), anchor: "present" };
  const check = withAuthority(block);
  assert.equal(evidenceStrength(check), "authority_resolved");
  assert.match(evidenceStrengthText(check), /已用外部 anchor 核验/);
});

test("an unknown provenance format is unreadable, never authority-resolved", () => {
  const check = withAuthority(authorityBlock({ format: "hlens.p11.authority-provenance@9.0.0" }));
  assert.equal(evidenceStrength(check), "authority_unreadable");
  assert.ok(authorityOf(check)?.problems.some((p) => p.includes("未识别的 provenance 格式")));
  assert.match(evidenceStrengthText(check), /按调用方声明对待/);
});

test("an authority block that disagrees with its evidence is unreadable, each binding named", () => {
  const cases: [Record<string, unknown>, string][] = [
    [{ window_end: "2026-03-01T00:00:00Z" }, "window end"],
    [{ window_start: "2025-12-01T00:00:00Z" }, "window start"],
    [{ recent_manifest_hash: "e".repeat(64) }, "recent manifest"],
  ];
  for (const [overrides, what] of cases) {
    const check = withAuthority(authorityBlock(overrides));
    assert.equal(evidenceStrength(check), "authority_unreadable", what);
    assert.ok(authorityOf(check)?.problems.includes(`authority 与 evidence 的 ${what} 不一致`), what);
  }
  const otherHistory = withAuthority(authorityBlock(), {
    ...callerDeclaredEvidence(),
    lifecycle_history_hash: "f".repeat(64),
  });
  assert.ok(authorityOf(otherHistory)?.problems.includes("authority 与 evidence 的 lifecycle history 不一致"));
});

test("as_of before the window end, a bad anchor or a non-object block is unreadable", () => {
  // the evaluation time must not precede the window end (ADR-0098 修订 2 §1)
  const early = withAuthority(authorityBlock({ as_of: "2026-01-15T00:00:00Z" }));
  assert.equal(evidenceStrength(early), "authority_unreadable");
  const block = authorityBlock();
  block.lifecycle = { ...(block.lifecycle as Record<string, unknown>), anchor: "maybe" };
  assert.equal(evidenceStrength(withAuthority(block)), "authority_unreadable");
  const notObject = withAuthority("authority");
  assert.equal(evidenceStrength(notObject), "authority_unreadable");
  assert.ok(authorityOf(notObject)?.problems.includes("authority 不是对象"));
});
