// 销售分析（D-41 客户需求 Round1，PR-3b 后端 /sales/analytics/*，PR-4b 前端）。
// 三金额（不含税/含税订单金额/营收）一次全部返回，前端切换口径不重新请求；
// 金额与对账键被数据权限遮蔽时后端置 null（键名保留），定型一律 number | null，
// 绝不当 0。对账块与历史条目字段集不同（历史无 has_data / reconciled_display），
// 类型分开定义。
import { api } from "../api";

/** 业务类型候选取值（value=null 是「未填写」组，无法作为筛选值传递，只作展示）。 */
export interface SalesAnalyticsBusinessTypeOption {
  value: string | null;
  orders: number;
}

export interface SalesAnalyticsOptions {
  business_types: SalesAnalyticsBusinessTypeOption[];
}

/** 全部筛选：日期闭区间（缺省 = 全部历史）；business_type 多选精确匹配；客户/销售人员为子串模糊。 */
export interface SalesAnalyticsFilters {
  date_from?: string;
  date_to?: string;
  business_type?: string[];
  customer?: string;
  salesperson?: string;
}

export interface SalesAnalyticsSummary {
  window: { date_from: string | null; date_to: string | null };
  filters_echo: {
    business_type: string[];
    customer: string | null;
    salesperson: string | null;
  };
  as_of: string;
  kpi: {
    orders: number;
    amount_ex_tax_total: number | null;
    amount_inc_tax_total: number | null;
    /** 含税订单金额缺失单数（D-41 §3：缺失显示为空，不推算）。 */
    amount_inc_tax_missing: number;
    revenue_total: number | null;
  };
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
  reconciliation: SalesReconciliationBlock;
}

export const SALES_ANALYTICS_SORT_KEYS = [
  "amount_ex_tax",
  "amount_inc_tax",
  "revenue",
] as const;
export type SalesAnalyticsSortKey = (typeof SALES_ANALYTICS_SORT_KEYS)[number];

export interface SalesAnalyticsOrdersQuery extends SalesAnalyticsFilters {
  sort?: SalesAnalyticsSortKey;
  page?: number;
  page_size?: number;
  include_returns?: boolean;
}

export interface SalesAnalyticsOrdersResp {
  total: number;
  page: number;
  page_size: number;
  as_of: string;
  /** 实际生效的排序键：金额键被遮蔽（data_purchase_cost=false）时后端静默回退
   * revenue（行序防泄漏），回显的是生效值——不要假设 echo 等于请求值。 */
  sort: SalesAnalyticsSortKey;
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
  as_of: string;
  reconciliation: SalesReconciliationBlock;
  reconciliation_history: SalesReconciliationHistoryEntry[];
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
  for (const [k, v] of Object.entries(extra)) {
    // 空串/空值/0 不落参数（脏值不静默进请求；page=0 等无意义值不进 URL）；
    // include_returns=false 走缺省，不落键
    if (v === "" || v == null || v === false || v === 0) continue;
    p.set(k, String(v));
  }
  return p;
}

export const fetchSalesAnalyticsOptions = () =>
  api.get<SalesAnalyticsOptions>("/sales/analytics/options");

export const fetchSalesAnalyticsSummary = (filters: SalesAnalyticsFilters) =>
  api.get<SalesAnalyticsSummary>("/sales/analytics/summary", {
    params: toSalesAnalyticsParams(filters),
  });

export const fetchSalesAnalyticsOrders = (query: SalesAnalyticsOrdersQuery) => {
  const extra: Record<string, string | number | boolean> = {};
  if (query.sort) extra.sort = query.sort;
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
