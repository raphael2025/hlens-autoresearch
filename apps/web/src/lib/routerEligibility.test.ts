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
    // checked after G5 (research/router/evidence.py): reaching them means every G5 gate passed
    profile_not_found: "passed",
    market_benchmark_missing: "passed",
    inverse_control_missing: "passed",
  } as const;
  assert.deepEqual([...REFUSAL_ORDER], Object.keys(expectedG5));
  for (const code of REFUSAL_ORDER) {
    const text = refusalText(code);
    assert.ok(text.endsWith(`（${code}）`) && text.length > code.length + 2, code);
    assert.equal(sealedOosStatus(check({ refusal: code })), expectedG5[code]);
  }
  assert.equal(refusalText("something_new"), "something_new");
  assert.equal(checkResultText(check({ refusal: "something_new" })), "拒绝：something_new");
  // an unknown code says nothing about G5: neither passed nor "not checked"
  assert.equal(sealedOosStatus(check({ refusal: "something_new" })), "unknown");
  assert.equal(sealedOosText(check({ refusal: "something_new" })), "未知（无法识别的拒绝代码） · G5.fixture");
});

test("Profile / market benchmark refusals come after G5: G5 passed, the claim still refused", () => {
  const view = eligibilityOf({
    eligibility: [
      check({
        refusal: "profile_not_found",
        detail: `no given Profile is profile:default@1.0.0 / ${H("c")}`,
      }),
      check({
        strategy: B,
        subject: B,
        sealed_oos_gates: ["G5.a", "G5.b"],
        refusal: "market_benchmark_missing",
        detail:
          "benchmark.market_benchmark_rule=buy_and_hold_equal_weight calls for " +
          "G2.market_benchmark.buy_and_hold_equal_weight, " +
          "which the report lacks (ADR-0060)",
      }),
    ],
  });
  assert.ok(view !== null);
  assert.equal(eligibilitySummary(view), "2 个策略中 0 个已核验，2 个被拒绝");
  const [noProfile, noBenchmark] = view.checks;
  assert.equal(
    checkResultText(noProfile),
    "拒绝：未提供验证报告所用的 Validation Profile（内容哈希与 ref 须与报告一致）（profile_not_found）",
  );
  assert.equal(sealedOosText(noProfile), "通过 · G5.fixture");
  assert.equal(
    checkResultText(noBenchmark),
    "拒绝：Profile 的市场基准规则要求的 G2.market_benchmark 项在验证报告中缺失（ADR-0060）（market_benchmark_missing）",
  );
  assert.equal(sealedOosText(noBenchmark), "通过 · G5.a, G5.b");
});

test("an inverse control refusal comes after G5 and the market benchmark: G5 passed, still refused", () => {
  const view = eligibilityOf({
    eligibility: [
      check({
        refusal: "inverse_control_missing",
        detail: "benchmark.inverse_control_reported=true calls for G2.inverse_control, which the report lacks (ADR-0060)",
      }),
    ],
  });
  assert.ok(view !== null);
  assert.equal(eligibilitySummary(view), "1 个策略中 0 个已核验，1 个被拒绝");
  const [noInverse] = view.checks;
  assert.equal(
    checkResultText(noInverse),
    "拒绝：Profile 要求报告反向对照（inverse_control_reported），验证报告缺少 G2.inverse_control 项（ADR-0060）（inverse_control_missing）",
  );
  assert.equal(sealedOosText(noInverse), "通过 · G5.fixture");
  // it is the last check: after the Profile and the market benchmark item
  assert.deepEqual(REFUSAL_ORDER.slice(-3), ["profile_not_found", "market_benchmark_missing", "inverse_control_missing"]);
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
