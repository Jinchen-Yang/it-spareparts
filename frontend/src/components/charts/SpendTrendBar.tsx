import { useMemo } from "react";
import { Typography } from "antd";
import dayjs from "dayjs";
import EChartContainer from "./EChartContainer";
import type { ECOption } from "./echartsCore";
import { CHART_COLORS } from "./chartTheme";
import { BOARD_BUSINESS_TYPE_LABELS } from "../../api/maintenanceBossBoard";
import type {
  SpendBusinessTypeCode,
  SpendTrendBucket,
  SpendTrendGranularity,
} from "../../api/maintenanceAnalytics";
import { escapeHtml, moneyAxis, moneyExact } from "../../utils/format";

/**
 * 开支统计堆叠柱状图（v1.34 spend-trend）。
 * 约定（charts/README）：option 必须 useMemo；null ≠ 0（restricted/not_imported/
 * 空值一律 null，不进堆叠，绝不画 0）；颜色只从 CHART_COLORS 取；金额文案写
 * 「金额合计」；生命周期/三态交给 EChartContainer。
 * 超过 40 期只画最近 40 期（完整数据在页面下方按期明细表），并在图题旁注明。
 */

export const SPEND_TREND_MAX_BUCKETS = 40;

/** 堆叠顺序 = 图例顺序 = 汇总表分类顺序（契约顺序）。 */
export const SPEND_CATEGORY_CODES: SpendBusinessTypeCode[] = [
  "overall", "spare", "computing", "refit", "other", "unlabeled", "unassigned",
];

export const SPEND_CATEGORY_LABELS: Record<SpendBusinessTypeCode, string> = {
  ...BOARD_BUSINESS_TYPE_LABELS,
  unassigned: "未归属",
};

export const SPEND_GRANULARITY_LABELS: Record<SpendTrendGranularity, string> = {
  day: "按天",
  week: "按周",
  month: "按月",
  year: "按年",
};

/**
 * 色随实体固定分配：六个业务档直接用 CHART_COLORS 的系列色，未归属（残差桶）
 * 用同一灰 token 的降透明度派生——调色板只有六个可达色相，够用且不新增色相。
 */
function withAlpha(hex: string, alpha: number): string {
  const body = hex.replace("#", "");
  const full = body.length === 3 ? body.split("").map((c) => c + c).join("") : body;
  const n = parseInt(full, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export const SPEND_CATEGORY_COLORS: Record<SpendBusinessTypeCode, string> = {
  overall: CHART_COLORS.sales,
  spare: CHART_COLORS.purchase,
  computing: CHART_COLORS.profit,
  refit: CHART_COLORS.emphasis,
  other: CHART_COLORS.profitNegative,
  unlabeled: CHART_COLORS.crosshair,
  unassigned: withAlpha(CHART_COLORS.crosshair, 0.4),
};

/** 信封 → 图表数值：仅 ready 且非空可入图；其余状态 null（缺失 ≠ 0）。 */
export function spendValue(stat: { state?: string; value?: unknown } | undefined): number | null {
  if (!stat || stat.state !== "ready") return null;
  if (stat.value === null || stat.value === undefined || stat.value === "") return null;
  const n = Number(stat.value);
  return Number.isFinite(n) ? n : null;
}

/** 信封 → tooltip 文案：与页面 statMoney 同三态语义（图表不 import 页面 helper）。 */
export function spendMoneyText(stat: { state?: string; value?: unknown } | undefined): string {
  if (!stat) return "—";
  if (stat.state === "restricted") return "🔒 无权限";
  if (stat.state === "not_imported") return "尚未导入";
  if (stat.state === "error") return "暂不可用";
  const value = spendValue(stat);
  return value === null ? "—" : moneyExact(value);
}

/** 桶起点日期（YYYY-MM-DD）→ 粒度可读标签：日 MM-DD / 周 MM-DD 周 / 月 YYYY-MM / 年 YYYY。 */
export function formatSpendBucket(bucket: string, granularity: SpendTrendGranularity): string {
  const d = dayjs(bucket);
  if (!d.isValid()) return bucket;
  if (granularity === "month") return d.format("YYYY-MM");
  if (granularity === "year") return d.format("YYYY");
  const md = d.format("MM-DD");
  return granularity === "week" ? `${md} 周` : md;
}

/** 超过 40 期取最新 N 期；返回可见桶与被隐藏的期数（供页面注明）。 */
export function visibleSpendBuckets(
  buckets: SpendTrendBucket[],
  limit = SPEND_TREND_MAX_BUCKETS,
): { visible: SpendTrendBucket[]; hiddenCount: number } {
  if (buckets.length <= limit) return { visible: buckets, hiddenCount: 0 };
  return { visible: buckets.slice(buckets.length - limit), hiddenCount: buckets.length - limit };
}

/** axis tooltip：期标签 + 七分类精确金额（受限/未导入显式标注）+ 金额合计（含税）。 */
export function formatSpendTrendTooltip(
  bucket: SpendTrendBucket | undefined,
  granularity: SpendTrendGranularity,
): string {
  if (!bucket) return "";
  const lines = SPEND_CATEGORY_CODES.map((code) =>
    `${SPEND_CATEGORY_LABELS[code]}：${spendMoneyText(bucket.by_business_type?.[code])}`);
  return [
    `<b>${escapeHtml(formatSpendBucket(bucket.bucket, granularity))}</b>`,
    ...lines,
    `金额合计（含税）：${spendMoneyText(bucket.cost_inc)}`,
  ].join("<br/>");
}

export function buildSpendTrendBarOption(
  buckets: SpendTrendBucket[],
  granularity: SpendTrendGranularity,
  maxBuckets = SPEND_TREND_MAX_BUCKETS,
): ECOption {
  const { visible } = visibleSpendBuckets(buckets, maxBuckets);
  return {
    grid: { left: 8, right: 16, top: 44, bottom: 8, containLabel: true },
    legend: {
      type: "scroll",
      top: 0,
      left: 0,
      icon: "roundRect",
      itemWidth: 14,
      itemHeight: 8,
      data: SPEND_CATEGORY_CODES.map((code) => SPEND_CATEGORY_LABELS[code]),
    },
    xAxis: {
      type: "category",
      data: visible.map((b) => formatSpendBucket(b.bucket, granularity)),
      axisLabel: { fontSize: 11, color: CHART_COLORS.axisLabel, hideOverlap: true },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        fontSize: 11,
        color: CHART_COLORS.axisLabel,
        formatter: (v: number) => moneyAxis(v),
      },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: "shadow", shadowStyle: { color: CHART_COLORS.selectionBg } },
      formatter: (params: unknown) => {
        const list = params as { dataIndex: number }[] | undefined;
        const index = list?.[0]?.dataIndex;
        return formatSpendTrendTooltip(
          typeof index === "number" ? visible[index] : undefined,
          granularity,
        );
      },
    },
    series: SPEND_CATEGORY_CODES.map((code) => ({
      name: SPEND_CATEGORY_LABELS[code],
      type: "bar",
      stack: "spend",
      barMaxWidth: 32,
      itemStyle: { color: SPEND_CATEGORY_COLORS[code] },
      emphasis: { focus: "series" },
      data: visible.map((b) => spendValue(b.by_business_type?.[code])),
    })),
  } as ECOption;
}

export function SpendTrendBar({
  buckets,
  granularity,
  loading = false,
  error = null,
  height = 360,
  testId,
}: {
  buckets: SpendTrendBucket[];
  granularity: SpendTrendGranularity;
  loading?: boolean;
  error?: string | null;
  height?: number;
  testId?: string;
}) {
  const option = useMemo(
    () => buildSpendTrendBarOption(buckets, granularity),
    [buckets, granularity],
  );
  const { visible, hiddenCount } = useMemo(() => visibleSpendBuckets(buckets), [buckets]);
  const hasData = visible.some((b) =>
    SPEND_CATEGORY_CODES.some((code) => spendValue(b.by_business_type?.[code]) !== null));
  return (
    <div data-testid={testId}>
      <Typography.Title level={5} style={{ marginTop: 0, marginBottom: 4 }}>
        各期开支金额合计（含税）
      </Typography.Title>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {SPEND_GRANULARITY_LABELS[granularity]}分桶
        {hiddenCount > 0
          ? ` · 共 ${buckets.length} 期，仅展示最近 ${SPEND_TREND_MAX_BUCKETS} 期（更早 ${hiddenCount} 期见下方按期明细）`
          : " · 悬停看精确值与分类拆分"}
      </Typography.Text>
      <EChartContainer
        option={option}
        loading={loading}
        error={error}
        empty={!hasData}
        emptyText="当前窗口没有可展示的开支数据"
        height={height}
        ariaLabel="各期开支金额合计（含税）"
      />
    </div>
  );
}

export default SpendTrendBar;
