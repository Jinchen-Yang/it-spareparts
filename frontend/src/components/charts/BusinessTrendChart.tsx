import { useMemo } from "react";
import EChartContainer from "./EChartContainer";
import { CHART_COLORS } from "./chartTheme";
import type { ECOption } from "./echartsCore";
import { EMPTY, escapeHtml, moneyAxis, moneyExact, pctSigned } from "../../utils/format";

/**
 * 经营趋势图：销售额 / 采购额 / 毛利三条折线（金额同一量纲，共用单轴——不做双轴）。
 * 数据由调用方喂（看板 /dashboard/trend 的 TrendPoint 可直接映射），组件不发请求。
 * 直线连接不做 smooth 平滑：平滑曲线会在相邻点间捏造不存在的极值，扭曲经营走势。
 */
export type TrendGranularity = "day" | "week" | "month";
export type TrendSeriesKey = "sales_ex_tax" | "purchase_ex_tax" | "gross_profit";

/** 同比/环比为小数比率（0.123 = +12.3%）；调用方没有就不传，tooltip 自动省略该行。 */
export interface TrendCompareInfo {
  yoy?: number | null;
  mom?: number | null;
}

export interface BusinessTrendPoint {
  /** 粒度对应的期标签：day=YYYY-MM-DD，week/month 沿用后端返回原文 */
  period: string;
  /** null=该期无数据（画成断点，绝不折算成 0） */
  sales_ex_tax: number | null;
  purchase_ex_tax: number | null;
  gross_profit: number | null;
  compare?: Partial<Record<TrendSeriesKey, TrendCompareInfo>>;
}

export interface BusinessTrendChartProps {
  data: BusinessTrendPoint[];
  granularity: TrendGranularity;
  loading?: boolean;
  error?: string | null;
  height?: number;
  /** 点击某期的数据点：上层用 period 反查该期订单做下钻筛选。 */
  onPointClick?: (period: string, point: BusinessTrendPoint) => void;
}

const SERIES: { key: TrendSeriesKey; name: string; color: string }[] = [
  { key: "sales_ex_tax", name: "销售额", color: CHART_COLORS.sales },
  { key: "purchase_ex_tax", name: "采购额", color: CHART_COLORS.purchase },
  { key: "gross_profit", name: "毛利额", color: CHART_COLORS.profit },
];

const GROSS_PROFIT_INDEX = SERIES.findIndex((s) => s.key === "gross_profit");

/** 期标签压缩只做"日→去年份"这一档；周/月标签本就短，改写反而造成歧义。 */
const axisPeriodLabel = (period: string, granularity: TrendGranularity): string =>
  granularity === "day" && /^\d{4}-\d{2}-\d{2}$/.test(period) ? period.slice(5) : period;

interface TooltipParam {
  seriesName?: string;
  seriesIndex?: number;
  dataIndex?: number;
  marker?: string;
  value?: unknown;
}

/** tooltip：期全称 + 三系列精确金额 +（若提供）同比/环比。纯函数便于单测。 */
export function formatTrendTooltip(
  params: TooltipParam[],
  data: BusinessTrendPoint[],
): string {
  const first = params[0];
  const point = first?.dataIndex != null ? data[first.dataIndex] : undefined;
  if (!point) return "";
  const rows = params.map((p) => {
    const def = p.seriesIndex != null ? SERIES[p.seriesIndex] : undefined;
    const v = typeof p.value === "number" ? p.value : null;
    const cmp = def ? point.compare?.[def.key] : undefined;
    const cmpParts = [
      cmp?.yoy != null ? `同比 ${pctSigned(cmp.yoy)}` : null,
      cmp?.mom != null ? `环比 ${pctSigned(cmp.mom)}` : null,
    ].filter(Boolean);
    const cmpHtml = cmpParts.length
      ? `<span style="color:${CHART_COLORS.axisLabel};margin-left:8px">${cmpParts.join(" · ")}</span>`
      : "";
    return `<div style="display:flex;justify-content:space-between;gap:16px;line-height:1.9">`
      + `<span>${p.marker ?? ""}${escapeHtml(p.seriesName)}</span>`
      + `<span><b>${v == null ? EMPTY : moneyExact(v)}</b>${cmpHtml}</span></div>`;
  });
  return `<div style="font-weight:600;margin-bottom:2px">${escapeHtml(point.period)}</div>${rows.join("")}`;
}

/** 像素反解所需的最小 chart 接口（结构化子集，便于单测传桩）。 */
export interface TrendClickChart {
  containPixel: (finder: unknown, value: number[]) => boolean;
  convertFromPixel: (finder: unknown, value: number[]) => number[] | number;
}

/**
 * 绘图区任意位置点击 → 最近日期的数据点（zr:click + convertFromPixel 反解）。
 * 不用 series click：120 点日线收起符号后 2px 线体几乎点不中，"点日期下钻"
 * 必须整列可命中。网格外（图例/dataZoom）点击返回 null。纯函数便于单测。
 */
export function resolveTrendZrClick(
  chart: TrendClickChart | null | undefined,
  params: unknown,
  data: BusinessTrendPoint[],
): { period: string; point: BusinessTrendPoint } | null {
  const p = params as { offsetX?: number; offsetY?: number } | null;
  if (!chart || !p || typeof p.offsetX !== "number" || typeof p.offsetY !== "number") return null;
  const pixel = [p.offsetX, p.offsetY];
  if (!chart.containPixel({ gridIndex: 0 }, pixel)) return null;
  const converted = chart.convertFromPixel({ gridIndex: 0 }, pixel);
  const idx = Math.round(Array.isArray(converted) ? converted[0] : converted);
  const point = data[idx];
  return point ? { period: point.period, point } : null;
}

export function buildTrendOption(
  data: BusinessTrendPoint[],
  granularity: TrendGranularity,
): ECOption {
  // 点数多时收掉常驻圆点并启用 LTTB 降采样：LTTB 是抽稀（保留真实极值点），
  // 不是平滑（捏造中间值），走势不失真；悬浮时十字指针 + tooltip 仍逐点精确。
  const dense = data.length > 60;
  // visualMap 分段边界必须有限：echarts 的 PiecewiseModel.getVisualMeta 把无界
  // 区间只写进 outerColors、不产 stops，stops 为空时 LineView.getVisualGradient
  // 越过守卫读 colorStopsInRange[0] 直接崩（5.6.0/6.1.0 实测同病，见 PR 说明）。
  // 边界取 2×max|毛利|：覆盖全部真实数据，且 gradient 跨度不失精度。
  const profitAbs = data
    .map((p) => p.gross_profit)
    .filter((v): v is number => v != null && Number.isFinite(v))
    .map(Math.abs);
  const profitBound = Math.max(1, ...(profitAbs.length ? profitAbs : [1])) * 2;
  return {
    aria: { enabled: true },
    animationDuration: 200,
    legend: { top: 0, left: 0 },
    grid: { left: 8, right: 16, top: 36, bottom: 64, containLabel: true },
    tooltip: {
      trigger: "axis",
      confine: true, // 容器 overflow:hidden（防 tooltip 尸体撑宽页面），可见时须限制在图内
      axisPointer: {
        type: "cross",
        crossStyle: { color: CHART_COLORS.crosshair },
        label: { backgroundColor: CHART_COLORS.text2 },
      },
      formatter: (params: unknown) =>
        formatTrendTooltip(
          (Array.isArray(params) ? params : [params]) as TooltipParam[],
          data,
        ),
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: data.map((p) => p.period),
      axisLabel: { formatter: (v: string) => axisPeriodLabel(v, granularity) },
      axisPointer: { label: { formatter: ({ value }: { value: unknown }) => String(value) } },
    },
    yAxis: {
      type: "value",
      axisLabel: { formatter: (v: number) => moneyAxis(v) },
      axisPointer: { label: { formatter: ({ value }: { value: unknown }) => moneyAxis(Number(value)) } },
    },
    dataZoom: [
      // inside：滚轮缩放 + 按住拖动平移；slider：长时间范围的窗口拖选
      { type: "inside", zoomOnMouseWheel: true, moveOnMouseMove: true, throttle: 50 },
      { type: "slider", height: 20, bottom: 8 },
    ],
    // 负毛利变色：按 y 值分段上色（毛利系列专属）。红绿弱视辅助编码=0 轴参考线+负号。
    visualMap: {
      show: false,
      type: "piecewise",
      seriesIndex: GROSS_PROFIT_INDEX,
      dimension: 1,
      pieces: [
        { gte: -profitBound, lt: 0, color: CHART_COLORS.profitNegative },
        { gte: 0, lte: profitBound, color: CHART_COLORS.profit },
      ],
    },
    series: SERIES.map((s, i) => ({
      name: s.name,
      type: "line" as const,
      smooth: false,
      connectNulls: false,
      showSymbol: !dense,
      symbol: "circle",
      symbolSize: 6,
      sampling: dense ? ("lttb" as const) : undefined,
      lineStyle: { width: 2 },
      itemStyle: { color: s.color },
      emphasis: { focus: "series" as const },
      data: data.map((p) => p[s.key]),
      ...(i === GROSS_PROFIT_INDEX
        ? {
            markLine: {
              silent: true,
              symbol: "none",
              label: { show: false },
              lineStyle: { type: "dashed" as const, color: CHART_COLORS.axisLabel },
              data: [{ yAxis: 0 }],
            },
          }
        : {}),
    })),
  };
}

export default function BusinessTrendChart({
  data, granularity, loading, error, height = 340, onPointClick,
}: BusinessTrendChartProps) {
  const option = useMemo(() => buildTrendOption(data, granularity), [data, granularity]);
  const onEvents = useMemo(
    () => ({
      "zr:click": (params: unknown, chart: unknown) => {
        const hit = resolveTrendZrClick(chart as TrendClickChart, params, data);
        if (hit && onPointClick) onPointClick(hit.period, hit.point);
      },
    }),
    // data/onPointClick 经 EChartContainer 的 ref 桥取最新值，这里保持引用稳定即可
    [data, onPointClick],
  );
  return (
    <EChartContainer
      option={option}
      loading={loading}
      error={error}
      empty={data.length === 0}
      height={height}
      onEvents={onEvents}
      ariaLabel="经营趋势：销售额、采购额、毛利额折线图"
      testId="business-trend-chart"
    />
  );
}
