import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { ReportEnvelope } from "../api";
import { api, escaped, renderSettled } from "../components/render.test-util.tsx";
import { fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { RouterStops } from "./RouterStops.tsx";

// The evidence-mode refusal wording on the Router Stops page (a refused check always ends in a
// stop; a paper run carries only verified checks). The committed router_stop fixture is trust-mode
// (no `eligibility` key), so the evidence-mode payload is built inline in the shape
// research/reports/router.py's `_stop_payload` writes (EligibilityCheck.to_dict() records), as in
// src/lib/routerEligibility.test.ts: the two refusals that come after G5 (`profile_not_found`,
// `market_benchmark_missing`) are shown in words, with G5 as passed.

const A = "strategy:alpha@1.0.0";
const B = "strategy:beta@1.0.0";
const H = (c: string) => c.repeat(64);

function check(overrides: Record<string, unknown>): Record<string, unknown> {
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
  };
}

const NO_PROFILE = check({
  refusal: "profile_not_found",
  detail: `no given Profile is profile:default@1.0.0 / ${H("c")}`,
});
const NO_BENCHMARK = check({
  strategy: B,
  lifecycle: "production_candidate",
  report_hash: H("b"),
  subject: B,
  refusal: "market_benchmark_missing",
  detail:
    "benchmark.market_benchmark_rule=buy_and_hold_equal_weight calls for " +
    "G2.market_benchmark.buy_and_hold_equal_weight, which the report lacks (ADR-0060)",
});

const PROFILE_TEXT =
  "拒绝：未提供验证报告所用的 Validation Profile（内容哈希与 ref 须与报告一致）（profile_not_found）";
const BENCHMARK_TEXT =
  "拒绝：Profile 的市场基准规则要求的 G2.market_benchmark 项在验证报告中缺失（ADR-0060）（market_benchmark_missing）";

function envelope(id: string, payload: Record<string, unknown>): ReportEnvelope {
  return { kind: "router_stop", id, created: "2026-09-26T00:00:00Z", content_hash: H("0"), payload };
}

async function detailOf(Page: () => JSX.Element, report: ReportEnvelope): Promise<string> {
  const { html } = await renderSettled(<Page />, {
    routes: [api.listing(report.kind, [report]), api.report(report)],
    selected: report.id,
  });
  assert.ok(!html.includes("<pre>{"), "the detail is parsed, not the raw-payload fallback");
  assert.ok(!html.includes('role="alert"'));
  return html;
}

function assertRefusalRows(html: string): void {
  assert.ok(html.includes(escaped("资格证据（证据模式）")));
  assert.ok(html.includes(escaped("2 个策略中 0 个已核验，2 个被拒绝")));
  assert.ok(html.includes(escaped(PROFILE_TEXT)), "profile_not_found in words");
  assert.ok(html.includes(escaped(BENCHMARK_TEXT)), "market_benchmark_missing in words");
  // both refusals come after G5: G5 is shown as passed, never "not checked"
  assert.ok(!html.includes(escaped("未核验（更早的检查已拒绝）")));
  assert.ok(html.includes(`<td>${escaped("通过 · G5.fixture")}</td>`));
  // the raw code is never shown alone (without its wording)
  assert.ok(!html.includes(">拒绝：profile_not_found<") && !html.includes(">拒绝：market_benchmark_missing<"));
}

// research/router/router.py's RouterEligibilityRefused detail: "<ref>: <refusal> (<detail>)" per
// refused check, "; "-joined in ref order.
const STOP_DETAIL = [NO_PROFILE, NO_BENCHMARK]
  .map((c) => `${String(c.strategy)}: ${String(c.refusal)} (${String(c.detail)})`)
  .join("; ");

describe("RouterStops evidence mode: Profile / market benchmark refusals", () => {
  test("the refusals in words with their raw codes, G5 shown as passed", async () => {
    const [trust] = fixtureEnvelopes("router_stop");
    const payload = {
      ...trust.payload,
      reason: "eligibility_not_evidenced",
      detail: STOP_DETAIL,
      lifecycle: { [A]: "active", [B]: "production_candidate" },
      strategy_result_hashes: { [A]: H("3"), [B]: H("4") },
      eligibility: [NO_BENCHMARK, NO_PROFILE], // order is not trusted: sorted by strategy
    };
    const html = await detailOf(RouterStops, envelope(H("5"), payload));
    assert.ok(html.includes(escaped("（eligibility_not_evidenced）")));
    assert.ok(html.includes(escaped(STOP_DETAIL)));
    assertRefusalRows(html);
    assert.ok(html.indexOf(escaped(PROFILE_TEXT)) < html.indexOf(escaped(BENCHMARK_TEXT)), "alpha before beta");
  });

  test("an unknown refusal code is shown verbatim with G5 unknown, never passed or not checked", async () => {
    const [trust] = fixtureEnvelopes("router_stop");
    const payload = {
      ...trust.payload,
      reason: "eligibility_not_evidenced",
      detail: `${A}: something_new (?)`,
      lifecycle: { [A]: "active" },
      strategy_result_hashes: { [A]: H("3") },
      eligibility: [check({ refusal: "something_new", detail: "?" })],
    };
    const html = await detailOf(RouterStops, envelope(H("6"), payload));
    assert.ok(html.includes(`<td>${escaped("未知（无法识别的拒绝代码） · G5.fixture")}</td>`));
    assert.ok(html.includes(escaped("拒绝：something_new")));
  });
});
