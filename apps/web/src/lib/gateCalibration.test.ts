import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asCalibrationPayload,
  boundsCell,
  ciCell,
  detectorErrorRuns,
  detectorErrorsOf,
  hasDetectorErrors,
  hasPassRateBounds,
  passRate,
  readBounds,
  sealedG5Arms,
  sealedG5Of,
} from "./gateCalibration.ts";
import { NOISE_G5, PLANTED_G5_WITH_ERRORS } from "./gateCalibrationG5.test-util.ts";

// The current (contract 2.2.0) report and the legacy readable 2.1.0 and 2.0.0 ones
// (apps/web/fixtures/README.md).
const fixtures = fixtureEnvelopes("gate_calibration");
const [fixture] = fixtures;

test("every committed fixture (2.2.0 and legacy 2.1.0 / 2.0.0) parses with every candidate", () => {
  assert.equal(fixtures.length, 3);
  for (const envelope of fixtures) {
    const payload = asCalibrationPayload(envelope.payload);
    assert.ok(payload !== null, envelope.id);
    assert.equal(payload.report_hash, envelope.id);
    assert.equal(payload.candidates.length, 2);
    assert.equal(passRate(payload.candidates[0].pipeline.noise)?.label, "false_positive_rate");
  }
});

function payloadOf(envelope: typeof fixture) {
  const payload = asCalibrationPayload(envelope.payload);
  assert.ok(payload !== null, "the fixture is a gate calibration payload");
  return payload;
}

test("the real fixture parses; noise reports an FPR and planted arms report power", () => {
  const payload = payloadOf(fixture);
  assert.ok(payload.candidates.length > 0);
  const candidate = payload.candidates[0];
  const noise = passRate(candidate.pipeline.noise);
  assert.equal(noise?.label, "false_positive_rate");
  const planted = Object.keys(candidate.pipeline).find((arm) => arm !== "noise");
  assert.ok(planted !== undefined);
  assert.equal(passRate(candidate.pipeline[planted])?.label, "power");
  assert.match(ciCell(candidate.pipeline.noise.inconclusive_rate), /^\[0\.000000, 0\.369417\] \(α=0\.05, clopper-pearson\)$/);
});

test("without detector errors the keys are absent and nothing is shown", () => {
  for (const candidate of payloadOf(fixture).candidates) {
    assert.equal(hasDetectorErrors(candidate), false);
    assert.deepEqual(detectorErrorRuns(candidate), []);
    for (const evidence of Object.values(candidate.pipeline)) {
      assert.equal("detector_errors" in evidence, false);
      assert.equal(detectorErrorsOf(evidence), null);
    }
  }
});

test("per-arm detector_errors and per-run detector_error are surfaced when present", () => {
  const edited = clone(fixture);
  const candidate = payloadOf(edited).candidates[0];
  candidate.pipeline.noise.detector_errors = 2;
  candidate.runs[0] = { ...candidate.runs[0], verdict: "INCONCLUSIVE", detector_error: "ValueError: singular matrix" };
  assert.equal(hasDetectorErrors(candidate), true);
  assert.equal(detectorErrorsOf(candidate.pipeline.noise), 2);
  const planted = Object.keys(candidate.pipeline).find((arm) => arm !== "noise");
  assert.ok(planted !== undefined);
  assert.equal(detectorErrorsOf(candidate.pipeline[planted]), null);
  const runs = detectorErrorRuns(candidate);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].detector_error, "ValueError: singular matrix");
  assert.equal(runs[0].verdict, "INCONCLUSIVE");
});

test("a payload that is not a calibration report is not parsed as one", () => {
  assert.equal(asCalibrationPayload({ candidates: "nope" }), null);
  assert.equal(asCalibrationPayload(undefined), null);
  assert.equal(passRate({}), null);
});

// --- uncertainty from detector errors (B45 / B48 / B54) -------------------------------------

test("readBounds: absent, a well-formed pair, or malformed (never silently dropped)", () => {
  assert.deepEqual(readBounds(undefined), { kind: "absent" });
  assert.deepEqual(readBounds(["0.428571", "0.714286"]), {
    kind: "bounds",
    lower: "0.428571",
    upper: "0.714286",
  });
  assert.deepEqual(readBounds(["0.000000", "1.000000"]), { kind: "bounds", lower: "0.000000", upper: "1.000000" });
  for (const bad of [
    null,
    "0.1,0.2",
    ["0.1"],
    ["0.1", "0.2", "0.3"],
    [0.1, 0.2],
    ["0.2", "0.1"], // lower above upper
    ["0.5", "1.5"], // above 1
    ["-0.1", "0.2"],
    ["1e-3", "0.2"],
    ["", "0.2"],
  ]) {
    assert.deepEqual(readBounds(bad), { kind: "malformed" }, JSON.stringify(bad));
  }
  assert.equal(boundsCell({ kind: "bounds", lower: "0.1", upper: "0.3" }), "[0.1, 0.3]");
  assert.equal(boundsCell({ kind: "malformed" }), "malformed bounds");
  assert.equal(boundsCell({ kind: "absent" }), null);
});

test("the committed fixtures have no bounds and no G5 block (nothing extra is shown)", () => {
  for (const envelope of fixtures) {
    for (const candidate of payloadOf(envelope).candidates) {
      assert.equal(hasPassRateBounds(candidate), false);
      assert.deepEqual(sealedG5Arms(candidate), []);
      for (const evidence of Object.values(candidate.pipeline)) {
        assert.equal(sealedG5Of(evidence), null);
        assert.equal(readBounds(evidence.pass_rate_bounds).kind, "absent");
      }
    }
  }
});

test("an arm with detector errors exposes its pipeline pass_rate_bounds", () => {
  const edited = clone(fixture);
  const candidate = payloadOf(edited).candidates[0];
  const noise = candidate.pipeline.noise;
  const rate = passRate(noise);
  assert.ok(rate !== null);
  noise.detector_errors = 2;
  noise.pass_rate_bounds = ["0.000000", "0.400000"];
  assert.equal(hasPassRateBounds(candidate), true);
  assert.deepEqual(readBounds(noise.pass_rate_bounds), { kind: "bounds", lower: "0.000000", upper: "0.400000" });
  noise.pass_rate_bounds = ["0.4", "0.0"];
  assert.equal(hasPassRateBounds(candidate), true, "malformed bounds still count as present");
  assert.equal(readBounds(noise.pass_rate_bounds).kind, "malformed");
});

test("G5 blocks (writer shape): rates, end-to-end rate and the bounds only where errors occurred", () => {
  const edited = clone(fixture);
  const candidate = payloadOf(edited).candidates[0];
  const planted = Object.keys(candidate.pipeline).find((arm) => arm !== "noise");
  assert.ok(planted !== undefined);
  candidate.pipeline.noise.sealed_oos_g5 = clone(NOISE_G5);
  candidate.pipeline[planted].sealed_oos_g5 = clone(PLANTED_G5_WITH_ERRORS);
  assert.deepEqual(sealedG5Arms(candidate), Object.keys(candidate.pipeline));

  const noise = sealedG5Of(candidate.pipeline.noise);
  assert.ok(noise !== null);
  assert.equal(noise.detector_errors, 0);
  assert.equal(readBounds(noise.pass_rate_bounds).kind, "absent");
  assert.equal(readBounds(noise.end_to_end_bounds).kind, "absent");
  assert.equal(passRate(noise.end_to_end_g0_g5)?.label, "false_positive_rate");

  const erred = sealedG5Of(candidate.pipeline[planted]);
  assert.ok(erred !== null);
  assert.equal(erred.detector_errors, erred.reached);
  assert.deepEqual(readBounds(erred.pass_rate_bounds), { kind: "bounds", lower: "0.000000", upper: "1.000000" });
  assert.deepEqual(readBounds(erred.end_to_end_bounds), { kind: "bounds", lower: "0.000000", upper: "1.000000" });
  assert.equal(passRate(erred.end_to_end_g0_g5)?.label, "power");
});
