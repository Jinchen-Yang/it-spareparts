// 销售分析（D-41 客户需求 Round1，PR-3b 后端 /sales/analytics/*，PR-4b 前端）。
// 三金额（不含税/含税订单金额/营收）一次全部返回，前端切换口径不重新请求；
// 金额与对账键被数据权限遮蔽时后端置 null（键名保留），定型一律 number | null，
// 绝不当 0。对账块与历史条目字段集不同（历史无 has_data / reconciled_display），
// 类型分开定义。
import { api } from "../api";

/** 业务类型候选取值（value=null 是「未填写」组，筛选用哨兵 __empty__，契约 §0）。 */
export interface SalesAnalyticsBusinessTypeOption {
  value: string | null;
  orders: number;
}

/** D-44 候选取值（value=null 为空值组，筛选用哨兵 __empty__；大类空值组后端已给
 * 「未分类」字面值，不走哨兵）。 */
export interface SalesAnalyticsValueOption {
  value: string | null;
  orders: number;
}

export interface SalesAnalyticsOptions {
  business_types: SalesAnalyticsBusinessTypeOption[];
  // 以下 D-44 新增；旧后端无这些键时按空表处理
  salespeople?: SalesAnalyticsValueOption[];
  customer_types?: SalesAnalyticsValueOption[];
  /** 按 customer_q 补全的客户规范名（最多 50 条）；无客户权限时为空表。 */
  customers?: SalesAnalyticsValueOption[];
  categories?: SalesAnalyticsValueOption[];
  brands?: SalesAnalyticsValueOption[];
  /** 金额拖动条右端：当前其他筛选下各口径最大值（被遮蔽口径为 null）。 */
  amount_range?: {
    amount_ex_tax_max: number | null;
    amount_inc_tax_max: number | null;
    revenue_max: number | null;
  };
}

export type SalesAmountBasis = "amount_ex_tax" | "amount_inc_tax" | "revenue";

/**
 * 缺失值筛选哨兵（DL-020 / A-AC06）：筛「（未填）」桶传此值。与后端
 * sales_analytics.EMPTY_FILTER 同串；真实对象名即使字面等于哨兵也按字面精确匹配，
 * 二者不碰撞。大类空值不走哨兵——后端用「未分类」字面值匹配空大类。
 */
export const EMPTY_FILTER_VALUE = "__empty__";

/** 全部筛选：日期闭区间（缺省 = 全部历史）；多选均为精确匹配；customer/salesperson
 * 为旧版子串模糊参数（兼容旧链接）。金额区间闭区间，口径由 amount_basis 指定（D-44 §7）；
 * 选了大类 / 品牌即进入行级口径（D-44 §8）。 */
export interface SalesAnalyticsFilters {
  date_from?: string;
  date_to?: string;
  business_type?: string[];
  customer?: string;
  salesperson?: string;
  customer_name?: string[];
  customer_type?: string[];
  salesperson_name?: string[];
  category?: string[];
  brand?: string[];
  amount_basis?: SalesAmountBasis;
  amount_min?: number;
  amount_max?: number;
  /** 默认 true（含），只有 false 时才落参数。 */
  include_negative?: boolean;
  include_empty?: boolean;
}

export interface SalesKpi {
  orders: number;
  amount_ex_tax_total: number | null;
  amount_inc_tax_total: number | null;
  /** 含税订单金额缺失单数（D-41 §3：缺失显示为空，不推算）。 */
  amount_inc_tax_missing: number;
  revenue_total: number | null;
  // D-44 新增（旧响应缺省）
  customers?: number;
  salespeople?: number;
  revenue_costed?: number | null;
  gross_profit?: number | null;
  gross_margin?: number | null;
  cost_coverage?: number | null;
  new_customers?: number | null;
  returning_customers?: number | null;
  /** 首单日期无法判定的客户数（Q-A-10：不当新客，标「无法判断」）。 */
  undetermined?: number | null;
  /** false = 区间缺起始日期（「全部」），不拆新老客，各拆分值为 null。 */
  available?: boolean;
}

export interface SalesAnalyticsSummary {
  window: { date_from: string | null; date_to: string | null };
  filters_echo: {
    business_type: string[];
    customer: string | null;
    salesperson: string | null;
  };
  as_of: string;
  kpi: SalesKpi;
  /** 行级口径（选了大类 / 品牌）：不含税为按 13% 推算的名义值。 */
  line_mode?: boolean;
  /** 上月同期 KPI（compare=true 且区间不长于一个月时）。 */
  compare?: { date_from: string; date_to: string; kpi: SalesKpi } | null;
  compare_available?: boolean;
}

/** 最新对账快照块（D-40 §4-§5）。has_data=false = 无任何快照，其余键全 null。 */
export interface SalesReconciliationBlock {
  has_data: boolean;
  reconciled_as_of: string | null;
  order_amount_at_snapshot: number | null;
  reconciled_amount: number | null;
  pending_amount: number | null;
  return_reconciled_amount: number | null;
  /** include_returns 唯一影响的字段：不含=已对账；含=已对账+退货已对账。 */
  reconciled_display: number | null;
}

export interface SalesAnalyticsOrderRow {
  order_id: number;
  order_no: string;
  order_date: string | null;
  /** 订单日期晚于服务端今天（本页不排除未来单，只打标）。 */
  is_future: boolean;
  salesperson: string | null;
  customer: string | null;
  business_type: string | null;
  data_status: string;
  amount_ex_tax: number | null;
  amount_inc_tax: number | null;
  revenue: number | null;
  customer_type?: string | null;
  gross_profit?: number | null;
  gross_margin?: number | null;
  reconciliation: SalesReconciliationBlock;
}

export const SALES_ANALYTICS_SORT_KEYS = [
  "amount_ex_tax",
  "amount_inc_tax",
  "revenue",
  "gross_profit",
  "order_date",
  "order_no",
  "business_type",
  "customer",
  "customer_type",
  "salesperson",
  // 对账四列（A-AC07：所有实际可见数据列双向排序；无 data_reconciliation 时
  // 后端回退 order_date，前端以响应回显为准）。return_reconciled_amount 按最新
  // 快照的退货已对账原值排序，不随 include_returns（契约 §3）。
  "reconciled_display",
  "pending_amount",
  "reconciled_as_of",
  "return_reconciled_amount",
] as const;
export type SortOrder = "asc" | "desc";
export type SalesAnalyticsSortKey = (typeof SALES_ANALYTICS_SORT_KEYS)[number];

export interface SalesAnalyticsOrdersQuery extends SalesAnalyticsFilters {
  sort?: SalesAnalyticsSortKey;
  order?: SortOrder;
  page?: number;
  page_size?: number;
  include_returns?: boolean;
}

export interface SalesAnalyticsOrdersResp {
  total: number;
  page: number;
  page_size: number;
  as_of: string;
  /** 实际生效的排序键：被遮蔽的金额/毛利/客户/对账键静默回退 order_date
   * （Q-A-04，契约 §10.3），回显的是生效值——不要假设 echo 等于请求值。 */
  sort: SalesAnalyticsSortKey;
  order?: SortOrder;
  include_returns: boolean;
  items: SalesAnalyticsOrderRow[];
}

/** 对账历史条目（D-40 §3 只追加，旧→新排序）。字段集与最新块不同：无 has_data、
 * 无 reconciled_display。 */
export interface SalesReconciliationHistoryEntry {
  import_batch_id: number;
  source: string;
  created_at: string | null;
  reconciled_as_of: string | null;
  order_amount_at_snapshot: number | null;
  reconciled_amount: number | null;
  pending_amount: number | null;
  return_reconciled_amount: number | null;
}

export interface SalesAnalyticsOrderDetail {
  order_id: number;
  order_no: string;
  order_date: string | null;
  is_future: boolean;
  salesperson: string | null;
  customer: string | null;
  business_type: string | null;
  data_status: string;
  amount_ex_tax: number | null;
  amount_inc_tax: number | null;
  revenue: number | null;
  customer_type?: string | null;
  gross_profit?: number | null;
  gross_margin?: number | null;
  /** D-44 订单明细行（unit_price / line_amount 随成本权限、毛利随利润权限遮蔽）。 */
  lines?: SalesOrderLine[];
  as_of: string;
  reconciliation: SalesReconciliationBlock;
  reconciliation_history: SalesReconciliationHistoryEntry[];
}

export interface SalesOrderLine {
  line_id: number;
  line_no: number | null;
  pn: string | null;
  description: string | null;
  category_major: string | null;
  brand: string | null;
  qty: number | null;
  unit_price: number | null;
  line_amount: number | null;
  revenue: number | null;
  counts_revenue: boolean;
  gross_profit: number | null;
  gross_margin: number | null;
}

/**
 * 筛选 → URLSearchParams（多选 business_type 用重复键 k=v1&k=v2，FastAPI list 形态）。
 * 不直接把数组交给 axios 默认序列化——那会产出 k[]=v1，后端解析不出来。
 */
export function toSalesAnalyticsParams(
  filters: SalesAnalyticsFilters,
  extra: Record<string, string | number | boolean> = {},
): URLSearchParams {
  const p = new URLSearchParams();
  if (filters.date_from) p.set("date_from", filters.date_from);
  if (filters.date_to) p.set("date_to", filters.date_to);
  for (const v of filters.business_type ?? []) {
    if (v) p.append("business_type", v);
  }
  if (filters.customer) p.set("customer", filters.customer);
  if (filters.salesperson) p.set("salesperson", filters.salesperson);
  const lists: [keyof SalesAnalyticsFilters, string][] = [
    ["customer_name", "customer_name"], ["customer_type", "customer_type"],
    ["salesperson_name", "salesperson_name"], ["category", "category"], ["brand", "brand"],
  ];
  for (const [field, key] of lists) {
    for (const v of (filters[field] as string[] | undefined) ?? []) {
      if (v) p.append(key, v);
    }
  }
  if (filters.amount_basis && filters.amount_basis !== "amount_ex_tax") {
    p.set("amount_basis", filters.amount_basis);
  }
  if (filters.amount_min != null) p.set("amount_min", String(filters.amount_min));
  if (filters.amount_max != null) p.set("amount_max", String(filters.amount_max));
  if (filters.include_negative === false) p.set("include_negative", "false");
  if (filters.include_empty === false) p.set("include_empty", "false");
  for (const [k, v] of Object.entries(extra)) {
    // 空串/空值/0 不落参数（脏值不静默进请求；page=0 等无意义值不进 URL）；
    // include_returns=false 走缺省，不落键
    if (v === "" || v == null || v === false || v === 0) continue;
    p.set(k, String(v));
  }
  return p;
}

export const fetchSalesAnalyticsOptions = (
  filters: SalesAnalyticsFilters = {},
  customerQ = "",
) =>
  api.get<SalesAnalyticsOptions>("/sales/analytics/options", {
    params: toSalesAnalyticsParams(filters, customerQ ? { customer_q: customerQ } : {}),
  });

/** 汇总恒带 compare=true：上月同期 KPI 一并返回（区间长于一个月时后端给 null），
 * 页面「对比上月」开关只决定是否展示，切换不重新请求。 */
export const fetchSalesAnalyticsSummary = (filters: SalesAnalyticsFilters) =>
  api.get<SalesAnalyticsSummary>("/sales/analytics/summary", {
    params: toSalesAnalyticsParams(filters, { compare: true }),
  });

export const fetchSalesAnalyticsOrders = (query: SalesAnalyticsOrdersQuery) => {
  const extra: Record<string, string | number | boolean> = {};
  if (query.sort) extra.sort = query.sort;
  if (query.order && query.order !== "desc") extra.order = query.order;
  if (query.page) extra.page = query.page;
  if (query.page_size) extra.page_size = query.page_size;
  if (query.include_returns) extra.include_returns = true;
  return api.get<SalesAnalyticsOrdersResp>("/sales/analytics/orders", {
    params: toSalesAnalyticsParams(query, extra),
  });
};

export const fetchSalesAnalyticsOrderDetail = (
  orderId: number,
  includeReturns: boolean,
) =>
  api.get<SalesAnalyticsOrderDetail>(`/sales/analytics/orders/${orderId}`, {
    params: includeReturns ? { include_returns: true } : {},
  });

// ---------------------------------------------------------------- D-44 分组 / 趋势 / 直方图 / 导出

export const SALES_GROUP_DIMS = [
  "customer", "customer_type", "customer_city", "customer_source",
  "salesperson", "business_type", "warehouse", "category", "brand", "pn",
] as const;
export type SalesGroupDim = (typeof SALES_GROUP_DIMS)[number];
/** 客户类属维度：无客户权限时后端 403，前端不给入口。 */
export const SALES_CUSTOMER_DIMS: readonly SalesGroupDim[] = [
  "customer", "customer_type", "customer_city", "customer_source",
];
/** 分组排序键（契约 §6）。lines 只对行级维度（category/brand/pn）合法，单头维度
 * 请求会 422——列本身只在行级维度出现，不另发校验；basis_change 需 compare 可用，
 * 未开对比时后端静默退化为组键序（列隐藏时无排序入口）。 */
export const SALES_GROUP_SORTS = [
  "amount_ex_tax", "amount_inc_tax", "revenue", "orders", "gross_profit", "customers", "key",
  "lines", "avg_order", "basis_change",
] as const;
export type SalesGroupSort = (typeof SALES_GROUP_SORTS)[number];

export interface SalesGroupMetrics {
  orders: number;
  customers?: number;
  lines?: number;
  amount_ex_tax: number | null;
  amount_inc_tax: number | null;
  revenue: number | null;
  gross_profit: number | null;
  gross_margin: number | null;
  avg_order: number | null;
}

export interface SalesGroupRow extends SalesGroupMetrics {
  /** 缺失组为 null + is_empty=true（契约 §6）——显示标签由前端给，下钻转哨兵。 */
  key: string | null;
  is_empty: boolean;
  share: number | null;
  cumulative_share: number | null;
  compare?: SalesGroupMetrics | null;
}

export interface SalesGroupsResp {
  group_by: SalesGroupDim;
  line_level: boolean;
  sort: SalesGroupSort;
  order: SortOrder;
  amount_basis: SalesAmountBasis;
  top: number;
  total_groups: number;
  groups: SalesGroupRow[];
  other: (Omit<SalesGroupMetrics, "customers" | "lines"> & {
    key: string; is_other?: boolean; groups: number; share: number | null;
  }) | null;
  concentration: { top1_share: number | null; top5_share: number | null; top10_share: number | null };
  compare_window: { date_from: string; date_to: string } | null;
  compare_available: boolean;
}

export interface SalesGroupsQuery extends SalesAnalyticsFilters {
  group_by: SalesGroupDim;
  sort?: SalesGroupSort;
  order?: SortOrder;
  top?: number;
  compare?: boolean;
}

export const fetchSalesAnalyticsGroups = (q: SalesGroupsQuery) => {
  const extra: Record<string, string | number | boolean> = { group_by: q.group_by };
  if (q.sort) extra.sort = q.sort;
  if (q.order) extra.order = q.order;
  // top=0（全部）是有效值：toSalesAnalyticsParams 会丢弃 0，这里单独写入
  const params = toSalesAnalyticsParams(q, extra);
  if (q.top != null) params.set("top", String(q.top));
  if (q.compare) params.set("compare", "true");
  return api.get<SalesGroupsResp>("/sales/analytics/groups", { params });
};

export type SalesTrendGranularity = "day" | "week" | "month";

export interface SalesTrendBucket extends Omit<SalesGroupMetrics, "lines"> {
  /** null = 「无日期」桶（仅「全部」口径返回，A-AC05：无日期单不猜归属）。 */
  bucket: string | null;
  no_date?: boolean;
}

export interface SalesTrendResp {
  granularity: SalesTrendGranularity;
  date_from?: string | null;
  date_to?: string | null;
  buckets: SalesTrendBucket[];
  compare: SalesTrendBucket[] | null;
  compare_window: { date_from: string; date_to: string } | null;
  compare_available: boolean;
  weekday: {
    isodow: number; orders: number;
    amount_ex_tax: number | null; amount_inc_tax: number | null; revenue: number | null;
  }[];
  /** 无日期单数（有界区间时不入任何日期桶，前端显示排除说明）。 */
  no_date_orders?: number;
  has_no_date?: boolean;
  /** 无日期单的三金额（契约 §7/§10.5：有界区间时不入桶，提示条展示）。 */
  no_date_amount_ex_tax?: number | null;
  no_date_amount_inc_tax?: number | null;
  no_date_revenue?: number | null;
}

export const fetchSalesAnalyticsTrend = (
  filters: SalesAnalyticsFilters,
  granularity: SalesTrendGranularity,
  compare: boolean,
) =>
  api.get<SalesTrendResp>("/sales/analytics/trend", {
    params: toSalesAnalyticsParams(filters, {
      granularity, ...(compare ? { compare: true } : {}),
    }),
  });

export interface SalesAmountHistogram {
  amount_basis: SalesAmountBasis;
  max: number | null;
  /** bins 右端闭（8000/8501 恰落段界归该段）；负 / 无金额单单列，不并入段。 */
  bins: { lo: number; hi: number; orders: number }[];
  negative_orders: number;
  empty_orders: number;
  /** sum(bins) + negative + empty = total（逐单守恒，A-AC09）。 */
  total?: number;
}

export const fetchSalesAmountHistogram = (filters: SalesAnalyticsFilters) =>
  api.get<SalesAmountHistogram>("/sales/analytics/amount-histogram", {
    params: toSalesAnalyticsParams(filters),
  });

/** 订单 CSV 导出（同筛选同排序同脱敏）：以 blob 取回，由调用方触发浏览器下载。 */
export const downloadSalesOrdersCsv = (
  filters: SalesAnalyticsFilters,
  sort: SalesAnalyticsSortKey,
  order: SortOrder,
) =>
  api.get<Blob>("/sales/analytics/orders/export", {
    params: toSalesAnalyticsParams(filters, { sort, ...(order !== "desc" ? { order } : {}) }),
    responseType: "blob",
  });
