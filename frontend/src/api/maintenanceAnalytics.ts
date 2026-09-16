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
  part_id: number;
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

export const fetchAnalyticsFilterOptions = async () => {
  const resp = await api.get<AnalyticsFilterOptions>(
    "/maintenance/analytics/filter-options",
  );
  return resp.data;
};
