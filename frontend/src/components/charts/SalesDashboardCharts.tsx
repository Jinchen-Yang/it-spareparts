import { useMemo } from "react";
import dayjs from "dayjs";
import EChartContainer from "./EChartContainer";
import type { ECOption } from "./echartsCore";
import { CHART_COLORS } from "./chartTheme";
import { escapeHtml, moneyAxis, moneyExact } from "../../utils/format";

/**
 * 销售看板图表（D-44）：分组排行横条图 + 时间趋势柱图（可叠上月同期折线）+ 星期分布。
 * 约定（charts/README）：option 必须 useMemo；null ≠ 0（null 剔除 / 断点，绝不画 0）；
 * 颜色只从 CHART_COLORS 取；单图单纵轴（金额 / 单数切换，不做双轴）；负值换色并带
 * 负号文字与 0 轴参考线；金额文案写「金额合计」。
 */

export type SalesChartMetric = "amount_ex_tax" | "amount_inc_tax" | "revenue" | "gross_profit" | "orders";

export const SALES_METRIC_LABELS: Record<SalesChartMetric, string> = {
  amount_ex_tax: "不含税金额合计",
  amount_inc_tax: "含税订单金额合计",
  revenue: "营收合计",
  gross_profit: "毛利合计",
  orders: "销售单数",
};

const isMoney = (m: SalesChartMetric) => m !== "orders";
const fmtValue = (m: SalesChartMetric, v: number | null | undefined) =>
  v == null ? "—" : isMoney(m) ? moneyExact(v) : `${v} 单`;

type MetricRow = Partial<Record<SalesChartMetric, number | null>>;

function valueOf(row: MetricRow, metric: SalesChartMetric): number | null {
  const v = row[metric];
  return v == null || !Number.isFinite(Number(v)) ? null : Number(v);
}

// ---------------------------------------------------------------- 分组排行横条

export interface GroupBarItem extends MetricRow {
  key: string;
}

/** 横条图 option：按传入顺序自上而下（表格已排好序）；null 项剔除并在副标题注明。 */
export function buildGroupBarOption(items: GroupBarItem[], metric: SalesChartMetric): ECOption {
  const present = items.filter((i) => valueOf(i, metric) !== null);
  const labels = present.map((i) => i.key);
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
      data: labels,
      axisLabel: {
        fontSize: 12, color: CHART_COLORS.text2, width: 140, overflow: "truncate",
      },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
    },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (p: unknown) => {
        const { name, value } = p as { name: string; value: number };
        return `<b>${escapeHtml(name)}</b><br/>${SALES_METRIC_LABELS[metric]}：${fmtValue(metric, value)}`;
      },
    },
    series: [{
      type: "bar",
      name: SALES_METRIC_LABELS[metric],
      barMaxWidth: 18,
      data: present.map((i) => {
        const v = valueOf(i, metric) as number;
        return {
          value: v,
          itemStyle: {
            color: v < 0 ? CHART_COLORS.profitNegative : CHART_COLORS.sales,
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
      markLine: present.some((i) => (valueOf(i, metric) as number) < 0) ? {
        silent: true, symbol: "none",
        lineStyle: { color: CHART_COLORS.crosshair, type: "solid" },
        data: [{ xAxis: 0 }],
        label: { show: false },
      } : undefined,
    }],
  } as ECOption;
}

export function GroupRankBar({
  items,
  metric,
  loading,
  onItemClick,
}: {
  items: GroupBarItem[];
  metric: SalesChartMetric;
  loading?: boolean;
  onItemClick?: (key: string) => void;
}) {
  const option = useMemo(() => buildGroupBarOption(items, metric), [items, metric]);
  const missing = items.filter((i) => valueOf(i, metric) === null).length;
  const height = Math.max(160, Math.min(items.length, 30) * 26 + 24);
  return (
    <div>
      <EChartContainer
        option={option}
        loading={loading}
        empty={items.length - missing === 0}
        emptyText="当前筛选下没有可画的数据"
        height={height}
        ariaLabel={`分组排行：${SALES_METRIC_LABELS[metric]}`}
        onEvents={{
          click: (params) => {
            const name = (params as { name?: string }).name;
            if (name && onItemClick) onItemClick(name);
          },
        }}
      />
      {missing > 0 && (
        <div style={{ fontSize: 12, color: CHART_COLORS.axisLabel }}>另有 {missing} 组该指标无数据，未画出（见下表）</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 趋势

export interface TrendPoint extends MetricRow {
  bucket: string;
}

export type TrendGranularity = "day" | "week" | "month";

export function formatTrendBucket(bucket: string, g: TrendGranularity): string {
  const d = dayjs(bucket);
  if (!d.isValid()) return bucket;
  if (g === "month") return d.format("YYYY-MM");
  return g === "week" ? `${d.format("MM-DD")} 周` : d.format("MM-DD");
}

/** 趋势 option：本期柱 + 上月同期虚线（按位置对齐，第 1 桶对第 1 桶；同一纵轴）。 */
export function buildTrendOption(
  buckets: TrendPoint[],
  compare: TrendPoint[] | null,
  metric: SalesChartMetric,
  granularity: TrendGranularity,
): ECOption {
  const label = SALES_METRIC_LABELS[metric];
  const hasCompare = !!compare && compare.length > 0;
  return {
    grid: { left: 8, right: 16, top: hasCompare ? 32 : 12, bottom: 8, containLabel: true },
    legend: hasCompare ? {
      top: 0, left: 0, icon: "roundRect", itemWidth: 14, itemHeight: 8,
      data: ["本期", "上月同期"],
    } : undefined,
    xAxis: {
      type: "category",
      data: buckets.map((b) => formatTrendBucket(b.bucket, granularity)),
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel, hideOverlap: true },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        fontSize: 11, color: CHART_COLORS.axisLabel,
        formatter: (v: number) => (isMoney(metric) ? moneyAxis(v) : String(v)),
      },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: "shadow", shadowStyle: { color: CHART_COLORS.selectionBg } },
      formatter: (params: unknown) => {
        const list = params as { dataIndex: number }[] | undefined;
        const i = list?.[0]?.dataIndex;
        if (typeof i !== "number") return "";
        const cur = buckets[i];
        const prev = compare?.[i];
        const lines = [
          `<b>${escapeHtml(formatTrendBucket(cur.bucket, granularity))}</b>`,
          `${label}：${fmtValue(metric, valueOf(cur, metric))}`,
        ];
        if (prev) {
          lines.push(`上月同期（${escapeHtml(formatTrendBucket(prev.bucket, granularity))}）：${fmtValue(metric, valueOf(prev, metric))}`);
        }
        return lines.join("<br/>");
      },
    },
    series: [
      {
        type: "bar",
        name: "本期",
        barMaxWidth: 28,
        itemStyle: { color: CHART_COLORS.sales, borderRadius: [4, 4, 0, 0] },
        data: buckets.map((b) => valueOf(b, metric)),
      },
      ...(hasCompare ? [{
        type: "line" as const,
        name: "上月同期",
        symbol: "circle",
        symbolSize: 8,
        lineStyle: { width: 2, type: "dashed" as const, color: CHART_COLORS.crosshair },
        itemStyle: { color: CHART_COLORS.crosshair },
        connectNulls: false,
        data: buckets.map((_b, i) => (compare && compare[i] ? valueOf(compare[i], metric) : null)),
      }] : []),
    ],
  } as ECOption;
}

export function SalesTrendChart({
  buckets,
  compare,
  metric,
  granularity,
  loading,
  height = 320,
  onBucketClick,
}: {
  buckets: TrendPoint[];
  compare: TrendPoint[] | null;
  metric: SalesChartMetric;
  granularity: TrendGranularity;
  loading?: boolean;
  height?: number;
  onBucketClick?: (bucket: string) => void;
}) {
  const option = useMemo(
    () => buildTrendOption(buckets, compare, metric, granularity),
    [buckets, compare, metric, granularity],
  );
  return (
    <EChartContainer
      option={option}
      loading={loading}
      empty={buckets.length === 0}
      emptyText="所选区间没有销售单"
      height={height}
      ariaLabel={`销售趋势：${SALES_METRIC_LABELS[metric]}`}
      onEvents={{
        click: (params) => {
          const i = (params as { dataIndex?: number; seriesName?: string }).dataIndex;
          if (typeof i === "number" && buckets[i] && onBucketClick) onBucketClick(buckets[i].bucket);
        },
      }}
    />
  );
}

const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export interface WeekdayPoint extends MetricRow {
  isodow: number;
}

export function buildWeekdayOption(points: WeekdayPoint[], metric: SalesChartMetric): ECOption {
  const byDow = new Map(points.map((p) => [p.isodow, p]));
  return {
    grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
    xAxis: {
      type: "category", data: WEEKDAY_LABELS,
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel },
      axisTick: { show: false },
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
    tooltip: {
      trigger: "item", confine: true,
      formatter: (p: unknown) => {
        const { name, value } = p as { name: string; value: number | null };
        return `<b>${escapeHtml(name)}</b><br/>${SALES_METRIC_LABELS[metric]}：${fmtValue(metric, value)}`;
      },
    },
    series: [{
      type: "bar",
      name: SALES_METRIC_LABELS[metric],
      barMaxWidth: 28,
      itemStyle: { color: CHART_COLORS.sales, borderRadius: [4, 4, 0, 0] },
      data: [1, 2, 3, 4, 5, 6, 7].map((d) => {
        const p = byDow.get(d);
        return p ? valueOf(p, metric) : null;
      }),
    }],
  } as ECOption;
}

export function WeekdayChart({ points, metric, loading }: {
  points: WeekdayPoint[]; metric: SalesChartMetric; loading?: boolean;
}) {
  const option = useMemo(() => buildWeekdayOption(points, metric), [points, metric]);
  return (
    <EChartContainer
      option={option}
      loading={loading}
      empty={points.every((p) => !p.orders)}
      emptyText="所选区间没有销售单"
      height={200}
      ariaLabel={`星期分布：${SALES_METRIC_LABELS[metric]}`}
    />
  );
}
