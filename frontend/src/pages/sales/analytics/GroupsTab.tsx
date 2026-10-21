// 分组排行页签（D-44 方案 §3.3）：按客户 / 客户类型 / 城市 / 来源 / 销售 / 业务类型 /
// 仓库 / 商品大类 / 品牌 / PN 分组；任一指标列升序 / 降序（以响应回显为准：被遮蔽的
// 金额 / 毛利排序后端退回单数）；Top N + “其他”（合计 = 总计，不丢钱）；集中度；
// 上月同期增减。点某组 = 把该值加进筛选并切到订单明细（只对有筛选参数的维度）。
import { Alert, Button, Select, Space, Table, Tooltip } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { FilterValue, SorterResult } from "antd/es/table/interface";
import {
  fetchSalesAnalyticsGroups,
  SALES_CUSTOMER_DIMS,
  type SalesAnalyticsFilters,
  type SalesGroupDim,
  type SalesGroupRow,
  type SalesGroupSort,
  type SalesGroupsResp,
} from "../../../api/salesAnalytics";
import { GroupRankBar, type SalesChartMetric } from "../../../components/charts/SalesDashboardCharts";
import { pct, pctSigned } from "../../../utils/format";
import { fmtMoney } from "../shared";
import { changeRatio } from "./SummaryView";
import { useAsyncData } from "./useAsyncData";
import { GROUP_TOP_OPTIONS, type SalesAnalyticsUrlState } from "./urlState";

export const GROUP_DIM_LABELS: Record<SalesGroupDim, string> = {
  customer: "客户",
  customer_type: "客户类型",
  customer_city: "客户城市",
  customer_source: "客户来源",
  salesperson: "销售人员",
  business_type: "业务类型",
  warehouse: "仓库",
  category: "商品大类",
  brand: "品牌",
  pn: "PN",
};

/** 维度 → 下钻时写入的筛选字段（无对应筛选参数的维度不可下钻）。 */
export const GROUP_DRILL_FIELD: Partial<Record<SalesGroupDim, keyof SalesAnalyticsUrlState>> = {
  customer: "customers",
  customer_type: "customerTypes",
  salesperson: "salespeople",
  business_type: "businessType",
  category: "categories",
  brand: "brands",
};

/** 下钻补丁：把组键加进对应多选筛选（去重），空值组（「未填」）不可下钻。 */
export function drillPatch(
  state: SalesAnalyticsUrlState,
  dim: SalesGroupDim,
  row: Pick<SalesGroupRow, "key" | "is_empty">,
): Partial<SalesAnalyticsUrlState> | null {
  const field = GROUP_DRILL_FIELD[dim];
  if (!field || row.is_empty) return null;
  // 商品大类的空值组键「未分类」本身是合法筛选值（后端匹配空大类）
  const current = state[field] as string[];
  return { [field]: [...new Set([...current, row.key])], tab: "orders", page: 1 } as Partial<SalesAnalyticsUrlState>;
}

const CHART_METRIC: Record<SalesGroupSort, SalesChartMetric | null> = {
  amount_ex_tax: "amount_ex_tax",
  amount_inc_tax: "amount_inc_tax",
  revenue: "revenue",
  gross_profit: "gross_profit",
  orders: "orders",
  customers: null,
  key: null,
};

export interface GroupsTabProps {
  state: SalesAnalyticsUrlState;
  apiFilters: SalesAnalyticsFilters;
  filterKey: string;
  patch: (next: Partial<SalesAnalyticsUrlState>, opts?: { history?: "push" | "replace" }) => void;
  costRestricted: boolean;
  profitRestricted: boolean;
  customerRestricted: boolean;
  toMessage: (err: unknown) => string;
}

export default function GroupsTab({
  state, apiFilters, filterKey, patch, costRestricted, profitRestricted, customerRestricted, toMessage,
}: GroupsTabProps) {
  const dim = customerRestricted && SALES_CUSTOMER_DIMS.includes(state.groupBy) ? "salesperson" : state.groupBy;
  const { data, loading, error, retry } = useAsyncData<SalesGroupsResp>(
    () => fetchSalesAnalyticsGroups({
      ...apiFilters, group_by: dim, sort: state.groupSort, order: state.groupOrder,
      top: state.groupTop, compare: state.compare,
    }),
    [filterKey, dim, state.groupSort, state.groupOrder, state.groupTop, state.compare],
    toMessage,
  );
  const effectiveSort = data?.sort ?? state.groupSort;
  const effectiveOrder = data?.order ?? state.groupOrder;
  const lineLevel = data?.line_level ?? ["category", "brand", "pn"].includes(dim);
  const basisKey = data?.amount_basis ?? "amount_ex_tax";
  const chartMetric: SalesChartMetric = CHART_METRIC[effectiveSort] ?? (basisKey as SalesChartMetric);

  const sortCol = (key: SalesGroupSort, enabled = true) => ({
    key,
    sorter: enabled,
    sortDirections: ["descend" as const, "ascend" as const],
    sortOrder: effectiveSort === key ? (effectiveOrder === "asc" ? "ascend" as const : "descend" as const) : null,
  });
  const money = (v: number | null | undefined) => fmtMoney(v ?? null);

  const rows: (SalesGroupRow & { _other?: boolean })[] = [
    ...(data?.groups ?? []),
    ...(data?.other ? [{
      ...data.other, key: `其他（${data.other.groups} 组）`, is_empty: false,
      cumulative_share: null, _other: true,
    } as SalesGroupRow & { _other: boolean }] : []),
  ];

  const columns: ColumnsType<SalesGroupRow & { _other?: boolean }> = [
    {
      title: GROUP_DIM_LABELS[dim], dataIndex: "key", ellipsis: true, ...sortCol("key"),
      render: (v: string, r) => {
        const drill = !r._other && drillPatch(state, dim, r);
        return drill ? (
          <Button type="link" size="small" style={{ padding: 0 }}
            onClick={() => patch(drill, { history: "push" })}>{v}</Button>
        ) : <span style={r._other ? { color: "var(--mb-text-3)" } : undefined}>{v}</span>;
      },
    },
    { title: "单数", dataIndex: "orders", width: 80, align: "right", ...sortCol("orders") },
    ...(lineLevel ? [{ title: "明细行", dataIndex: "lines", width: 80, align: "right" as const, render: (v?: number) => v ?? "—" }] : []),
    ...(dim !== "customer" && !customerRestricted ? [{
      title: "客户数", dataIndex: "customers", width: 80, align: "right" as const,
      render: (v?: number) => v ?? "—", ...sortCol("customers"),
    }] : []),
    ...(state.amounts.includes("ex") ? [{
      title: lineLevel ? "不含税（名义）" : "不含税金额", dataIndex: "amount_ex_tax", width: 130,
      align: "right" as const, render: money, ...sortCol("amount_ex_tax", !costRestricted),
    }] : []),
    ...(state.amounts.includes("inc") ? [{
      title: "含税订单金额", dataIndex: "amount_inc_tax", width: 130, align: "right" as const,
      render: money, ...sortCol("amount_inc_tax", !costRestricted),
    }] : []),
    ...(state.amounts.includes("revenue") ? [{
      title: "营收", dataIndex: "revenue", width: 120, align: "right" as const,
      render: money, ...sortCol("revenue"),
    }] : []),
    ...(!profitRestricted ? [{
      title: "毛利", dataIndex: "gross_profit", width: 130, align: "right" as const,
      render: (v: number | null, r: SalesGroupRow) => (
        <span>{money(v)}{r.gross_margin != null && <span style={{ marginLeft: 4, fontSize: 12, color: "var(--mb-text-3)" }}>{pct(r.gross_margin)}</span>}</span>
      ),
      ...sortCol("gross_profit"),
    }] : []),
    {
      title: <Tooltip title="按页面「金额区间口径」计算；该口径不可见时按营收">占比 / 累计</Tooltip>,
      key: "share", width: 120, align: "right",
      render: (_v, r) => `${pct(r.share)}${r.cumulative_share != null ? ` / ${pct(r.cumulative_share)}` : ""}`,
    },
    { title: "单均", dataIndex: "avg_order", width: 110, align: "right", render: money },
    ...(data?.compare_window ? [{
      title: <Tooltip title={`对比 ${data.compare_window.date_from} ~ ${data.compare_window.date_to}`}>较上月同期</Tooltip>,
      key: "compare", width: 110, align: "right" as const,
      render: (_v: unknown, r: SalesGroupRow) => {
        if (!r.compare && !("_other" in r)) return <span style={{ color: "var(--mb-text-3)" }}>上月无</span>;
        const cur = (r as unknown as Record<string, number | null>)[basisKey];
        const prev = r.compare ? (r.compare as unknown as Record<string, number | null>)[basisKey] : null;
        const ratio = changeRatio(cur, prev);
        return ratio == null ? "—" : `${ratio > 0 ? "↑" : ratio < 0 ? "↓" : ""}${pctSigned(ratio)}`;
      },
    }] : []),
  ];

  const onTableChange = (
    _pg: TablePaginationConfig,
    _f: Record<string, FilterValue | null>,
    sorter: SorterResult<SalesGroupRow> | SorterResult<SalesGroupRow>[],
  ) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    if (!s?.columnKey) return;
    patch({ groupSort: s.columnKey as SalesGroupSort, groupOrder: s.order === "ascend" ? "asc" : "desc" });
  };

  const dimOptions = (Object.keys(GROUP_DIM_LABELS) as SalesGroupDim[]).map((d) => ({
    value: d, label: GROUP_DIM_LABELS[d],
    disabled: customerRestricted && SALES_CUSTOMER_DIMS.includes(d),
  }));
  const conc = data?.concentration;

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <Space wrap>
        <span>分组维度</span>
        <Select aria-label="分组维度" style={{ width: 140 }} value={dim} options={dimOptions}
          onChange={(v: SalesGroupDim) => patch({ groupBy: v })} />
        <span>显示</span>
        <Select aria-label="Top N" style={{ width: 110 }} value={state.groupTop}
          options={GROUP_TOP_OPTIONS.map((n) => ({ value: n, label: n === 0 ? "全部" : `前 ${n}` }))}
          onChange={(v: number) => patch({ groupTop: v })} />
        {conc && conc.top1_share != null && (
          <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>
            集中度：第 1 名占 {pct(conc.top1_share)}，前 5 占 {pct(conc.top5_share)}，前 10 占 {pct(conc.top10_share)}
          </span>
        )}
      </Space>
      {lineLevel && (
        <Alert type="info" showIcon message="按明细行分组：一张单可同时计入多个组，各组单数之和可能大于总单数；不含税为按 13% 推算的名义值。" />
      )}
      {data && data.sort !== state.groupSort && (
        <Alert type="info" showIcon message="所选排序字段不可见：已改按单数排序，组序不反映隐藏金额的排名。" />
      )}
      {error ? (
        <Alert type="error" showIcon message="分组排行加载失败" description={error}
          action={<Button type="link" size="small" onClick={retry}>重试</Button>} />
      ) : (
        <>
          <GroupRankBar
            items={(data?.groups ?? []).map((g) => ({ ...g }))}
            metric={chartMetric}
            loading={loading}
            onItemClick={(key) => {
              const row = data?.groups.find((g) => g.key === key);
              const drill = row && drillPatch(state, dim, row);
              if (drill) patch(drill, { history: "push" });
            }}
          />
          <Table
            size="small"
            rowKey="key"
            loading={loading}
            dataSource={rows}
            columns={columns}
            pagination={false}
            onChange={onTableChange}
            scroll={{ x: 1000 }}
            locale={{ emptyText: "当前筛选下没有销售单" }}
            rowClassName={(r) => (r._other ? "sales-group-other" : "")}
          />
        </>
      )}
    </div>
  );
}
