import assert from "node:assert/strict";
import { describe, test } from "node:test";
import type { ReportEnvelope } from "../api";
import { api, count, escaped, renderSettled } from "../components/render.test-util.tsx";
import { clone, fixtureEnvelopes } from "../lib/fixtures.test-util.ts";
import type { GateCalibrationPayload } from "../lib/gateCalibration.ts";
import { NOISE_G5, PLANTED_G5_WITH_ERRORS } from "../lib/gateCalibrationG5.test-util.ts";
import { GateCalibration } from "./GateCalibration.tsx";

// The Gate Calibration page shows the uncertainty the report carries when a detector raised
// (B45 / B48 / B54): the arm's pipeline `pass_rate_bounds`, and in G5 mode the `sealed_oos_g5`
// block with its G5 `pass_rate_bounds` and `end_to_end_bounds`. Inputs: the real apps/web/fixtures/
// gate_calibration/ report, edited with G5 blocks of the writer's exact shape
// (src/lib/gateCalibrationG5.test-util.ts).

const reports = fixtureEnvelopes("gate_calibration");
const [base] = reports;

const G5_HEADING = "G5（密封样本外）与端到端 G0 – G5";

function edited(edit: (payload: GateCalibrationPayload) => void, id: string): ReportEnvelope {
  const payload = clone(base.payload) as unknown as GateCalibrationPayload;
  edit(payload);
  return { ...base, id, payload: payload as unknown as Record<string, unknown> };
}

function plantedArm(payload: GateCalibrationPayload): string {
  const arm = Object.keys(payload.candidates[0].pipeline).find((name) => name !== "noise");
  assert.ok(arm !== undefined);
  return arm;
}

async function detailOf(report: ReportEnvelope): Promise<string> {
  const { html, requests } = await renderSettled(<GateCalibration />, {
    routes: [api.listing("gate_calibration", [...reports, report]), api.report(report)],
    selected: report.id,
  });
  assert.ok(requests.includes(`GET /api/reports/gate_calibration/${report.id}`));
  assert.ok(!html.includes("<pre>{"), "the detail is parsed, not the raw-payload fallback");
  assert.ok(!html.includes('role="alert"'));
  assert.ok(!html.includes('role="status"'), "nothing still loading");
  return html;
}

describe("GateCalibration: uncertainty from detector errors", () => {
  test("a report without errors or G5 shows neither a bounds column nor a G5 table", async () => {
    const html = await detailOf(edited(() => undefined, "plain"));
    assert.ok(!html.includes("pass_rate_bounds"));
    assert.ok(!html.includes("data-bounds="));
    assert.ok(!html.includes(escaped(G5_HEADING)));
  });

  test("an arm with detector errors shows its pipeline pass_rate_bounds next to the point rate", async () => {
    const report = edited((payload) => {
      const noise = payload.candidates[0].pipeline.noise;
      noise.detector_errors = 2;
      noise.pass_rate_bounds = ["0.000000", "0.400000"];
    }, "arm-bounds");
    const html = await detailOf(report);
    assert.ok(html.includes("<th>pass_rate_bounds</th>"));
    assert.equal(count(html, 'data-bounds="bounds"'), 1);
    assert.ok(html.includes(">[0.000000, 0.400000]<"));
    assert.ok(html.includes(escaped("这些 arm 的 rate 只是点估计")));
  });

  test("malformed bounds are shown as malformed, never dropped", async () => {
    const report = edited((payload) => {
      const noise = payload.candidates[0].pipeline.noise;
      noise.detector_errors = 1;
      noise.pass_rate_bounds = ["0.9", "0.1"];
    }, "arm-bad-bounds");
    const html = await detailOf(report);
    assert.equal(count(html, 'data-bounds="malformed"'), 1);
    assert.ok(html.includes(">malformed bounds<"));
  });

  test("G5 mode: the G5 table shows reached, rates, end-to-end, and bounds only for the erroring arm", async () => {
    const report = edited((payload) => {
      const candidate = payload.candidates[0];
      candidate.pipeline.noise.sealed_oos_g5 = clone(NOISE_G5);
      candidate.pipeline[plantedArm(payload)].sealed_oos_g5 = clone(PLANTED_G5_WITH_ERRORS);
    }, "g5");
    const html = await detailOf(report);
    assert.ok(html.includes(escaped(G5_HEADING)));
    assert.equal(count(html, 'data-section="sealed-oos-g5"'), 1);
    // noise: 3 of 4 reached runs passed G5; no errors, so both bounds cells are "—"
    assert.ok(html.includes(">0.750000 (3/4)<"));
    assert.ok(html.includes(">false_positive_rate: "));
    assert.equal(count(html, 'data-bounds="absent"'), 2);
    // planted: every G5 raised -> 0/4 passed, bounds [0, 1] for G5 and end to end
    assert.ok(html.includes(">power: 0.000000 (0/4)<"));
    assert.equal(count(html, 'data-bounds="bounds"'), 2);
    assert.equal(count(html, ">[0.000000, 1.000000]<"), 2);
    assert.ok(html.includes(escaped("有检测器错误时，点估计不是真实比率")));
  });
});
