// 采购分析（D-39 客户需求 Round1，PR-3a 后端 /purchases/analytics/*）。
// 三口径（含税/不含税/实付）一次全部返回，前端切换口径不重新请求；
// 金额键被 data_purchase_cost 遮蔽时后端置 null（键名保留），定型一律 number | null。
import { api } from "../api";

/** 筛选取值：类型原值 / 采购员 / 关联销售人员 / 分类（未分类固定垫底）。 */
export interface PurchaseAnalyticsOptions {
  customers: { customer_id: number; customer: string | null }[];
  customers_has_more: boolean;
  selected_customers: { customer_id: number; customer: string | null }[];
  source_types: string[];
  purchasers: string[];
  salespersons: string[];
  categories: string[];
  uncategorized_label: string;
  no_linked_label: string;
}

/** 全部筛选（多选 = 精确匹配；日期闭区间，缺省 = 全部历史）。 */
export interface PurchaseAnalyticsFilters {
  date_from?: string;
  date_to?: string;
  source_type?: string[];
  purchaser?: string[];
  salesperson?: string[];
  customer_id?: number[];
  category?: string[];
}

/** 后端 filters_echo：把可缺省字段回显为完整形态（null = 未筛）。 */
export interface PurchaseAnalyticsFiltersEcho {
  date_from: string | null;
  date_to: string | null;
  source_type: string[];
  purchaser: string[];
  salesperson: string[];
  customer_id: number[];
  category: string[];
  group_by: string | null;
  group_key: string | null;
}

export interface PurchaseAnalyticsKpi {
  orders: number;
  lines: number;
  amount_inc: number | null;
  amount_ex: number | null;
  amount_paid: number | null;
  /** 名义含税占比 = 未标税率非含税单的推算含税 ÷ 含税合计；无金额权限或分母为 0 时 null。 */
  nominal_inc_share: number | null;
  /** 名义不含税占比 = 未标税率含税单的推算不含税 ÷ 不含税合计；同上（D-39 名义侧裁定）。 */
  nominal_ex_share: number | null;
}

/** 名义侧（D-39 2026-10-18 裁定）：未标税率时按 13% 推算的那一侧；已标税率为 null。 */
export type NominalSide = "inc" | "ex" | null;

/** 分组行。customer 维度下 label 为 null、客户名放 customer（登记过的脱敏键）。 */
export interface PurchaseAnalyticsGroup {
  key: string;
  label: string | null;
  orders: number;
  lines: number;
  amount_inc: number | null;
  amount_ex: number | null;
  amount_paid: number | null;
  /** 分组内名义占比（同 KPI 口径，仅该组分子/分母）；被遮蔽或分母 0 时 null。 */
  nominal_inc_share?: number | null;
  nominal_ex_share?: number | null;
  customer?: string | null;
}

export interface PurchaseAnalyticsSummary {
  basis_mode: "line" | "order";
  filters_echo: PurchaseAnalyticsFiltersEcho;
  as_of: string;
  kpi: PurchaseAnalyticsKpi;
  groups: PurchaseAnalyticsGroup[];
  /** true = 金额被遮蔽，分组已按订单数排序（前端不要再按金额解释组序）。 */
  groups_ranking_restricted: boolean;
}

export interface PurchaseAnalyticsOrderRow {
  order_id: number;
  order_no: string;
  order_date: string | null;
  source_type: string | null;
  purchaser: string | null;
  salesperson: string | null;
  customer_id: number | null;
  customer: string | null;
  lines: number | null;
  amount_inc: number | null;
  amount_ex: number | null;
  amount_paid: number | null;
  /** 该单的名义侧（未标税率才有）；随金额一起被遮蔽（null）。 */
  nominal_side: NominalSide;
}

export interface PurchaseAnalyticsOrdersQuery extends PurchaseAnalyticsFilters {
  group_by?: string;
  group_key?: string;
  sort?: string;
  order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}

export interface PurchaseAnalyticsOrdersResp {
  total: number;
  page: number;
  page_size: number;
  sort: string;
  order: string;
  /** 实际生效的排序键（被遮蔽回退 order_date 时与 sort 不同）。 */
  effective_sort: string;
  ranking_restricted: boolean;
  basis_mode: "line" | "order";
  filters_echo: PurchaseAnalyticsFiltersEcho;
  as_of: string;
  items: PurchaseAnalyticsOrderRow[];
}

export interface PurchaseAnalyticsOrderLine {
  line_id: number;
  line_no: number | null;
  part_id: number;
  pn_std: string | null;
  description: string | null;
  brand: string | null;
  qty: number | null;
  unit_price: number | null;
  line_amount: number | null;
  category: string | null;
  line_amount_inc: number | null;
  line_amount_ex: number | null;
}

/** 订单明细（单头三口径 + 单头批次行）。category 传入时附 category_matched。 */
export interface PurchaseAnalyticsOrderDetail {
  order_id: number;
  order_no: string;
  order_date: string | null;
  source_type: string | null;
  purchaser: string | null;
  data_status: string | null;
  is_tax_inclusive: boolean | null;
  tax_rate: number | null;
  tax_rate_effective: number | null;
  amount_ex: number | null;
  amount_inc: number | null;
  amount_paid: number | null;
  /** 该单的名义侧（未标税率才有）；随金额一起被遮蔽（null）。 */
  nominal_side: NominalSide;
  linked_sales_order_no: string | null;
  salesperson: string | null;
  customer_id: number | null;
  customer: string | null;
  lines_count: number;
  lines: PurchaseAnalyticsOrderLine[];
  category_matched?: {
    lines: number;
    amount_inc: number | null;
    amount_ex: number | null;
    amount_paid: number | null;
  };
}

/**
 * 多选筛选 → URLSearchParams（重复键 k=v1&k=v2，FastAPI list 参数形态）。
 * 不直接把数组交给 axios 默认序列化——那会产出 k[]=v1，后端解析不出来。
 */
export function toAnalyticsParams(
  filters: PurchaseAnalyticsFilters,
  extra: Record<string, string | number> = {},
): URLSearchParams {
  const p = new URLSearchParams();
  if (filters.date_from) p.set("date_from", filters.date_from);
  if (filters.date_to) p.set("date_to", filters.date_to);
  appendList(p, "source_type", filters.source_type);
  appendList(p, "purchaser", filters.purchaser);
  appendList(p, "salesperson", filters.salesperson);
  appendList(p, "category", filters.category);
  for (const id of filters.customer_id ?? []) {
    if (Number.isInteger(id)) p.append("customer_id", String(id));
  }
  for (const [k, v] of Object.entries(extra)) {
    // 空串/空值不落参数（脏值不静默进请求）
    if (v !== "" && v != null) p.set(k, String(v));
  }
  return p;
}

function appendList(p: URLSearchParams, key: string, values?: string[]): void {
  for (const v of values ?? []) {
    if (v) p.append(key, v);
  }
}

/** 客户候选最多200项；额外补齐最多200个已选ID（分享链接恢复姓名）。 */
export const fetchPurchaseAnalyticsOptions = (customerQuery = "", selectedIds: number[] = []) => {
  const params = new URLSearchParams({ customer_q: customerQuery });
  selectedIds.forEach(id => params.append("selected_customer_id", String(id)));
  return api.get<PurchaseAnalyticsOptions>("/purchases/analytics/options", { params });
};

export const fetchPurchaseAnalyticsSummary = (filters: PurchaseAnalyticsFilters & {
  group_by?: string;
}) => api.get<PurchaseAnalyticsSummary>("/purchases/analytics/summary", {
  params: toAnalyticsParams(filters, filters.group_by ? { group_by: filters.group_by } : {}),
});

export const fetchPurchaseAnalyticsOrders = (query: PurchaseAnalyticsOrdersQuery) => {
  const extra: Record<string, string | number> = {};
  if (query.group_by) extra.group_by = query.group_by;
  if (query.group_key) extra.group_key = query.group_key;
  if (query.sort) extra.sort = query.sort;
  if (query.order) extra.order = query.order;
  if (query.page) extra.page = query.page;
  if (query.page_size) extra.page_size = query.page_size;
  return api.get<PurchaseAnalyticsOrdersResp>("/purchases/analytics/orders", {
    params: toAnalyticsParams(query, extra),
  });
};

export const fetchPurchaseAnalyticsOrderDetail = (
  orderId: number,
  category: string[] = [],
) => {
  const p = new URLSearchParams();
  for (const c of category) {
    if (c) p.append("category", c);
  }
  return api.get<PurchaseAnalyticsOrderDetail>(
    `/purchases/analytics/orders/${orderId}`,
    { params: p },
  );
};
