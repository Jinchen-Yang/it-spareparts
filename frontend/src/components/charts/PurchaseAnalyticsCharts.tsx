import { useMemo } from "react";
import EChartContainer from "./EChartContainer";
import type { ECOption } from "./echartsCore";
import { CHART_COLORS } from "./chartTheme";
import { escapeHtml, moneyAxis, moneyExact } from "../../utils/format";
import type { PurchaseAnalyticsGroup } from "../../api/purchaseAnalytics";

/**
 * 采购分析图表（Q-A-01 / D-39 补图表）：日 / 月趋势柱图 + 当前分组维度排行横条图。
 * 数据全部来自既有 /purchases/analytics/summary（趋势 = group_by=day/month 的同端点
 * 第二次请求；排行 = 与表格同一次请求的 groups，同源同 drill），不新增后端、不做
 * 新客 / 滑块 / 毛利。约定同 SalesDashboardCharts：option 纯函数可单测；null ≠ 0
 * （null 剔除断点，绝不画 0）；颜色只从 CHART_COLORS 取；负值换色带负号 + 0 轴
 * 参考线；单图单纵轴。
 */

export type PurchaseChartMetric = "amount_inc" | "amount_ex" | "amount_paid" | "orders";

export const PURCHASE_METRIC_LABELS: Record<PurchaseChartMetric, string> = {
  amount_inc: "含税金额合计",
  amount_ex: "不含税金额合计",
  amount_paid: "实付金额合计",
  orders: "采购单数",
};

/** 采购侧无日期桶的后端文本键（purchase_analytics.EMPTY_LABEL，与 UI 约定一致）。 */
export const PURCHASE_EMPTY_KEY = "(空)";

const isMoney = (m: PurchaseChartMetric) => m !== "orders";
const fmtValue = (m: PurchaseChartMetric, v: number | null | undefined) =>
  v == null ? "—" : isMoney(m) ? moneyExact(v) : `${v} 单`;

function valueOf(g: PurchaseAnalyticsGroup, metric: PurchaseChartMetric): number | null {
  const v = g[metric];
  return v == null || !Number.isFinite(Number(v)) ? null : Number(v);
}

/**
 * 日 / 月桶排序：日期文本键升序；「(空)」（无订单日期）恒排最后——与销售侧
 * 「无日期列在末端」同一语义，不猜归属。
 */
export function sortTrendGroups(groups: PurchaseAnalyticsGroup[]): PurchaseAnalyticsGroup[] {
  return [...groups].sort((a, b) => {
    const aEmpty = a.key === PURCHASE_EMPTY_KEY;
    const bEmpty = b.key === PURCHASE_EMPTY_KEY;
    if (aEmpty !== bEmpty) return aEmpty ? 1 : -1;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });
}

const barItemStyle = (v: number) => ({
  itemStyle: {
    color: v < 0 ? CHART_COLORS.profitNegative : CHART_COLORS.purchase,
    borderRadius: [4, 4, 0, 0] as [number, number, number, number],
  },
});

/** 趋势柱图 option：按传入顺序（调用方先 sortTrendGroups）；null 值剔除断点。 */
export function buildPurchaseTrendOption(
  groups: PurchaseAnalyticsGroup[],
  metric: PurchaseChartMetric,
): ECOption {
  const labels = groups.map((g) => (g.key === PURCHASE_EMPTY_KEY ? "无日期" : g.key));
  return {
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (p: unknown) => {
        const { dataIndex, value } = p as { dataIndex: number; value: number | null };
        const g = groups[dataIndex];
        const name = g ? (g.key === PURCHASE_EMPTY_KEY ? "无日期" : g.key) : "";
        return `<b>${escapeHtml(name)}</b><br/>${PURCHASE_METRIC_LABELS[metric]}：${fmtValue(metric, value)}`;
      },
    },
    xAxis: {
      type: "category",
      data: labels,
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel, hideOverlap: true },
      axisTick: { alignWithLabel: true },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        fontSize: 11, color: CHART_COLORS.axisLabel,
        formatter: (v: number) => (isMoney(metric) ? moneyAxis(v) : String(v)),
      },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    series: [{
      type: "bar",
      name: PURCHASE_METRIC_LABELS[metric],
      barMaxWidth: 26,
      data: groups.map((g) => {
        const v = valueOf(g, metric);
        // null 不画 0：缺失桶留空（断点），不冒充 0
        return v == null ? null : { value: v, ...barItemStyle(v) };
      }),
      markLine: groups.some((g) => {
        const v = valueOf(g, metric);
        return v != null && v < 0;
      }) ? {
        silent: true, symbol: "none",
        lineStyle: { color: CHART_COLORS.crosshair, type: "solid" },
        data: [{ yAxis: 0 }], label: { show: false },
      } : undefined,
    }],
  } as ECOption;
}

/** 排行横条 option：按传入顺序（表格已排好序）；null 项剔除并在组件副标题注明。 */
export function buildPurchaseRankOption(
  items: { label: string; group: PurchaseAnalyticsGroup }[],
  metric: PurchaseChartMetric,
): ECOption {
  const present = items.filter((i) => valueOf(i.group, metric) !== null);
  return {
    grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
    xAxis: {
      type: "value",
      axisLabel: {
        fontSize: 11, color: CHART_COLORS.axisLabel,
        formatter: (v: number) => (isMoney(metric) ? moneyAxis(v) : String(v)),
      },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: present.map((i) => i.label),
      axisLabel: { fontSize: 12, color: CHART_COLORS.text2, width: 140, overflow: "truncate" },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
    },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (p: unknown) => {
        const { name, value } = p as { name: string; value: number };
        return `<b>${escapeHtml(name)}</b><br/>${PURCHASE_METRIC_LABELS[metric]}：${fmtValue(metric, value)}`;
      },
    },
    series: [{
      type: "bar",
      name: PURCHASE_METRIC_LABELS[metric],
      barMaxWidth: 18,
      data: present.map((i) => {
        const v = valueOf(i.group, metric) as number;
        return {
          value: v,
          itemStyle: {
            color: v < 0 ? CHART_COLORS.profitNegative : CHART_COLORS.purchase,
            borderRadius: v < 0 ? [4, 0, 0, 4] : [0, 4, 4, 0],
          },
        };
      }),
      label: {
        show: true,
        position: "right",
        fontSize: 11,
        color: CHART_COLORS.text2,
        formatter: (p: unknown) => {
          const v = (p as { value: number }).value;
          return isMoney(metric) ? moneyAxis(v) : String(v);
        },
      },
      markLine: present.some((i) => (valueOf(i.group, metric) as number) < 0) ? {
        silent: true, symbol: "none",
        lineStyle: { color: CHART_COLORS.crosshair, type: "solid" },
        data: [{ xAxis: 0 }],
        label: { show: false },
      } : undefined,
    }],
  } as ECOption;
}

export function PurchaseTrendChart({
  groups, metric, loading, onBucketClick,
}: {
  groups: PurchaseAnalyticsGroup[];
  metric: PurchaseChartMetric;
  loading?: boolean;
  /** 点某根柱 → 下钻到该日 / 月的订单列表（与表格行点击同一 drill 语义）。 */
  onBucketClick?: (key: string) => void;
}) {
  const sorted = useMemo(() => sortTrendGroups(groups), [groups]);
  const option = useMemo(() => buildPurchaseTrendOption(sorted, metric), [sorted, metric]);
  return (
    <EChartContainer
      option={option}
      loading={loading}
      empty={sorted.length === 0}
      emptyText="当前筛选下没有趋势数据"
      height={260}
      ariaLabel={`采购趋势：${PURCHASE_METRIC_LABELS[metric]}`}
      onEvents={{
        click: (params) => {
          const dataIndex = (params as { dataIndex?: number }).dataIndex;
          if (dataIndex == null || !onBucketClick) return;
          const g = sorted[dataIndex];
          if (g) onBucketClick(g.key);
        },
      }}
    />
  );
}

export function PurchaseRankBar({
  groups, metric, loading, labelOf, onItemClick,
}: {
  groups: PurchaseAnalyticsGroup[];
  metric: PurchaseChartMetric;
  loading?: boolean;
  /** 分组行 → 显示名（与表格同标签：客户名 / label / key）。 */
  labelOf: (g: PurchaseAnalyticsGroup) => string;
  /** 点某条 → 下钻该组（与表格行点击同一 drill 语义，回传分组键）。 */
  onItemClick?: (key: string) => void;
}) {
  const items = useMemo(
    () => groups.map((g) => ({ label: labelOf(g), group: g })),
    [groups, labelOf],
  );
  const option = useMemo(() => buildPurchaseRankOption(items, metric), [items, metric]);
  const missing = items.filter((i) => valueOf(i.group, metric) === null).length;
  const height = Math.max(160, Math.min(items.length, 30) * 26 + 24);
  return (
    <div>
      <EChartContainer
        option={option}
        loading={loading}
        empty={items.length - missing === 0}
        emptyText="当前筛选下没有可画的数据"
        height={height}
        ariaLabel={`采购分组排行：${PURCHASE_METRIC_LABELS[metric]}`}
        onEvents={{
          click: (params) => {
            const name = (params as { name?: string }).name;
            if (!name || !onItemClick) return;
            const hit = items.find((i) => i.label === name);
            if (hit) onItemClick(hit.group.key);
          },
        }}
      />
      {missing > 0 && (
        <div style={{ fontSize: 12, color: CHART_COLORS.axisLabel }}>
          另有 {missing} 组该指标无数据，未画出（见下表）
        </div>
      )}
    </div>
  );
}
