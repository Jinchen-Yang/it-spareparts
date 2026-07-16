import { COLORS } from "../../theme";

/**
 * 图表颜色 token 单一真值源：所有图表组件从这里取色，不得内联十六进制。
 *
 * 系列色经 dataviz 六检验证（浅色面 #FFFFFF）：亮度带 / 色度下限 / 相邻对 CVD ΔE /
 * 对比度全 PASS。两处刻意偏离 theme.ts 的说明：
 * - profit 不用 COLORS.success(#4F875C)：该绿 OKLCH 色度 0.089 低于 0.1 下限，
 *   在图表细线宽下读作灰色；图表专用 #2E8B57 同色相加饱和（文字/Tag 仍用 theme 绿）。
 * - profitNegative(#BE564B) 与 profit 的 protan ΔE=11.8 落在 8–12 下限带——红绿色弱
 *   用户依赖辅助编码而非色相：0 轴虚线参考线（位置编码）+ 数值负号 + tooltip 文字。
 *   任何用到负值变色的图表必须同时保留这三样，不得只靠颜色。
 */
export const CHART_COLORS = {
  /** 销售额系列（=theme 强调蓝） */
  sales: COLORS.accent,
  /** 强调点/选中标记（=theme 深强调蓝） */
  emphasis: COLORS.accentStrong,
  /** 采购额系列（=theme 暖琥珀） */
  purchase: COLORS.warning,
  /** 毛利系列（图表专用绿，见文件头说明） */
  profit: "#2E8B57",
  /** 负毛利分段（=theme 暖红） */
  profitNegative: COLORS.danger,
  axisLine: COLORS.border,
  splitLine: COLORS.borderSoft,
  axisLabel: COLORS.text3,
  text: COLORS.text,
  text2: COLORS.text2,
  tooltipBg: COLORS.surface,
  tooltipBorder: COLORS.border,
  crosshair: COLORS.text3,
  dataZoomFill: COLORS.accentSoft,
  dataZoomBorder: COLORS.accentSoftBorder,
  /** 图表附属选中卡片背景 */
  selectionBg: COLORS.accentSoft,
} as const;

export const CHART_THEME_NAME = "spareparts";

const FONT_FAMILY =
  "'PingFang SC', -apple-system, BlinkMacSystemFont, 'Microsoft YaHei', 'Segoe UI', sans-serif";

/**
 * ECharts 主题对象（echartsCore 注册为 "spareparts"）。
 * 只放"底盘"样式：文字、轴、分隔线、tooltip 容器、dataZoom 皮肤。
 * 系列颜色一律由各组件按业务语义显式指定（dataviz 规则：色随实体固定分配，
 * 不进主题 color 列表吃自动轮换——轮换色在系列增减时会漂移）。
 */
export function buildChartTheme(): Record<string, unknown> {
  const axis = {
    axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
    axisTick: { lineStyle: { color: CHART_COLORS.axisLine } },
    axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 11 },
    splitLine: { lineStyle: { color: CHART_COLORS.splitLine } },
    nameTextStyle: { color: CHART_COLORS.text2 },
  };
  return {
    textStyle: { fontFamily: FONT_FAMILY, color: CHART_COLORS.text },
    categoryAxis: { ...axis, splitLine: { show: false } },
    valueAxis: axis,
    timeAxis: axis,
    legend: {
      textStyle: { color: CHART_COLORS.text2, fontSize: 12 },
      itemWidth: 14,
      itemHeight: 8,
    },
    tooltip: {
      backgroundColor: CHART_COLORS.tooltipBg,
      borderColor: CHART_COLORS.tooltipBorder,
      borderWidth: 1,
      textStyle: { color: CHART_COLORS.text, fontSize: 12 },
      extraCssText: "box-shadow: 0 4px 14px rgba(40,33,24,.12); border-radius: 8px;",
    },
    dataZoom: {
      borderColor: CHART_COLORS.dataZoomBorder,
      fillerColor: "rgba(62,111,209,.10)",
      handleStyle: { color: COLORS.surface, borderColor: COLORS.accent },
      moveHandleStyle: { color: COLORS.accent },
      dataBackground: {
        lineStyle: { color: CHART_COLORS.axisLine },
        areaStyle: { color: CHART_COLORS.splitLine },
      },
      selectedDataBackground: {
        lineStyle: { color: COLORS.accent },
        areaStyle: { color: CHART_COLORS.dataZoomFill },
      },
      textStyle: { color: CHART_COLORS.axisLabel },
    },
  };
}
