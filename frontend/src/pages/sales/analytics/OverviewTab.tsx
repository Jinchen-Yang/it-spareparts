// 概览页签（D-44 方案 §3.2）：迷你趋势 + 客户 / 销售人员 / 业务类型三个 Top 5 小榜
// （按页面首个已显示金额口径降序；金额不可见时后端按单数）。“更多”跳分组排行。
// 无客户权限时不出客户榜（后端 403）。KPI 卡在页面顶部常驻，不在本页签重复。
import { Alert, Button, Card, List, Space } from "antd";
import {
  fetchSalesAnalyticsGroups,
  fetchSalesAnalyticsTrend,
  type SalesAnalyticsFilters,
  type SalesGroupDim,
  type SalesGroupSort,
  type SalesGroupsResp,
  type SalesTrendResp,
} from "../../../api/salesAnalytics";
import { SalesTrendChart, type SalesChartMetric } from "../../../components/charts/SalesDashboardCharts";
import { pct } from "../../../utils/format";
import { fmtMoney } from "../shared";
import { drillPatch, groupRowLabel, GROUP_DIM_LABELS } from "./GroupsTab";
import { recommendGranularity } from "./TrendTab";
import { useAsyncData } from "./useAsyncData";
import { AMOUNT_SORT_KEY, type SalesAnalyticsUrlState } from "./urlState";

interface OverviewTabProps {
  state: SalesAnalyticsUrlState;
  apiFilters: SalesAnalyticsFilters;
  filterKey: string;
  patch: (next: Partial<SalesAnalyticsUrlState>, opts?: { history?: "push" | "replace" }) => void;
  costRestricted: boolean;
  customerRestricted: boolean;
  toMessage: (err: unknown) => string;
  /** 所属会话 owner（页级下传，DL-010 返工）：失效时请求前拒发并清旧数据。 */
  ownerToken: string | null;
  /** 页级过期通知（DL-010 落地清理）：检测到 owner 失效的迟到响应通知页级清旧上下文。 */
  onOwnerStale: () => void;
}

function TopList({ dim, sort, state, apiFilters, filterKey, patch, toMessage, ownerToken, onOwnerStale }: {
  dim: SalesGroupDim; sort: SalesGroupSort;
} & Omit<OverviewTabProps, "costRestricted" | "customerRestricted">) {
  const { data, loading, error, retry } = useAsyncData<SalesGroupsResp>(
    () => fetchSalesAnalyticsGroups({ ...apiFilters, group_by: dim, sort, order: "desc", top: 5 }),
    [filterKey, dim, sort],
    toMessage,
    true,
    ownerToken,
    onOwnerStale,
  );
  const valueKey = (data?.sort ?? sort) as keyof SalesGroupsResp["groups"][number];
  return (
    <Card
      size="small"
      title={`${GROUP_DIM_LABELS[dim]} Top 5`}
      style={{ flex: "1 1 280px", minWidth: 260 }}
      extra={<Button type="link" size="small" onClick={() => patch({ tab: "groups", groupBy: dim })}>更多</Button>}
    >
      {error ? (
        <Alert type="error" showIcon message={error} action={<Button type="link" size="small" onClick={retry}>重试</Button>} />
      ) : (
        <List
          size="small"
          loading={loading}
          dataSource={data?.groups ?? []}
          locale={{ emptyText: "无数据" }}
          renderItem={(g, i) => {
            const drill = drillPatch(state, dim, g);
            const v = g[valueKey] as number | null;
            // 长组名（客户 / 销售）截断省略，320/390px 窄屏不撑破文档流；缺失组
            // 显示统一标签（「未分类」/「（未填）」）
            const label = groupRowLabel(dim, g);
            return (
              <List.Item style={{ paddingInline: 0 }}>
                <span style={{ display: "flex", gap: 6, minWidth: 0, flex: "1 1 auto", overflow: "hidden" }}>
                  {/* DL-025 稳态返工：序号原 text-3 在嵌套卡 inset 底（rgb(246,243,238)）
                      实测 4.32:1，改 text-2（inset 5.15 / 白 5.69） */}
                  <span style={{ color: "var(--mb-text-2)", width: 16, flexShrink: 0 }}>{i + 1}</span>
                  {drill ? (
                    <Button type="link" size="small" title={label}
                      style={{
                        padding: 0, height: "auto", maxWidth: "100%",
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}
                      onClick={() => patch(drill, { history: "push" })}>{label}</Button>
                  ) : (
                    <span title={label}
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
                  )}
                </span>
                <span style={{ whiteSpace: "nowrap", flexShrink: 0 }}>
                  {valueKey === "orders" ? `${v ?? "—"} 单` : fmtMoney(v)}
                  {/* 占比同序号：text-3 在 inset 底 4.32:1，改 text-2（DL-025 稳态） */}
                  <span style={{ marginLeft: 6, fontSize: 12, color: "var(--mb-text-2)" }}>{pct(g.share)}</span>
                </span>
              </List.Item>
            );
          }}
        />
      )}
    </Card>
  );
}

export default function OverviewTab(props: OverviewTabProps) {
  const { state, apiFilters, filterKey, patch, costRestricted, customerRestricted, toMessage, ownerToken, onOwnerStale } = props;
  const firstAmount = AMOUNT_SORT_KEY[state.amounts[0]];
  const sort: SalesGroupSort = costRestricted && firstAmount !== "revenue" ? "revenue" : firstAmount as SalesGroupSort;
  const granularity = recommendGranularity(apiFilters.date_from, apiFilters.date_to);
  const trend = useAsyncData<SalesTrendResp>(
    () => fetchSalesAnalyticsTrend(apiFilters, granularity, state.compare),
    [filterKey, granularity, state.compare],
    toMessage,
    true,
    ownerToken,
    onOwnerStale,
  );
  const dims: SalesGroupDim[] = [
    ...(customerRestricted ? [] : ["customer" as const]), "salesperson", "business_type",
  ];
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Card size="small" title="销售趋势" extra={<Button type="link" size="small" onClick={() => patch({ tab: "trend" })}>详细趋势</Button>}>
        {trend.error ? (
          <Alert type="error" showIcon message={trend.error} />
        ) : (
          <SalesTrendChart
            buckets={trend.data?.buckets ?? []}
            compare={trend.data?.compare ?? null}
            metric={sort as SalesChartMetric}
            granularity={granularity}
            loading={trend.loading}
            height={220}
          />
        )}
      </Card>
      <Space style={{ display: "flex", flexWrap: "wrap" }} align="start" size={12}>
        {dims.map((dim) => (
          <TopList key={dim} dim={dim} sort={sort} state={state} apiFilters={apiFilters}
            filterKey={filterKey} patch={patch} toMessage={toMessage} ownerToken={ownerToken} onOwnerStale={onOwnerStale} />
        ))}
      </Space>
    </div>
  );
}
