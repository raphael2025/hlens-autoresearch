import assert from "node:assert/strict";
import { test } from "node:test";
import type { EChartsOption } from "echarts";
import { SVGRenderer } from "echarts/renderers";
import { echarts } from "./echarts.ts";
import { fixtureEnvelopes } from "./fixtures.test-util.ts";
import { usageChartOption, roundRows, usageSeries } from "./researchLoop.ts";
import { asPaperDeviationPayload, deviationChartOption } from "./paperDeviation.ts";
import {
  asRouterPayload,
  equityChartOption,
  equitySeries,
  weightsChartOption,
  weightSeries,
} from "./routerPaperRun.ts";
import {
  asMatrixPayload,
  heatmapChartOption,
  heatmapGrid,
} from "./stateStrategyMatrix.ts";

// Register the production chart types/components from src/lib/echarts.ts plus the SVG renderer
// for deterministic, DOM-free SSR acceptance. The pages still initialize their charts as SVG-less
// browser canvas instances; this test checks that the same option builders render in ECharts 6.
echarts.use([SVGRenderer]);

function assertRenderedSvg(name: string, option: EChartsOption) {
  const chart = echarts.init(null, null, {
    renderer: "svg",
    ssr: true,
    width: 640,
    height: 320,
  });
  try {
    chart.setOption(option);
    const svg = chart.renderToSVGString();
    assert.ok(svg.startsWith("<svg"), `${name}: output starts with SVG markup`);
    assert.match(svg, /<path\b/, `${name}: output includes rendered paths`);
    assert.ok(svg.length > 500, `${name}: SVG output is nontrivial (${svg.length} bytes)`);
  } finally {
    chart.dispose();
  }
}

test("ECharts 6 SSR renders page-built options from representative report fixtures", () => {
  const rounds = roundRows(fixtureEnvelopes("research_loop_round"));
  const usage = usageSeries(rounds);
  assert.ok(usage.labels.length > 0, "Research Loop fixture yields chart labels");
  assertRenderedSvg("ResearchLoop usage", usageChartOption(usage.labels, usage.series[0]));

  const deviationEnvelope = fixtureEnvelopes("paper_deviation").find((envelope) => {
    const payload = asPaperDeviationPayload(envelope.payload);
    return payload !== null && payload.marks.length > 0;
  });
  assert.ok(deviationEnvelope, "Paper Deviation fixture has marks");
  const deviation = asPaperDeviationPayload(deviationEnvelope.payload);
  assert.ok(deviation !== null);
  assertRenderedSvg("PaperDeviation", deviationChartOption(deviation));

  const routerEnvelope = fixtureEnvelopes("router_paper_run")[0];
  const router = asRouterPayload(routerEnvelope?.payload);
  assert.ok(router !== null, "Router fixture parses");
  assertRenderedSvg("Router weights", weightsChartOption(weightSeries(router)));
  assertRenderedSvg("Router equity", equityChartOption(equitySeries(router)));

  const matrixEnvelope = fixtureEnvelopes("state_strategy_matrix")[0];
  const matrix = asMatrixPayload(matrixEnvelope?.payload);
  assert.ok(matrix !== null, "State × Strategy fixture parses");
  assertRenderedSvg("StateStrategy heatmap", heatmapChartOption(matrix, heatmapGrid(matrix)));
});
