// Shared, tree-shaken ECharts setup (apps/web README "Code splitting"): every chart-bearing page
// imports `echarts` from here instead of the full "echarts" package, so Rollup only bundles the
// chart types / components the console actually uses (bar: Research Loop; heatmap: State x
// Strategy Matrices; line: Router Paper Runs) into one shared, lazily-loaded chunk, not the whole
// ~1 MB library.
import * as echarts from "echarts/core";
import { BarChart, HeatmapChart, LineChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  HeatmapChart,
  LineChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export { echarts };
