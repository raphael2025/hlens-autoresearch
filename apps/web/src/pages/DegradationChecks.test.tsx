import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { ReportEnvelope } from "../api";
import { api, count, escaped, renderSettled } from "../components/render.test-util.tsx";
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
