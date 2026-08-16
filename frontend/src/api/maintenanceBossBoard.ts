import { api } from "../api";

/**
 * 维保展示板 API 客户端（plan v1.3 §4.4/§4.5）。
 *
 * 六态信封（§4.6）：ready / partial / stale / not_imported / restricted / error。
 * not_imported / restricted / error 的 value 恒为 null —— 前端**任何状态都不渲染 0**
 * （铁律 5：未导入绝不伪装成 0）。渲染唯一入口是 components/maintenance/boss/StatCell。
 */
export type StatState =
  | "ready"
  | "partial"
  | "stale"
  | "not_imported"
  | "restricted"
  | "error";

export interface Stat<T = number | string | null> {
  state: StatState;
  value: T | null;
  as_of: string | null;
  unlinked?: number | null;
}

/** 「已知申请估算成本（含税）」五件套（§4.3）；缺价 → quality=incomplete「已知下限」。 */
export interface KnownCostValue {
  actual_amount: string | number;
  estimated_amount: string | number;
  known_amount: string | number;
  missing_lines: number;
  coverage_pct: number | null;
  quality: "actual_only" | "contains_estimate" | "incomplete";
}

export type KnownCostStat = Stat<KnownCostValue>;

export interface SourceHealth {
  readiness: "ready" | "partial" | "stale" | "not_imported";
  label: string;
  as_of: string | null;
  batch_id: string | null;
  uploaded_at: string | null;
  unlinked_rows: number | null;
}

export interface BoardHealth {
  sources: {
    wbdd: SourceHealth;
    ckd: SourceHealth;
    return_order: SourceHealth;
    rkd_inbound: SourceHealth;
  };
  stale_days: number;
}

export interface BoardWindow {
  from: string;
  to: string;
}

export interface BoardSummary {
  window: BoardWindow;
  orders_ytd: Stat<number>;
  lines_ytd: Stat<number>;
  known_apply_cost_inc_tax: KnownCostStat;
  prev_window: {
    window: BoardWindow;
    orders_ytd: Stat<number>;
    lines_ytd: Stat<number>;
    known_apply_cost_inc_tax: KnownCostStat;
  };
}

export interface AttentionItem {
  kind: string;
  project_id?: string | null;
  value?: unknown;
  evidence_link?: string | null;
}

export interface BoardAttention {
  items: AttentionItem[];
  registered_kinds: string[];
  /** M0-A 未拍板时为 "M0-A"：队列留空，不预置内容（不替业务拍板）。 */
  pending_decision: string | null;
}

export interface BoardProjectRow {
  project_id: string;
  project_code: string;
  display_name: string;
  lifecycle: "ongoing" | "ended" | "missing";
  has_activity_in_window: boolean;
  pre_delivery_order_count: number;
  orders_ytd: Stat<number>;
  lines_ytd: Stat<number>;
  known_apply_cost_inc_tax: KnownCostStat;
  shipped_qty: Stat<string | number>;
  returned_good_qty: Stat<string | number>;
  returned_bad_qty: Stat<string | number>;
}

export interface BoardProjects {
  rows: BoardProjectRow[];
  total: number;
  page: number;
  page_size: number;
  sort: string;
  window: BoardWindow;
}

export interface BoardOrderRow {
  source_order_id: string;
  order_no: string;
  order_date: string | null;
  data_status: string | null;
  project_raw: string | null;
  is_pre_delivery: boolean;
  line_count: number;
  known_apply_cost_inc_tax: KnownCostStat;
  /** 自报四列：与 facts **纯并排**，服务端不产出任何差异判定（铁律 3 / M4-4）。 */
  self_report: {
    head_demand_qty: string | number | null;
    head_purchase_qty: string | number | null;
    head_shipped_qty: string | number | null;
    head_returned_qty: string | number | null;
  };
  facts: {
    shipped_qty: Stat<string | number>;
    returned_good_qty: Stat<string | number>;
    returned_bad_qty: Stat<string | number>;
  };
}

export interface BoardOrders {
  rows: BoardOrderRow[];
  total: number;
  page: number;
  page_size: number;
}

/** PN 证据行：14 个流转状态列**原样**展示，不参与任何计算（铁律 3）。 */
export interface BoardLineRow {
  raw_line_id: string;
  pn_std: string | null;
  pn_raw: string | null;
  description: string | null;
  qty: string | number | null;
  return_qty: string | number | null;
  purchase_qty: string | number | null;
  purchased_qty: string | number | null;
  pending_purchase_qty: string | number | null;
  direct_ship_qty: string | number | null;
  warehouse_need_qty: string | number | null;
  warehouse_shipped_qty: string | number | null;
  supplied_qty: string | number | null;
  pending_supply_qty: string | number | null;
  returned_qty: string | number | null;
  pending_return_qty: string | number | null;
  consumed_qty: string | number | null;
  demand_pending_return_qty: string | number | null;
  change_warehouse_purchase_qty: string | number | null;
  return_old_part: string | null;
  serial_numbers: string | null;
  known_apply_cost_inc_tax: Stat<string | number>;
  cost_source: Stat<string>;
  confidence: Stat<string>;
}

export interface BoardLines {
  rows: BoardLineRow[];
  total: number;
  page: number;
  page_size: number;
}

const BASE = "/maintenance/boss-board";

export const getBoardHealth = () => api.get<BoardHealth>(`${BASE}/health`);

export const getBoardSummary = (params?: { from?: string; to?: string }) =>
  api.get<BoardSummary>(`${BASE}/summary`, { params });

export const getBoardAttention = (limit = 10) =>
  api.get<BoardAttention>(`${BASE}/attention`, { params: { limit } });

export const getBoardProjects = (params?: {
  page?: number;
  page_size?: number;
  lifecycle?: string;
  sort?: string;
  from?: string;
  to?: string;
}) => api.get<BoardProjects>(`${BASE}/projects`, { params });

export const searchBoardProjects = (body: {
  q: string;
  page?: number;
  page_size?: number;
  lifecycle?: string;
  sort?: string;
}) => api.post<BoardProjects>(`${BASE}/projects/search`, body);

export const getBoardProjectOrders = (
  projectId: string,
  params?: { page?: number; page_size?: number },
) => api.get<BoardOrders>(`${BASE}/projects/${encodeURIComponent(projectId)}/orders`, { params });

export const getBoardOrderLines = (
  sourceOrderId: string,
  params?: { page?: number; page_size?: number },
) => api.get<BoardLines>(`${BASE}/orders/${encodeURIComponent(sourceOrderId)}/lines`, { params });

/** 未归属桶的伪项目 ID（后端 §4.5 约定）。 */
export const UNASSIGNED_BUCKET = "unassigned";
