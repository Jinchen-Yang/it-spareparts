/** 维保数据分析看板 API 客户端（2026-08-21，#pn-ranking 契约）。 */
import { api } from "../api";

/** 六态信封（与 boss-board 同约定）：restricted/not_imported 不带 value，绝不渲染 0。 */
export interface Stat<T = string | number> {
  state: "ready" | "restricted" | "not_imported" | "partial" | "stale" | "error";
  value: T | null;
  as_of?: string | null;
}

export interface PnRankingRow {
  rank: number;
  part_id: number | null;
  pn: string;
  description: string | null;
  occurrences: number;
  order_count: number;
  project_count: number;
  qty: string | null;
  return_qty: string | null;
  effective_qty: string;
  cost_inc: Stat<string>;
  cost_ex: Stat<string>;
  cost_share_pct: number | null;
  missing_lines: number;
  monthly_avg_qty: number | null;
  /** 所选期间已确认实际领用；不以需求数量替代。 */
  issued_qty: string;
  /** 所选期间有效收货台账数量，包含全部件况。 */
  receipt_qty: string;
  receipt_return_rate_pct: number | null;
  /** 历史坏件辅助字段；正式返还展示使用上面的 receipt 字段。 */
  bad_return_qty: string;
  bad_return_rate_pct: number | null;
  first_date: string | null;
  last_date: string | null;
}

export interface PnRanking {
  rows: PnRankingRow[];
  total: number;
  page: number;
  page_size: number;
  window: {
    range: string;
    date_from: string | null;
    date_to: string | null;
    months: number | null;
  };
  summary: {
    part_count: number;
    total_cost_inc: Stat<string>;
    total_cost_ex: Stat<string>;
    total_effective_qty: string;
    total_issued_qty: string;
    total_receipt_qty: string;
    total_bad_return_qty: string;
    wbdd_ready: boolean;
  };
  sort: string;
}

export interface PnRankingParams {
  range?: string;
  date_from?: string;
  date_to?: string;
  q?: string;
  /** Board business-type codes as CSV; the full six-code set (or `all`) disables filtering.
   *  A legacy five-code URL is an explicit subset (it no longer equals `all`). */
  business_type?: string;
  /** 项目主键 CSV；空/未传不过滤。 */
  project?: string;
  /** 客户名包含匹配；空/未传不过滤。 */
  customer?: string;
  /** 销售名包含匹配；空/未传不过滤。 */
  sp?: string;
  /** 需求单号包含匹配；空/未传不过滤。 */
  order_no?: string;
  /** 需求类型码 CSV（repair|stock）；空或全选（两项）不过滤。 */
  demand_type?: string;
  /** 仓库值 CSV；空/未传不过滤。 */
  warehouse?: string;
  /** 成本来源码 CSV（linked|estimated|manual|missing）；空或全选（四项）不过滤。 */
  cost_source?: string;
  sort?: string;
  page?: number;
  page_size?: number;
}

// ===== 开支统计（v1.34 spend-trend）：参数/权限/范围与 pn-ranking 同口径 =====

/** 时间统计粒度：分桶起点为 date_trunc(granularity)，周桶为周一。 */
export type SpendTrendGranularity = "day" | "week" | "month" | "year";

/** 开支分类：四类业务档 + 非维保 + 未标注 + 未归属（无活跃挂靠项目）。 */
export type SpendBusinessTypeCode =
  | "overall"
  | "spare"
  | "computing"
  | "refit"
  | "other"
  | "unlabeled"
  | "unassigned";

export interface SpendTrendParams {
  range?: string;
  date_from?: string;
  date_to?: string;
  granularity?: SpendTrendGranularity;
  business_type?: string;
  /** 项目主键 CSV；空/未传不过滤。 */
  project?: string;
  /** 客户名包含匹配；空/未传不过滤。 */
  customer?: string;
  /** 销售名包含匹配；空/未传不过滤。 */
  sp?: string;
  /** 需求单号包含匹配；空/未传不过滤。 */
  order_no?: string;
  /** 需求类型码 CSV（repair|stock）；空或全选（两项）不过滤。 */
  demand_type?: string;
  /** 仓库值 CSV；空/未传不过滤。 */
  warehouse?: string;
  /** 成本来源码 CSV（linked|estimated|manual|missing）；空或全选（四项）不过滤。 */
  cost_source?: string;
}

export interface SpendTrendBucket {
  /** 桶起点 YYYY-MM-DD（周桶为周一）。 */
  bucket: string;
  order_count: number;
  qty: string;
  effective_qty: string;
  missing_lines: number;
  cost_inc: Stat<string>;
  cost_ex: Stat<string>;
  /** 桶内只给有数据的档位（零数据档位后端省略）——读取端一律按缺省 null 处理。 */
  by_business_type: Partial<Record<SpendBusinessTypeCode, Stat<string>>>;
}

export interface SpendBusinessTypeRow {
  code: SpendBusinessTypeCode;
  label: string;
  order_count: number;
  qty: string;
  effective_qty: string;
  missing_lines: number;
  cost_inc: Stat<string>;
  cost_ex: Stat<string>;
  cost_share_pct: number | null;
}

export interface SpendSalespersonRow {
  /** null = 需求单未标注销售（页面显示「未标注」，不猜 0）。 */
  salesperson: string | null;
  order_count: number;
  qty: string;
  effective_qty: string;
  cost_inc: Stat<string>;
  cost_ex: Stat<string>;
  cost_share_pct: number | null;
}

/** 按项目汇总行（v1.35）：project_id=null 是未归属（无活跃挂靠项目）行。 */
export interface SpendProjectRow {
  project_id: string | null;
  display_name: string;
  business_type_code: string;
  business_type_label: string;
  order_count: number;
  qty: string;
  effective_qty: string;
  cost_inc: Stat<string>;
  cost_ex: Stat<string>;
  cost_share_pct: number | null;
}

export interface SpendTrendResponse {
  granularity: SpendTrendGranularity;
  window: { range: string; date_from: string | null; date_to: string | null };
  buckets: SpendTrendBucket[];
  by_business_type: SpendBusinessTypeRow[];
  by_salesperson: SpendSalespersonRow[];
  by_project: SpendProjectRow[];
  summary: {
    bucket_count: number;
    order_count: number;
    qty: string;
    effective_qty: string;
    missing_lines: number;
    total_cost_inc: Stat<string>;
    total_cost_ex: Stat<string>;
    wbdd_ready: boolean;
  };
}

/** GET /maintenance/analytics/filter-options 响应：仓库下拉候选。 */
export interface AnalyticsFilterOptions {
  warehouses: string[];
}

export const fetchPnRanking = async (params: PnRankingParams) => {
  const resp = await api.get<PnRanking>("/maintenance/analytics/pn-ranking", {
    params,
  });
  return resp.data;
};

export const fetchSpendTrend = async (params: SpendTrendParams) => {
  const resp = await api.get<SpendTrendResponse>(
    "/maintenance/analytics/spend-trend",
    { params },
  );
  return resp.data;
};

export const fetchAnalyticsFilterOptions = async () => {
  const resp = await api.get<AnalyticsFilterOptions>(
    "/maintenance/analytics/filter-options",
  );
  return resp.data;
};
