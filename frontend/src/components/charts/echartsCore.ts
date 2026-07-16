/**
 * ECharts 按需注册单一入口：全前端只允许从这里 import echarts。
 *
 * 为什么不用 echarts-for-react：它默认整包引入 echarts（~1MB min），维护低频
 * （size-sensor 全局监听的 resize 方案也难测试）；这里走 echarts/core 只注册
 * 折线/条形 + 5 个组件，配合自写 EChartContainer（~120 行，生命周期可单测），
 * vendor-echarts chunk 控制在整包的一半以下。新图表类型（饼图等）在此追加注册。
 */
import * as echarts from "echarts/core";
import { BarChart, CustomChart, LineChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type {
  BarSeriesOption,
  CustomSeriesOption,
  LineSeriesOption,
} from "echarts/charts";
import type {
  AriaComponentOption,
  DataZoomComponentOption,
  GridComponentOption,
  LegendComponentOption,
  MarkLineComponentOption,
  TooltipComponentOption,
  VisualMapComponentOption,
} from "echarts/components";
import type { ComposeOption } from "echarts/core";
import { buildChartTheme, CHART_THEME_NAME } from "./chartTheme";

echarts.use([
  BarChart,
  CustomChart,
  LineChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

echarts.registerTheme(CHART_THEME_NAME, buildChartTheme());

/** 本项目图表 option 的合成类型：只含已注册模块，用了未注册的组件会在编译期暴露。 */
export type ECOption = ComposeOption<
  | BarSeriesOption
  | CustomSeriesOption
  | LineSeriesOption
  | AriaComponentOption
  | DataZoomComponentOption
  | GridComponentOption
  | LegendComponentOption
  | MarkLineComponentOption
  | TooltipComponentOption
  | VisualMapComponentOption
>;

export type EChartsInstance = ReturnType<typeof echarts.init>;

export { echarts, CHART_THEME_NAME };
