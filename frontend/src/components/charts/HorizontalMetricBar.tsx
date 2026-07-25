import { useMemo } from "react";
import EChartContainer from "./EChartContainer";
import { CHART_COLORS } from "./chartTheme";
import type { ECOption } from "./echartsCore";
import { EMPTY, escapeHtml, moneyAxis, moneyExact, pctSigned, qty } from "../../utils/format";

/**
 * 横向指标条形图：纵轴 PN、横轴金额，自动从高到低排序。
 * 采购/销售 × 平均单价/金额合计 四种口径由 mode/metric 组合；数据由调用方喂
 * （盈亏榜 / 池成员均可映射成 MetricBarItem），组件不发请求。
 */
export type MetricBarMode = "purchase" | "sales";
export type MetricBarMetric = "average" | "total";

export interface MetricBarItem {
  part_id: number;
  pn: string;
  description?: string | null;
  /** 数量（采购=采购量 / 销售=销量），仅进 tooltip */
  qty?: number | null;
  order_count?: number | null;
  /** 最近一次交易日期（YYYY-MM-DD） */
  last_date?: string | null;
  /** 当前指标值：metric=average 时为平均单价，total 时为金额合计。null=无数据，不画柱 */
  value: number | null;
  /** 所在互通池的平均值（同 metric 口径），仅参考 */
  pool_avg?: number | null;
  /** 人工约束价（单价口径：采购封顶/销售保底） */
  constraint_price?: number | null;
}

export interface HorizontalMetricBarProps {
  items: MetricBarItem[];
  mode: MetricBarMode;
  metric: MetricBarMetric;
  loading?: boolean;
  error?: string | null;
  /** 缺省按可见行数自适应高度 */
  height?: number;
  /** 一屏最多几根柱，超出走滚动/缩放（dataZoom） */
  visibleCount?: number;
  onPartClick?: (partId: number, pn: string) => void;
}

export const MODE_LABEL: Record<MetricBarMode, string> = { purchase: "采购", sales: "销售" };

/** 指标标签唯一出口：total 必须写"××金额合计"，禁止"价格合计"（金额≠价格）。 */
export function metricLabel(mode: MetricBarMode, metric: MetricBarMetric): string {
  if (metric === "average") return "平均单价";
  return mode === "purchase" ? "采购金额合计" : "销售金额合计";
}

export const MODE_COLOR: Record<MetricBarMode, string> = {
  purchase: CHART_COLORS.purchase,
  sales: CHART_COLORS.sales,
};

/**
 * 排序 + 空值语义：value 为 null/非有限数的项**整项剔除**（画成 0 长度柱=谎报
 * "金额为 0"），剔除数量交给上层展示。返回新数组，不改入参。
 */
export function sortMetricBarItems(items: MetricBarItem[]): {
  sorted: MetricBarItem[];
  excluded: number;
} {
  const valid = items.filter((it) => it.value != null && Number.isFinite(it.value));
  const sorted = [...valid].sort((a, b) => (b.value as number) - (a.value as number));
  return { sorted, excluded: items.length - valid.length };
}

/** series 柱条点击 → 对应 item；点到其他元素返回 null。纯函数便于单测。 */
export function resolveMetricBarClick(
  params: unknown,
  sorted: MetricBarItem[],
): MetricBarItem | null {
  const p = params as { componentType?: string; dataIndex?: number } | null;
  if (!p || p.componentType !== "series" || typeof p.dataIndex !== "number") return null;
  return sorted[p.dataIndex] ?? null;
}

/** tooltip：PN/描述/数量/订单数/最近日期/当前指标值/池平均值/约束价及差异。 */
export function formatMetricBarTooltip(
  item: MetricBarItem,
  mode: MetricBarMode,
  metric: MetricBarMetric,
): string {
  const line = (label: string, value: string) =>
    `<div style="display:flex;justify-content:space-between;gap:24px;line-height:1.9">`
    + `<span style="color:${CHART_COLORS.axisLabel}">${label}</span><span>${value}</span></div>`;
  const rows = [
    line("描述", escapeHtml(item.description) || EMPTY),
    line("数量", qty(item.qty)),
    line("订单数", item.order_count == null ? EMPTY : String(item.order_count)),
    line("最近日期", escapeHtml(item.last_date) || EMPTY),
    line(metricLabel(mode, metric), `<b>${moneyExact(item.value)}</b>`),
    line("池平均值", moneyExact(item.pool_avg)),
  ];
  // 约束价是单价口径：只在 average 下算差异；total（合计）与单价不同量纲，差异无意义。
  if (item.constraint_price == null) {
    rows.push(line("人工约束价", EMPTY));
  } else if (metric === "average" && item.value != null) {
    const diff = item.value - item.constraint_price;
    const ratio = item.constraint_price !== 0 ? diff / item.constraint_price : null;
    const sign = diff > 0 ? "+" : "";
    rows.push(line(
      "人工约束价",
      `${moneyExact(item.constraint_price)}（差异 ${sign}${moneyExact(diff)}`
      + `${ratio != null ? ` / ${pctSigned(ratio)}` : ""}）`,
    ));
  } else {
    rows.push(line("人工约束价", moneyExact(item.constraint_price)));
  }
  return `<div style="font-weight:600;margin-bottom:2px">${escapeHtml(item.pn)}</div>${rows.join("")}`;
}

export function buildMetricBarOption(
  sorted: MetricBarItem[],
  mode: MetricBarMode,
  metric: MetricBarMetric,
  visibleCount = 12,
): ECOption {
  const scrollable = sorted.length > visibleCount;
  return {
    aria: { enabled: true },
    animationDuration: 200,
    grid: { left: 8, right: scrollable ? 96 : 80, top: 8, bottom: 28, containLabel: true },
    tooltip: {
      trigger: "item",
      confine: true, // 配合容器 overflow:hidden，避免 tooltip 撑宽页面
      formatter: (params: unknown) => {
        const item = resolveMetricBarClick(params, sorted);
        return item ? formatMetricBarTooltip(item, mode, metric) : "";
      },
    },
    xAxis: {
      type: "value",
      axisLabel: { formatter: (v: number) => moneyAxis(v) },
    },
    yAxis: {
      type: "category",
      // inverse + 降序数据：第一名固定在最顶端
      inverse: true,
      data: sorted.map((it) => it.pn),
      // 超长 PN 截断到固定宽度（完整 PN 看 tooltip），不许把绘图区挤没
      axisLabel: {
        width: 132,
        overflow: "truncate" as const,
        fontFamily: "monospace",
        fontSize: 11,
      },
      axisTick: { show: false },
    },
    dataZoom: scrollable
      ? [
          // 滚轮=平移列表（不是缩放），符合"长列表滚动"直觉；右侧滑块可拖窗口。
          // 初始窗口两个 zoom 都要给：同轴联动时缺省的一方（0-100%）会把
          // 另一方的 startValue/endValue 冲掉，导致"一屏 N 根"失效。
          {
            type: "inside",
            yAxisIndex: 0,
            startValue: 0,
            endValue: visibleCount - 1,
            zoomOnMouseWheel: false,
            moveOnMouseWheel: true,
            moveOnMouseMove: true,
          },
          {
            type: "slider",
            yAxisIndex: 0,
            right: 6,
            width: 14,
            startValue: 0,
            endValue: visibleCount - 1,
            brushSelect: false,
          },
        ]
      : undefined,
    series: [
      {
        type: "bar" as const,
        name: `${MODE_LABEL[mode]} · ${metricLabel(mode, metric)}`,
        data: sorted.map((it) => it.value as number),
        itemStyle: { color: MODE_COLOR[mode], borderRadius: [0, 4, 4, 0] },
        barMaxWidth: 22,
        // 柱端数值标签：金额不靠颜色/长度独自表意（dataviz：色彩非唯一信息源）
        label: {
          show: true,
          position: "right" as const,
          fontSize: 11,
          color: CHART_COLORS.text2,
          formatter: ({ value }: { value: unknown }) => moneyAxis(Number(value)),
        },
        emphasis: { itemStyle: { opacity: 0.85 } },
      },
    ],
  };
}

export default function HorizontalMetricBar({
  items, mode, metric, loading, error, height, visibleCount = 12, onPartClick,
}: HorizontalMetricBarProps) {
  const { sorted, excluded } = useMemo(() => sortMetricBarItems(items), [items]);
  const option = useMemo(
    () => buildMetricBarOption(sorted, mode, metric, visibleCount),
    [sorted, mode, metric, visibleCount],
  );
  const onEvents = useMemo(
    () => ({
      click: (params: unknown) => {
        const item = resolveMetricBarClick(params, sorted);
        if (item && onPartClick) onPartClick(item.part_id, item.pn);
      },
    }),
    [sorted, onPartClick],
  );
  const rows = Math.min(sorted.length, visibleCount);
  const autoHeight = Math.max(180, 64 + rows * 32);
  return (
    <div style={{ width: "100%" }}>
      {/* 文字口径条：模式/指标/排序方向不依赖柱色表达 */}
      <div style={{ display: "flex", gap: 8, alignItems: "baseline", marginBottom: 4, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>
          <span style={{
            display: "inline-block", width: 10, height: 10, borderRadius: 2,
            background: MODE_COLOR[mode], marginRight: 6,
          }} />
          {MODE_LABEL[mode]} · {metricLabel(mode, metric)}
        </span>
        <span style={{ fontSize: 11.5, color: CHART_COLORS.axisLabel }}>从高到低</span>
        {excluded > 0 && (
          <span style={{ fontSize: 11.5, color: CHART_COLORS.axisLabel }} data-testid="metric-bar-excluded">
            另有 {excluded} 项无{metricLabel(mode, metric)}数据，未绘制
          </span>
        )}
      </div>
      <EChartContainer
        option={option}
        loading={loading}
        error={error}
        empty={sorted.length === 0}
        emptyText="窗口内无可绘制数据"
        height={height ?? autoHeight}
        onEvents={onEvents}
        ariaLabel={`${MODE_LABEL[mode]}${metricLabel(mode, metric)}排名条形图`}
        testId="horizontal-metric-bar"
      />
    </div>
  );
}
