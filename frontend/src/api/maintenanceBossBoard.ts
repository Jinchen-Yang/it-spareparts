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
  /** null = 没有有效需求明细，不能把 SQL 聚合单位元 0 当成真实成本。 */
  known_amount: string | number | null;
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

/** 卡片三态（#35/#43）：<80% 正常 / 80–100% 提醒 / >100% 报警。 */
export type CardStatus = "normal" | "warning" | "alert";

export type BoardProjectLifecycle = "ongoing" | "ended" | "missing" | "all";
export type BoardProjectSort = "attention" | "orders" | "name" | "known_cost" | "cost_ratio";

export interface BoardProjectRow {
  project_id: string;
  project_code: string;
  display_name: string;
  /** 同一 canonical 项目的历史/来源名称，仅用于展示和搜索。 */
  aliases?: string[];
  lifecycle: "ongoing" | "ended" | "missing";
  /** 维保期限主数据（#51）：WBDD 聚合/名称解析回填，台账导入后为台账值。 */
  period_from: string | null;
  period_to: string | null;
  /** 已归档但仍带单：留在列表里保住母集恒等式，不让单据凭空消失。 */
  is_archived: boolean;
  /** XSDD 销售订单号＝归属判定依据（#45）；多合同项目返回多个。 */
  contract_nos: string[];
  project_manager: string | null;
  /** 销售（2026-08-21 客户反馈）：台账 salesperson 优先，XSDD 需求单众数兜底。 */
  salesperson: string | null;
  contract_amount_inc_tax: Stat<string | number>;
  /** #51 诚实标注：合同额来自 XSDD 回退层时，共用单（金额跨项目重复）/缺单（被低估）。 */
  contract_shared: boolean;
  contract_incomplete: boolean;
  known_apply_cost_ex_tax: Stat<string | number>;
  /** 维保备件采购数＝库房发货＋直采直发（#41 业务指定公式）。 */
  procured_qty: Stat<string | number>;
  collection_preview_inc_tax: Stat<string | number>;
  /** 报销成本（已批准报销含税，2026-08-22 上卡）。 */
  expense_cost_inc_tax: Stat<string | number>;
  /** 已领用成本（现场领用已知含税，2026-08-22 上卡）。 */
  requisition_cost_inc_tax: Stat<string | number>;
  cost_ratio_pct: Stat<string | number>;
  /** 三态＝进度条颜色（#43）；算不出来是 null，不拿绿色冒充健康。 */
  card_status: CardStatus | null;
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

/**
 * 项目清单导出的字段目录由服务端按当前账号权限下发。前端不得自行补充 key，
 * 也不得把未返回的数据库字段渲染成可选项。
 */
export interface BoardProjectExportField {
  key: string;
  label: string;
  group: string;
  default_selected: boolean;
}

export interface BoardProjectExportOptions {
  fields: BoardProjectExportField[];
  default_fields: string[];
}

export interface BoardProjectExportInput {
  fields: string[];
  q?: string;
  lifecycle?: BoardProjectLifecycle;
  card_status?: CardStatus;
  sort?: BoardProjectSort;
}

export interface BoardProjectExportDownload {
  blob: Blob;
  filename: string;
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
  /** facts 的口径：'project' = 项目级卷积（非本单数量）；null = 无项目口径（未归属桶）。 */
  facts_scope: "project" | null;
}

export interface BoardOrders {
  rows: BoardOrderRow[];
  total: number;
  page: number;
  page_size: number;
}

/** 互通池归属：in_pool=null 表示 PN 未标准化、无法判断（不等于「不在池」）。 */
export interface PoolMembership {
  in_pool: boolean | null;
  pool_name: string | null;
  pool_status: "active" | "archived" | null;
}

/** PN 证据行：14 个流转状态列**原样**展示，不参与任何计算（铁律 3）。 */
export interface BoardLineRow {
  raw_line_id: string;
  pn_std: string | null;
  pn_raw: string | null;
  pool: PoolMembership;
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
  unit_cost_ex_tax: Stat<string | number>;
  unit_cost_inc_tax: Stat<string | number>;
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
  card_status?: CardStatus;
  from?: string;
  to?: string;
}) => api.get<BoardProjects>(`${BASE}/projects`, { params });

export const searchBoardProjects = (body: {
  q: string;
  page?: number;
  page_size?: number;
  lifecycle?: string;
  sort?: string;
  card_status?: CardStatus;
}) => api.post<BoardProjects>(`${BASE}/projects/search`, body);

export const getBoardProjectExportOptions = () =>
  api.get<BoardProjectExportOptions>(`${BASE}/projects/export/options`);

/** 下载当前筛选命中的全部项目（不是只下载当前已经滚动加载的卡片）。 */
export const downloadBoardProjectsExport = async (
  body: BoardProjectExportInput,
): Promise<BoardProjectExportDownload> => {
  const response = await api.post<Blob>(`${BASE}/projects/export`, body, {
    responseType: "blob",
  });
  const disposition = String(response.headers["content-disposition"] ?? "");
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition)?.[1];
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition)?.[1];
  let filename = plain || "maintenance-projects.xlsx";
  if (encoded) {
    try {
      filename = decodeURIComponent(encoded.replace(/^"|"$/g, ""));
    } catch {
      // 非法 percent-encoding 时使用安全的 ASCII filename / 固定回退名。
    }
  }
  return { blob: response.data, filename };
};

/** 详情页按稳定 ID 取聚合卡，避免名称模糊搜索第 1 页漏掉同名项目。 */
export const getBoardProject = (projectId: string) =>
  api.get<BoardProjectRow>(`${BASE}/projects/${encodeURIComponent(projectId)}`);

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
