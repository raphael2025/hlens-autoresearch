import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asCalibrationPayload,
  ciCell,
  detectorErrorRuns,
  detectorErrorsOf,
  hasDetectorErrors,
  passRate,
} from "./gateCalibration.ts";

// The current (contract 2.1.0) report and the legacy readable 2.0.0 one (apps/web/fixtures/README.md).
const fixtures = fixtureEnvelopes("gate_calibration");
const [fixture] = fixtures;

test("both committed fixtures (2.1.0 and legacy 2.0.0) parse with every candidate", () => {
  assert.equal(fixtures.length, 2);
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
