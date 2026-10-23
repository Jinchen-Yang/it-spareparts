// 分组排行页签（D-44 方案 §3.3）：按客户 / 客户类型 / 城市 / 来源 / 销售 / 业务类型 /
// 仓库 / 商品大类 / 品牌 / PN 分组；任一指标列升序 / 降序（以响应回显为准：被遮蔽的
// 金额 / 毛利排序后端退回单数）；Top N + “其他”（合计 = 总计，不丢钱）；集中度；
// 上月同期增减。点某组 = 把该值加进筛选并切到订单明细（只对有筛选参数的维度）。
import { Alert, Button, Select, Space, Table, Tooltip } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { FilterValue, SorterResult } from "antd/es/table/interface";
import {
  EMPTY_FILTER_VALUE,
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

/** 分组行显示标签：缺失组由前端给标签（契约 §0：大类「未分类」，其余「（未填）」）。 */
export function groupRowLabel(dim: SalesGroupDim, row: Pick<SalesGroupRow, "key" | "is_empty">): string {
  if (!row.is_empty) return row.key ?? "（未填）";
  return dim === "category" ? "未分类" : "（未填）";
}

/** 行级维度（契约 §6：category / brand / pn，`lines` 排序仅对这些维度合法）。 */
export const LINE_LEVEL_DIMS: readonly SalesGroupDim[] = ["category", "brand", "pn"];

/**
 * 排序键 × 维度归一（契约 §6：单头维度请求 `sort=lines` 是组合校验 422）。URL 里的
 * gsort=lines 可能随维度切换残留（用户先在商品大类下按明细行排序，再切到客户；或
 * 直开 gsort=lines&group_by=customer 的分享链）——请求前归一为单数，不携带非法键；
 * 回显 ≠ URL 值走下方「已改按…」提示。其余键（含 basis_change）对所有维度合法
 * （basis_change 未开 compare 时后端静默退化，不 422）。
 */
export function normalizeGroupSort(sort: SalesGroupSort, dim: SalesGroupDim): SalesGroupSort {
  return sort === "lines" && !LINE_LEVEL_DIMS.includes(dim) ? "orders" : sort;
}

/**
 * 下钻补丁（A-AC13 收窄语义）：点组 = 该维度筛选**收窄为仅此值**（已选 A/B 点 A
 * → 只留 A），不是追加——排行页各组的对比语境在收窄后仍成立；同时切到订单明细
 * 并回到第 1 页。空值组同样可下钻：转后端哨兵（大类空值走「未分类」字面值，
 * 契约 §0）。返回 null = 该维度无筛选参数（城市 / 来源 / 仓库 / PN），不可下钻。
 */
export function drillPatch(
  _state: SalesAnalyticsUrlState,
  dim: SalesGroupDim,
  row: Pick<SalesGroupRow, "key" | "is_empty">,
): Partial<SalesAnalyticsUrlState> | null {
  const field = GROUP_DRILL_FIELD[dim];
  if (!field) return null;
  const value = row.is_empty
    ? (dim === "category" ? "未分类" : EMPTY_FILTER_VALUE)
    : row.key;
  if (value == null) return null;
  return { [field]: [value], tab: "orders", page: 1 } as Partial<SalesAnalyticsUrlState>;
}

const CHART_METRIC: Record<SalesGroupSort, SalesChartMetric | null> = {
  amount_ex_tax: "amount_ex_tax",
  amount_inc_tax: "amount_inc_tax",
  revenue: "revenue",
  gross_profit: "gross_profit",
  orders: "orders",
  customers: null,
  key: null,
  // 新增三键（契约 §6 返工）：无对应图表指标，图例口径回退响应 amount_basis
  lines: null,
  avg_order: null,
  basis_change: null,
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
  /** 所属会话 owner（页级下传，DL-010 返工）：失效时请求前拒发并清旧数据。 */
  ownerToken: string | null;
  /** 页级过期通知（DL-010 落地清理）：检测到 owner 失效的迟到响应通知页级清旧上下文。 */
  onOwnerStale: () => void;
}

export default function GroupsTab({
  state, apiFilters, filterKey, patch, costRestricted, profitRestricted, customerRestricted, toMessage, ownerToken, onOwnerStale,
}: GroupsTabProps) {
  const dim = customerRestricted && SALES_CUSTOMER_DIMS.includes(state.groupBy) ? "salesperson" : state.groupBy;
  // lines 仅行级维度合法（契约 §6）：维度切到单头（或受限替换）时归一，防 422
  const requestSort = normalizeGroupSort(state.groupSort, dim);
  const { data, loading, error, retry } = useAsyncData<SalesGroupsResp>(
    (context) => fetchSalesAnalyticsGroups({
      ...apiFilters, group_by: dim, sort: requestSort, order: state.groupOrder,
      top: state.groupTop, compare: state.compare,
    }, context),
    [filterKey, dim, requestSort, state.groupOrder, state.groupTop, state.compare],
    toMessage,
    true,
    ownerToken,
    onOwnerStale,
    "read_groups",
  );
  const effectiveSort = data?.sort ?? requestSort;
  const effectiveOrder = data?.order ?? state.groupOrder;
  const lineLevel = data?.line_level ?? LINE_LEVEL_DIMS.includes(dim);
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
      render: (v: string | null, r) => {
        const label = groupRowLabel(dim, r);
        const drill = !r._other && drillPatch(state, dim, r);
        return drill ? (
          <Button type="link" size="small" style={{ padding: 0 }}
            onClick={() => patch(drill, { history: "push" })}>{label}</Button>
        ) : <span style={r._other ? { color: "var(--mb-text-2)" } : undefined}>{label}</span>;
      },
    },
    { title: "单数", dataIndex: "orders", width: 80, align: "right", ...sortCol("orders") },
    // 明细行数排序（契约 §6 返工）：只随行级维度的这一列出现——单头维度没有该列，
    // 也就不会发出后端 422 的 sort=lines 请求
    ...(lineLevel ? [{ title: "明细行", dataIndex: "lines", width: 80, align: "right" as const, render: (v?: number) => v ?? "—", ...sortCol("lines") }] : []),
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
    // 单均排序 = 响应 amount_basis 口径总额 ÷ 去重单数（与展示列同值；被遮时后端
    // 已强制回 revenue，排序与展示同口径不泄漏——契约 §6）
    { title: "单均", dataIndex: "avg_order", width: 110, align: "right", render: money, ...sortCol("avg_order") },
    ...(data?.compare_window ? [{
      // basis_change = 真实同期变化率排序（基期 ≤0/未知不可比较，空值恒末），与金额
      // 排序不等价——只在对比列可见时提供入口（契约 §6/§10.10）
      title: <Tooltip title={`对比 ${data.compare_window.date_from} ~ ${data.compare_window.date_to}`}>较上月同期</Tooltip>,
      width: 110, align: "right" as const, ...sortCol("basis_change"),
      render: (_v: unknown, r: SalesGroupRow) => {
        if (!r.compare && !("_other" in r)) return <span style={{ color: "var(--mb-text-2)" }}>上月无</span>;
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
    // 控件主题（A3/DL-025 返工）由页面根 ConfigProvider 提供：受限账号「分组维度」
    // 弹层里的禁用客户类选项（portal，与触发控件同 token 集 / hashId）同享覆盖。
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
        <Alert type="info" showIcon message="所选排序不适用于当前维度或权限：已按单数排序（以响应回显为准），组序不反映该列排名。" />
      )}
      {error ? (
        <Alert type="error" showIcon message="分组排行加载失败" description={error}
          action={<Button type="link" size="small" onClick={retry}>重试</Button>} />
      ) : (
        <>
          <GroupRankBar
            items={(data?.groups ?? []).map((g) => ({ ...g, key: groupRowLabel(dim, g) }))}
            metric={chartMetric}
            loading={loading}
            onItemClick={(label) => {
              const row = data?.groups.find((g) => groupRowLabel(dim, g) === label);
              const drill = row && drillPatch(state, dim, row);
              if (drill) patch(drill, { history: "push" });
            }}
          />
          <Table
            size="small"
            rowKey={(r) => (r._other ? "__other__" : (r.key ?? "__empty__"))}
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
