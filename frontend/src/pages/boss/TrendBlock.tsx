/**
 * 专业经营趋势：BusinessTrendChart（十字指针/精确 tooltip/图例开关/时间缩放/负毛利分段变色，
 * smooth=false 不扭曲数据）。点击某期 → 联动订单块日期筛选（od_from/od_to 写入 URL）。
 */
import { useMemo } from "react";
import { Alert, Button, Card, Segmented, Tag } from "antd";
import { dashboardTrend, type TrendPoint } from "../../api";
import BusinessTrendChart, { type BusinessTrendPoint } from "../../components/charts/BusinessTrendChart";
import { MUTED, drillRangeOf, useGuardedFetch, type BoardFilters, type DateRange } from "./shared";

interface TrendBlockProps {
  filters: BoardFilters;
  dateRange: DateRange;
  patch: (next: Record<string, string | number | null | undefined>, opts?: { replace?: boolean }) => void;
}

export default function TrendBlock({ filters, dateRange, patch }: TrendBlockProps) {
  const { granularity } = filters;
  const { data, loading, error, reload } = useGuardedFetch<{ granularity: string; series: TrendPoint[] }>(
    () => dashboardTrend({ ...dateRange, granularity }),
    [dateRange, granularity]);

  // 后端 TrendPoint 与图表 BusinessTrendPoint 字段同名；毛利仅累计已配成本行（口径已在 KPI 揭示）
  const points: BusinessTrendPoint[] = useMemo(
    () => (data?.series ?? []).map((p) => ({
      period: p.period,
      sales_ex_tax: p.sales_ex_tax,
      purchase_ex_tax: p.purchase_ex_tax,
      gross_profit: p.gross_profit,
    })), [data]);

  const drillActive = !!(filters.drillFrom && filters.drillTo);
  const scopeBits = [filters.partId && "PN", filters.poolId && "池", (filters.purchaser || filters.salesperson) && "人员"]
    .filter(Boolean);

  return (
    <Card size="small" style={{ marginBottom: 16 }}
      title="经营趋势（销售额 / 采购额 / 毛利，未税）"
      extra={
        <Segmented size="small" value={granularity}
          aria-label="趋势粒度"
          onChange={(v) => patch({ gran: v as string }, { replace: true })}
          options={[{ label: "日", value: "day" }, { label: "周", value: "week" }, { label: "月", value: "month" }]} />
      }>
      <div style={{ ...MUTED, marginBottom: 6 }}>
        统计范围：{dateRange.date_from} ~ {dateRange.date_to} · 仅受时间筛选影响
        {scopeBits.length > 0 && `（${scopeBits.join("/")}筛选不作用于本图）`}
        · 点击图中某一期可联动下方订单
      </div>
      {drillActive && (
        <div style={{ marginBottom: 6 }}>
          <Tag color="blue" closable
            onClose={(e) => { e.preventDefault(); patch({ od_from: null, od_to: null }); }}
            aria-label={`订单已按趋势选中期筛选 ${filters.drillFrom} 至 ${filters.drillTo}，点关闭恢复`}>
            订单块已按选中期筛选：{filters.drillFrom} ~ {filters.drillTo}（点 × 恢复）
          </Tag>
        </div>
      )}
      {error ? (
        <Alert type="error" showIcon message={`趋势加载失败：${error}`}
          action={<Button size="small" onClick={reload}>重试</Button>} />
      ) : (
        <BusinessTrendChart
          data={points}
          granularity={granularity}
          loading={loading}
          onPointClick={(period) => {
            const r = drillRangeOf(period, granularity);
            patch({ od_from: r.from, od_to: r.to });
          }} />
      )}
    </Card>
  );
}
