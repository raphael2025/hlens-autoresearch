import assert from "node:assert/strict";
import { test } from "node:test";
import { clone, fixtureEnvelopes } from "./fixtures.test-util.ts";
import {
  asMatrixPayload,
  asNumber,
  heatmapGrid,
  matrixLabel,
  METRICS,
  normalizeRow,
  stateLabel,
  totalSamples,
} from "./stateStrategyMatrix.ts";

// apps/web/fixtures/state_strategy_matrix/: the current report (built at contract 2.1.0) and the
// legacy readable 2.0.0 one — the same matrix, whose bound backtest / state result hashes differ.
const fixtures = fixtureEnvelopes("state_strategy_matrix");
const LEGACY_ID = "5940a5de3bde080ff156d564ce582d73d1db85623fea163b53ba925030fec21c";

function payloadOf(payload: Record<string, unknown>) {
  const matrix = asMatrixPayload(payload);
  assert.ok(matrix !== null, "the fixture is a state x strategy matrix payload");
  return matrix;
}

test("both committed fixtures (current and legacy 2.0.0) parse to the same cells", () => {
  assert.equal(fixtures.length, 2);
  assert.ok(fixtures.some((envelope) => envelope.id === LEGACY_ID));
  const [a, b] = fixtures.map((envelope) => payloadOf(envelope.payload));
  for (const [envelope, matrix] of [
    [fixtures[0], a],
    [fixtures[1], b],
  ] as const) {
    assert.equal(matrix.matrix_hash, envelope.id);
    assert.equal(totalSamples(matrix), 4);
    assert.equal(matrixLabel(envelope.id, envelope.payload), `${matrix.strategy} × ${matrix.state} (n=4)`);
    assert.deepEqual(
      matrix.cells.map((cell) => stateLabel(cell.state)),
      ["high", "low", "(none)"],
    );
  }
  assert.deepEqual(a.cells, b.cells);
  assert.notEqual(a.backtest_result_hash, b.backtest_result_hash);
});

test("the heatmap keeps raw values per metric and normalizes each row to 0..1", () => {
  const matrix = payloadOf(fixtures[0].payload);
  const grid = heatmapGrid(matrix);
  assert.deepEqual(
    METRICS.map((metric) => metric.key),
    ["count", "mean", "hit_rate", "total"],
  );
  assert.deepEqual(grid.raw[0], [1, 2, 1]); // count
  assert.deepEqual(grid.raw[1], [0.1, -0.05, 0.1]); // mean (decimal strings parsed)
  assert.deepEqual(grid.normalized[0], [0, 1, 0]);
  assert.deepEqual(grid.normalized[2], [1, 0, 1]); // hit_rate 1, 0, 1
});

test("normalization: all-equal rows sit mid-scale, missing values stay missing", () => {
  assert.deepEqual(normalizeRow([2, 2, null]), [0.5, 0.5, null]);
  assert.deepEqual(normalizeRow([null, null]), [null, null]);
  assert.equal(asNumber(null), null);
  assert.equal(asNumber(""), null);
  assert.equal(asNumber("abc"), null);
  assert.equal(asNumber("0E-18"), 0);
});

test("a matrix with no provenance hashes still parses (a plain returns x states matrix)", () => {
  const plain = clone(fixtures[0].payload);
  plain.backtest_result_hash = null;
  plain.state_result_hash = null;
  assert.equal(payloadOf(plain).backtest_result_hash, null);
});

test("a payload that is not a matrix is not parsed as one", () => {
  assert.equal(asMatrixPayload(undefined), null);
  assert.equal(asMatrixPayload({ cells: [] }), null); // no strategy / state / matrix_hash
  const bad = clone(fixtures[0].payload);
  bad.cells = [{ state: "high" }]; // no count / top_returns
  assert.equal(asMatrixPayload(bad), null);
  const [router] = fixtureEnvelopes("router_paper_run");
  assert.equal(asMatrixPayload(router.payload), null);
  assert.equal(matrixLabel("abc", {}), "abc");
});
