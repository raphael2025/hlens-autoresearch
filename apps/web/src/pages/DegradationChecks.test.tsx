import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { ReportEnvelope } from "../api";
import { api, count, escaped, renderSettled } from "../components/render.test-util.tsx";
import { authorityBlock, callerDeclaredEvidence, withEvidence } from "../lib/degradationAuthority.test-util.ts";
import { clone, fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import { DegradationChecks } from "./DegradationChecks.tsx";

// The Degradation Checks page's insufficient-evidence state, over the real apps/web/fixtures/
// degradation_check/ reports: the variant with every metric missing (`"insufficient_evidence":
// true`, research/reports/degradation.py) is shown as its own state and never as healthy; the
// degraded fixture (partial evidence: one metric missing) is unchanged.

const reports = fixtureEnvelopes("degradation_check");

function one(insufficientEvidence: boolean): ReportEnvelope {
  const found = reports.filter((r) => (r.payload.insufficient_evidence === true) === insufficientEvidence);
  assert.equal(found.length, 1);
  return found[0];
}

const insufficient = one(true);
const degraded = one(false);

const BOX = "INSUFFICIENT EVIDENCE（证据不足）";
const HEALTHY = "未发现超出允许下降的指标";
const WITHIN = "在允许下降以内";
const MISSING = "无近期值 — 证据不足（missing），不是健康";

async function detailOf(report: ReportEnvelope): Promise<string> {
  const { html, requests } = await renderSettled(<DegradationChecks />, {
    routes: [
      api.listing("degradation_check", reports.includes(report) ? reports : [...reports, report]),
      api.report(report),
    ],
    selected: report.id,
  });
  assert.ok(requests.includes(`GET /api/reports/degradation_check/${report.id}`));
  assert.ok(!html.includes("<pre>{"), "the detail is parsed, not the raw-payload fallback");
  assert.ok(!html.includes('role="alert"'));
  // role="status" is the Loading state (States.tsx; the live smoke checks it): never reused here
  assert.ok(!html.includes('role="status"'), "nothing still loading");
  return html;
}

describe("DegradationChecks: insufficient evidence", () => {
  test("list: the variant is labelled INSUFFICIENT EVIDENCE, never ok", async () => {
    const { html } = await renderSettled(<DegradationChecks />, {
      routes: [api.listing("degradation_check", reports)],
    });
    assert.equal(count(html, "<button"), 2);
    assert.equal(count(html, "INSUFFICIENT EVIDENCE ("), 1);
    assert.equal(count(html, " — DEGRADED ("), 1);
    assert.ok(!html.includes(" — ok ("), "no check is labelled ok");
  });

  test("detail: a distinct insufficient-evidence state, every metric missing, nothing healthy", async () => {
    const html = await detailOf(insufficient);
    assert.ok(html.includes('data-check-status="insufficient_evidence"'));
    assert.ok(html.includes(escaped(BOX)));
    assert.ok(
      html.includes(
        escaped("证据不足：全部 3 个指标都没有近期值（hit_rate, max_drawdown, sharpe） — 无法判断是否退化，不是健康"),
      ),
    );
    assert.ok(html.includes(escaped("这次检查既不能说明退化，也不能说明健康")));
    assert.ok(!html.includes(escaped(HEALTHY)), "never the healthy verdict line");
    assert.ok(!html.includes(escaped(WITHIN)), "no metric reads as within its allowed decline");
    assert.equal(count(html, escaped(MISSING)), 3);
  });

  test("detail: an older payload without the flag but every metric missing is still insufficient", async () => {
    const payload = clone(insufficient.payload);
    delete payload.insufficient_evidence;
    const html = await detailOf({ ...insufficient, id: "older-all-missing", payload });
    assert.ok(html.includes(escaped(BOX)));
    assert.ok(!html.includes(escaped(HEALTHY)));
  });

  test("detail: partial evidence (the degraded fixture) keeps its summary, missing listed as 证据不足", async () => {
    const html = await detailOf(degraded);
    assert.ok(!html.includes(escaped(BOX)) && !html.includes('data-check-status="insufficient_evidence"'));
    assert.ok(html.includes(escaped("退化：1 个指标超出允许下降（sharpe）；证据不足（无近期值）：hit_rate")));
    assert.equal(count(html, escaped(MISSING)), 1);
    assert.equal(count(html, escaped(WITHIN)), 1);
  });
});

// Evidence strength (ADR-0067 caller-declared vs ADR-0098 authority-resolved). The committed
// fixtures are 1.0.0; the 1.1.0 payloads are built inline in the writer's shapes
// (src/lib/degradationAuthority.test-util.ts) on top of the degraded fixture.

const CALLER = "证据强度：CALLER-DECLARED（调用方声明）";
const AUTHORITY = "证据强度：AUTHORITY-RESOLVED（权威解析）";
const UNREADABLE = "证据强度：AUTHORITY UNREADABLE（无法读取的权威记录 — 不作权威证据）";

function variant(id: string, evidence: Record<string, unknown>): ReportEnvelope {
  return { ...degraded, id, payload: withEvidence(degraded.payload, evidence) };
}

describe("DegradationChecks: evidence strength", () => {
  test("legacy 1.0.0 fixture: caller-declared badge, no provenance panel", async () => {
    const html = await detailOf(degraded);
    assert.ok(html.includes('data-evidence-strength="caller_declared"'));
    assert.ok(html.includes(escaped(CALLER)));
    assert.ok(html.includes(escaped("schema 1.0.0：没有哈希绑定的证据记录")));
    assert.ok(!html.includes('aria-label="Hash-bound provenance"'));
    assert.ok(!html.includes('aria-label="Authority provenance"'));
  });

  test("1.1.0 without authority: caller-declared, the caller-declared provenance note, no authority panel", async () => {
    const html = await detailOf(variant("caller-declared-110", callerDeclaredEvidence()));
    assert.ok(html.includes('data-evidence-strength="caller_declared"'));
    assert.ok(html.includes(escaped(CALLER)));
    assert.ok(html.includes('aria-label="Hash-bound provenance"'));
    assert.ok(html.includes(escaped("这些内容由调用方声明")));
    assert.ok(!html.includes('aria-label="Authority provenance"'));
    assert.ok(!html.includes(escaped(AUTHORITY)));
  });

  test("1.1.0 with authority: authority badge and compact provenance panel", async () => {
    const html = await detailOf(
      variant("authority-110", { ...callerDeclaredEvidence(), authority: authorityBlock() }),
    );
    assert.ok(html.includes('data-evidence-strength="authority_resolved"'));
    assert.ok(html.includes(escaped(AUTHORITY)));
    assert.ok(html.includes(escaped("anchor absent：未用外部 anchor 核验，无法检测整段尾部记录的回滚")));
    assert.ok(html.includes('aria-label="Authority provenance"'));
    assert.ok(html.includes('data-authority-format="hlens.p11.authority-provenance@1.2.0"'));
    assert.ok(!html.includes("data-authority-problems"), "a bound block lists no problems");
    for (const text of [
      "3 records · 5555555555555555…",
      "absent（未核验：无法检测尾部回滚）",
      "ds-btcusdt-1h",
      "8888888888888888…",
      "bars / snap-42",
      "run-1",
      "hlens.p11.monitoring-metrics@2.0.0",
      "2026-02-02T00:00:00Z",
      "[2026-01-01T00:00:00Z, 2026-02-01T00:00:00Z)",
      "p11.window_validation.sharpe@1.0.0",
      "G1.sharpe",
      "observation_window",
    ]) {
      assert.ok(html.includes(escaped(text)), text);
    }
    assert.ok(!html.includes(escaped("这些内容由调用方声明")), "not described as caller-declared");
    assert.ok(!html.includes("<form") && !html.includes("<input"), "read-only: no form controls");
  });

  test("an authority block that does not bind to its evidence: unreadable badge, problems listed", async () => {
    const html = await detailOf(
      variant("authority-unbound", {
        ...callerDeclaredEvidence(),
        authority: authorityBlock({ window_start: "2025-12-01T00:00:00Z" }),
      }),
    );
    assert.ok(html.includes('data-evidence-strength="authority_unreadable"'));
    assert.ok(html.includes(escaped(UNREADABLE)));
    assert.ok(!html.includes(escaped(AUTHORITY)));
    assert.ok(html.includes('data-authority-problems="1"'));
    assert.ok(html.includes(escaped("authority 与 evidence 的 window start 不一致")));
    assert.ok(html.includes(escaped("这些内容由调用方声明")), "falls back to the caller-declared note");
  });
});
