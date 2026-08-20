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
  sort?: string;
  page?: number;
  page_size?: number;
}

export const fetchPnRanking = async (params: PnRankingParams) => {
  const resp = await api.get<PnRanking>("/maintenance/analytics/pn-ranking", {
    params,
  });
  return resp.data;
};
