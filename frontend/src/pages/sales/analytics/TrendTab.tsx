// 趋势页签（D-44 方案 §3.4）：日 / 周 / 月完整桶序列（无成交的桶为 0），可叠上月同期
// （按位置对齐）；指标在金额 / 单数 / 毛利间切换——单图单纵轴，不做双轴。附星期分布。
// 点某根柱 = 日期收窄到该桶并切到订单明细。粒度默认按区间长度推荐（≤31 天按日、
// ≤半年按周、否则按月），可手动切换并落 URL。
import { Alert, Button, Radio, Space } from "antd";
import dayjs from "dayjs";
import {
  fetchSalesAnalyticsTrend,
  type SalesAnalyticsFilters,
  type SalesTrendGranularity,
  type SalesTrendResp,
} from "../../../api/salesAnalytics";
import {
  SALES_METRIC_LABELS,
  SalesTrendChart,
  WeekdayChart,
  type SalesChartMetric,
} from "../../../components/charts/SalesDashboardCharts";
import { useAsyncData } from "./useAsyncData";
import { AMOUNT_SORT_KEY, type SalesAnalyticsUrlState } from "./urlState";

/** 按区间天数推荐粒度；区间未知（全部）按月。 */
export function recommendGranularity(from?: string, to?: string): SalesTrendGranularity {
  if (!from || !to) return "month";
  const days = dayjs(to).diff(dayjs(from), "day") + 1;
  if (days <= 31) return "day";
  if (days <= 183) return "week";
  return "month";
}

/** 桶 → 下钻日期区间（闭区间）。 */
export function bucketRange(bucket: string, g: SalesTrendGranularity): { from: string; to: string } {
  const d = dayjs(bucket);
  const end = g === "day" ? d : g === "week" ? d.add(6, "day") : d.endOf("month");
  return { from: d.format("YYYY-MM-DD"), to: end.format("YYYY-MM-DD") };
}

export interface TrendTabProps {
  state: SalesAnalyticsUrlState;
  apiFilters: SalesAnalyticsFilters;
  filterKey: string;
  patch: (next: Partial<SalesAnalyticsUrlState>, opts?: { history?: "push" | "replace" }) => void;
  costRestricted: boolean;
  profitRestricted: boolean;
  toMessage: (err: unknown) => string;
  metric: SalesChartMetric;
  onMetricChange: (m: SalesChartMetric) => void;
}

export default function TrendTab({
  state, apiFilters, filterKey, patch, costRestricted, profitRestricted, toMessage, metric, onMetricChange,
}: TrendTabProps) {
  const granularity = state.granularity ?? recommendGranularity(apiFilters.date_from, apiFilters.date_to);
  const { data, loading, error, retry } = useAsyncData<SalesTrendResp>(
    () => fetchSalesAnalyticsTrend(apiFilters, granularity, state.compare),
    [filterKey, granularity, state.compare],
    toMessage,
  );
  const metricOptions: SalesChartMetric[] = [
    ...state.amounts.map((a) => AMOUNT_SORT_KEY[a] as SalesChartMetric)
      .filter((m) => m === "revenue" || !costRestricted),
    "orders",
    ...(!profitRestricted ? ["gross_profit" as const] : []),
  ];
  const activeMetric = metricOptions.includes(metric) ? metric : metricOptions[0];
  // 星期分布没有毛利维度，选毛利时按单数展示
  const weekdayMetric: SalesChartMetric = activeMetric === "gross_profit" ? "orders" : activeMetric;

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Space wrap>
        <Radio.Group
          aria-label="粒度"
          optionType="button"
          size="small"
          value={granularity}
          options={[{ value: "day", label: "按日" }, { value: "week", label: "按周" }, { value: "month", label: "按月" }]}
          onChange={(e) => patch({ granularity: e.target.value })}
        />
        <Radio.Group
          aria-label="趋势指标"
          optionType="button"
          size="small"
          value={activeMetric}
          options={metricOptions.map((m) => ({ value: m, label: SALES_METRIC_LABELS[m] }))}
          onChange={(e) => onMetricChange(e.target.value)}
        />
        {data?.compare_window && (
          <span style={{ fontSize: 12, color: "var(--mb-text-3)" }}>
            虚线为上月同期 {data.compare_window.date_from} ~ {data.compare_window.date_to}（按位置对齐）
          </span>
        )}
        {data && state.compare && !data.compare_available && (
          <span style={{ fontSize: 12, color: "var(--mb-text-3)" }}>区间长于一个月，不提供上月对比</span>
        )}
      </Space>
      {error ? (
        <Alert type="error" showIcon message="趋势加载失败" description={error}
          action={<Button type="link" size="small" onClick={retry}>重试</Button>} />
      ) : (
        <>
          <SalesTrendChart
            buckets={data?.buckets ?? []}
            compare={data?.compare ?? null}
            metric={activeMetric}
            granularity={granularity}
            loading={loading}
            onBucketClick={(bucket) => {
              const { from, to } = bucketRange(bucket, granularity);
              patch({ preset: "custom", from, to, tab: "orders", page: 1 }, { history: "push" });
            }}
          />
          <div>
            <div style={{ fontSize: 13, marginBottom: 4 }}>星期分布（{SALES_METRIC_LABELS[weekdayMetric]}）</div>
            <WeekdayChart points={data?.weekday ?? []} metric={weekdayMetric} loading={loading} />
          </div>
        </>
      )}
    </div>
  );
}
