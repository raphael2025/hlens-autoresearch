import assert from "node:assert/strict";
import { test } from "node:test";
import { fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  type EligibilityCheck,
  REFUSAL_ORDER,
  checkResultText,
  eligibilityOf,
  eligibilitySummary,
  refusalText,
  sealedOosStatus,
  sealedOosText,
  validationReportsOf,
} from "./routerEligibility.ts";
import { asRouterStopPayload, reasonText } from "./routerStop.ts";

// Inline payloads in the shapes research/reports/router.py writes: `_stop_payload` (router_stop)
// and `_payload` (router_paper_run), each with the evidence-mode additive keys (EligibilityCheck
// .to_dict() records under `eligibility`, and `validation_reports`).

const A = "strategy:alpha@1.0.0";
const B = "strategy:beta@1.0.0";
const H = (c: string) => c.repeat(64);

function check(overrides: Record<string, unknown>): EligibilityCheck {
  return {
    strategy: A,
    lifecycle: "active",
    report_hash: H("a"),
    subject: A,
    verdict: "PASS",
    sealed_oos_gates: ["G5.fixture"],
    refusal: null,
    detail: "PASS including G5 (sealed OOS)",
    ...overrides,
  } as EligibilityCheck;
}

function evidenceStop(): Record<string, unknown> {
  return {
    router: "router:regime@1.0.0",
    router_spec_hash: H("1"),
    reason: "eligibility_not_evidenced",
    detail: "eligibility not evidenced: strategy:beta@1.0.0 (verdict_not_pass)",
    lifecycle: { [A]: "active", [B]: "production_candidate" },
    state_result_hash: H("2"),
    strategy_result_hashes: { [A]: H("3"), [B]: H("4") },
    validation_reports: null,
    stop_hash: H("5"),
    eligibility: [
      check({}),
      check({
        strategy: B,
        lifecycle: "production_candidate",
        report_hash: H("b"),
        subject: B,
        verdict: "FAIL",
        sealed_oos_gates: ["G5.fixture"],
        refusal: "verdict_not_pass",
        detail: "the report's verdict is FAIL",
      }),
    ],
  };
}

function evidenceRun(): Record<string, unknown> {
  return {
    router: "router:regime@1.0.0",
    router_spec_hash: H("1"),
    state_result_hash: H("2"),
    strategy_result_hashes: { [A]: H("3") },
    decisions: [],
    charges: [],
    total_switching_cost: "0",
    request_hash: H("6"),
    gross_result_hash: H("7"),
    result_hash: H("8"),
    initial_equity: "10000",
    final_equity: "10000",
    pnl: "0",
    gross_equity_curve: [],
    net_equity_curve: [],
    run_hash: H("9"),
    validation_reports: { [A]: H("a") },
    eligibility: [check({})],
  };
}

test("trust-mode payloads (the real fixtures) have no eligibility: nothing to show", () => {
  for (const kind of ["router_stop", "router_paper_run"] as const) {
    for (const envelope of fixtureEnvelopes(kind)) {
      assert.equal(eligibilityOf(envelope.payload), null);
    }
  }
  assert.equal(eligibilityOf(undefined), null);
});

test("an evidence-mode stop: reason in words, checks sorted by strategy with their evidence", () => {
  const payload = evidenceStop();
  payload.eligibility = [...(payload.eligibility as unknown[])].reverse(); // order is not trusted
  const stop = asRouterStopPayload(payload);
  assert.ok(stop !== null, "the evidence-mode stop is still a router stop");
  assert.equal(reasonText(stop.reason), "证据模式：至少一个可路由策略的资格未被其验证报告证实（eligibility_not_evidenced）");
  const view = eligibilityOf(payload);
  assert.ok(view !== null);
  assert.equal(view.malformed, 0);
  assert.deepEqual(
    view.checks.map((c) => [c.strategy, c.report_hash, c.verdict, c.refusal]),
    [
      [A, H("a"), "PASS", null],
      [B, H("b"), "FAIL", "verdict_not_pass"],
    ],
  );
  assert.equal(eligibilitySummary(view), "2 个策略中 1 个已核验，1 个被拒绝");
  const [verified, refused] = view.checks;
  assert.equal(checkResultText(verified), "已核验（PASS 且含 G5）");
  assert.equal(sealedOosText(verified), "通过 · G5.fixture");
  assert.equal(checkResultText(refused), "拒绝：验证报告判定不是 PASS（verdict_not_pass）");
  // the verdict check failed first, so G5 was never reached although the report lists a G5 gate
  assert.equal(sealedOosText(refused), "未核验（更早的检查已拒绝） · G5.fixture");
});

test("an evidence-mode run: eligibility and the validation report binding are read", () => {
  const payload = evidenceRun();
  const view = eligibilityOf(payload);
  assert.ok(view !== null);
  assert.equal(eligibilitySummary(view), "1 个策略中 1 个已核验，0 个被拒绝");
  assert.deepEqual(validationReportsOf(payload), [{ strategy: A, report_hash: H("a") }]);
});

test("validation_reports: absent or null is null; entries sorted, non-strings dropped", () => {
  assert.equal(validationReportsOf(undefined), null);
  assert.equal(validationReportsOf(evidenceStop()), null); // written as null
  const run = evidenceRun();
  delete run.validation_reports;
  assert.equal(validationReportsOf(run), null); // trust-mode run without the key
  run.validation_reports = { [B]: H("b"), [A]: H("a"), "strategy:odd@1.0.0": 7 };
  assert.deepEqual(validationReportsOf(run), [
    { strategy: A, report_hash: H("a") },
    { strategy: B, report_hash: H("b") },
  ]);
});

test("every refusal code has words and a G5 status; an unknown code is shown verbatim", () => {
  const expectedG5 = {
    report_hash_missing: "not_checked",
    report_not_found: "not_checked",
    report_invalid: "not_checked",
    report_hash_mismatch: "not_checked",
    subject_mismatch: "not_checked",
    verdict_not_pass: "not_checked",
    sealed_oos_not_evaluated: "not_evaluated",
    sealed_oos_not_passed: "not_passed",
  } as const;
  assert.deepEqual([...REFUSAL_ORDER], Object.keys(expectedG5));
  for (const code of REFUSAL_ORDER) {
    const text = refusalText(code);
    assert.ok(text.endsWith(`（${code}）`) && text.length > code.length + 2, code);
    assert.equal(sealedOosStatus(check({ refusal: code })), expectedG5[code]);
  }
  assert.equal(refusalText("something_new"), "something_new");
  assert.equal(checkResultText(check({ refusal: "something_new" })), "拒绝：something_new");
});

test("refusals before any report was found carry no evidence; G5 text says why", () => {
  const view = eligibilityOf({
    eligibility: [
      check({
        report_hash: null,
        subject: null,
        verdict: null,
        sealed_oos_gates: [],
        refusal: "report_hash_missing",
        detail: "no report hash claimed",
      }),
      check({
        strategy: B,
        sealed_oos_gates: [],
        refusal: "sealed_oos_not_evaluated",
        detail: "the report has no G5 (sealed OOS) gate",
      }),
    ],
  });
  assert.ok(view !== null);
  const [missing, noG5] = view.checks;
  assert.equal(missing.report_hash, null);
  assert.equal(sealedOosText(missing), "未核验（更早的检查已拒绝）");
  assert.equal(sealedOosText(noG5), "未评估（无 G5 门）");
  assert.equal(
    sealedOosText(check({ refusal: "sealed_oos_not_passed", sealed_oos_gates: ["G5.a", "G5.b"] })),
    "未通过 · G5.a, G5.b",
  );
});

test("malformed eligibility is counted, never mistaken for no evidence", () => {
  const notAList = eligibilityOf({ eligibility: { strategy: A } });
  assert.deepEqual(notAList, { checks: [], malformed: 1 });
  const mixed = eligibilityOf({
    eligibility: [check({}), check({ sealed_oos_gates: "G5.fixture" }), "junk", check({ refusal: 3 })],
  });
  assert.ok(mixed !== null);
  assert.equal(mixed.checks.length, 1);
  assert.equal(mixed.malformed, 3);
  assert.equal(eligibilitySummary(mixed), "1 个策略中 1 个已核验，0 个被拒绝；另有 3 条无法解析的检查记录");
  // an explicit `null` is not a list either
  assert.deepEqual(eligibilityOf({ eligibility: null }), { checks: [], malformed: 1 });
});
