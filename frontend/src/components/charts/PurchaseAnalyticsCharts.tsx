import { useMemo, useRef } from "react";
import EChartContainer from "./EChartContainer";
import type { ECOption } from "./echartsCore";
import { CHART_COLORS } from "./chartTheme";
import { escapeHtml, moneyAxis, moneyExact } from "../../utils/format";
import type { PurchaseAnalyticsGroup } from "../../api/purchaseAnalytics";
import { usePurchaseChartSample, type PurchaseChartSampleContext, type SampleTooltip } from "./usePurchaseChartSample";

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

function pointText(group: PurchaseAnalyticsGroup, axis: string, label: string, metric: PurchaseChartMetric, horizontal: boolean) {
  const dimension = `${horizontal ? "纵轴" : "横轴"} · ${axis}　${label}`;
  const metricField = `${horizontal ? "横轴" : "纵轴"} · ${PURCHASE_METRIC_LABELS[metric]}　${fmtValue(metric, valueOf(group, metric))}`;
  const counts = metric === "orders" ? [] : [`采购单数　${group.orders} 单`];
  return [dimension, metricField, ...counts, `明细行数　${group.lines} 行`];
}
function hitIndex(p: unknown) {
  const point = (Array.isArray(p) ? p[0] : p) as { dataIndex?: number } | undefined;
  return point?.dataIndex;
}
function usePointClick(onDrill?: (key: string) => void) {
  const lastTouch = useRef<{ key: string; at: number }>();
  return (params: unknown, group?: PurchaseAnalyticsGroup) => {
    if (!group || !onDrill) return;
    const native = (params as { event?: { event?: { zrByTouch?: boolean; pointerType?: string } } }).event?.event;
    if (native?.zrByTouch || native?.pointerType === "touch" || native?.pointerType === "pen") {
      if (lastTouch.current?.key !== group.key || Date.now() - lastTouch.current.at > 5000) {
        lastTouch.current = { key: group.key, at: Date.now() };
        return;
      }
    }
    lastTouch.current = undefined;
    onDrill(group.key);
  };
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
  axis = "采购日期",
  sample?: SampleTooltip,
): ECOption {
  const labels = groups.map((g) => (g.key === PURCHASE_EMPTY_KEY ? "无日期" : g.key));
  return {
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      confine: true,
      extraCssText: "max-width: min(340px, calc(100vw - 32px)); white-space: normal; overflow-wrap: anywhere;",
      formatter: (p: unknown, ticket: string, callback?: (ticket: string, html: string) => void) => {
        const index = hitIndex(p);
        if (index == null || !groups[index]) return "";
        const text = pointText(groups[index], axis, labels[index], metric, false).map(escapeHtml).join("<br/>");
        return sample && callback ? sample(groups[index].key, text, ticket, callback) : text;
      },
    },
    xAxis: {
      type: "category",
      data: labels,
      axisLabel: { fontSize: 11, color: CHART_COLORS.text2, hideOverlap: true },
      axisTick: { alignWithLabel: true },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        fontSize: 11, color: CHART_COLORS.text2,
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

/** 排行横条 option：按传入顺序（调用方依当前指标排序）；null 项剔除并在组件副标题注明。 */
export function buildPurchaseRankOption(
  items: { label: string; group: PurchaseAnalyticsGroup }[],
  metric: PurchaseChartMetric,
  axis = "分组",
  sample?: SampleTooltip,
): ECOption {
  const present = items.filter((i) => valueOf(i.group, metric) !== null);
  return {
    grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
    xAxis: {
      type: "value",
      axisLabel: {
        fontSize: 11, color: CHART_COLORS.text2,
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
      trigger: "axis",
      axisPointer: { type: "shadow" },
      confine: true,
      extraCssText: "max-width: min(340px, calc(100vw - 32px)); white-space: normal; overflow-wrap: anywhere;",
      formatter: (p: unknown, ticket: string, callback?: (ticket: string, html: string) => void) => {
        const index = hitIndex(p), hit = index == null ? undefined : present[index];
        if (!hit) return "";
        const text = pointText(hit.group, axis, hit.label, metric, true).map(escapeHtml).join("<br/>");
        return sample && callback ? sample(hit.group.key, text, ticket, callback) : text;
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
  groups, metric, loading, onBucketClick, axis = "采购日期", sampleContext,
}: {
  groups: PurchaseAnalyticsGroup[];
  metric: PurchaseChartMetric;
  axis?: string;
  loading?: boolean;
  sampleContext?: PurchaseChartSampleContext;
  /** 点某根柱 → 下钻到该日 / 月的订单列表（与表格行点击同一 drill 语义）。 */
  onBucketClick?: (key: string) => void;
}) {
  const sorted = useMemo(() => sortTrendGroups(groups), [groups]);
  const sample = usePurchaseChartSample(sampleContext);
  const option = useMemo(() => buildPurchaseTrendOption(sorted, metric, axis, sample), [sorted, metric, axis, sample]);
  const click = usePointClick(onBucketClick);
  const points = useMemo(() => sorted.flatMap((g, i) => valueOf(g, metric) == null ? [] : [{
    dataIndex: i, label: pointText(g, axis, g.key === PURCHASE_EMPTY_KEY ? "无日期" : g.key, metric, false).join("，")
  }]), [sorted, metric, axis]);
  return (
    <EChartContainer
      option={option}
      loading={loading}
      empty={sorted.length === 0}
      emptyText="当前筛选下没有趋势数据"
      height={260}
      ariaLabel={`采购趋势：${PURCHASE_METRIC_LABELS[metric]}`}
      keyboardPoints={points}
      onKeyboardActivate={i => onBucketClick?.(sorted[i].key)}
      onEvents={{
        click: (params) => {
          const dataIndex = (params as { dataIndex?: number }).dataIndex;
          if (dataIndex == null || !onBucketClick) return;
          click(params, sorted[dataIndex]);
        },
      }}
    />
  );
}

export function PurchaseRankBar({
  groups, metric, loading, labelOf, onItemClick, axis = "分组", sampleContext,
}: {
  groups: PurchaseAnalyticsGroup[];
  metric: PurchaseChartMetric;
  axis?: string;
  loading?: boolean;
  sampleContext?: PurchaseChartSampleContext;
  /** 分组行 → 显示名（与表格同标签：客户名 / label / key）。 */
  labelOf: (g: PurchaseAnalyticsGroup) => string;
  /** 点某条 → 下钻该组（与表格行点击同一 drill 语义，回传分组键）。 */
  onItemClick?: (key: string) => void;
}) {
  const items = useMemo(
    () => groups.map((g) => ({ label: labelOf(g), group: g })).sort((a, b) =>
      (valueOf(b.group, metric) ?? -Infinity) - (valueOf(a.group, metric) ?? -Infinity)),
    [groups, labelOf, metric],
  );
  const sample = usePurchaseChartSample(sampleContext);
  const option = useMemo(() => buildPurchaseRankOption(items, metric, axis, sample), [items, metric, axis, sample]);
  const present = useMemo(() => items.filter(i => valueOf(i.group, metric) !== null), [items, metric]);
  const points = useMemo(() => present.map(({ group, label }, i) => ({
    dataIndex: i, label: pointText(group, axis, label, metric, true).join("，")
  })), [present, metric, axis]);
  const click = usePointClick(onItemClick);
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
        keyboardPoints={points}
        onKeyboardActivate={i => onItemClick?.(present[i].group.key)}
        onEvents={{
          click: params => {
            const i = hitIndex(params);
            if (i !== undefined) click(params, present[i]?.group);
          },
        }}
      />
      {missing > 0 && (
        <div style={{ fontSize: 12, color: CHART_COLORS.text2 }}>
          另有 {missing} 组该指标无数据，未画出（见下表）
        </div>
      )}
    </div>
  );
}
