/** 维保数据看板 API 客户端（R33 四视图 + R34 / D-0024）。
 *
 * 数量与金额分离（R33 第 2 条，R34 延续）：无金额权的账号 summary/projects/parts
 * 响应里不出现任何金额字段（TS 类型上 valuation/references 为可选，页面按
 * amountsVisible 决定是否渲染金额卡/列，而不是拿 0 冒充）。
 *
 * R34 新形状：
 * - 价值保留比例（value_retention_ratio）是可空的本次试算参数 [0,1]；
 *   未设置（null）时估价为 awaiting_ratio（待试算），金额键整个不出现。
 * - 三域参考并列（采购/销售/领用已计成本），不相加；成本域是独立形状
 *   （单位成本 + 样本行 + 覆盖 + 来源），即使市场两域无样本也独立展示。
 * - M01 /orders（期间下单原 qty + 单数）、M03 /consumption（每日每项目已计
 *   消耗）与 /consumption/issues（领用明细下钻）。
 */
import { api } from "../api";

const BASE = "/maintenance/analytics/maintenance-dashboard";

/** 时间范围预设：默认 7d（近 7 天含今天，上海日历日）。 */
export type DashboardRangePreset =
  | "7d" | "15d" | "30d" | "half_year" | "year" | "all" | "custom";

export interface DashboardFilters {
  range?: DashboardRangePreset;
  date_from?: string;
  date_to?: string;
  /** 项目主键 CSV；空/未传 = 全部授权项目。 */
  project?: string;
  /** 业务类型（单值：缺省=all，或单个业务类型码；与 boss-board 同枚举）。 */
  business_type?: string;
  /** PN/描述关键词（orders/consumption/quantity-checks 端点无此参数）。 */
  q?: string;
  /** R34 价值保留比例（十进制串如 "0.5"；"0" 是合法显式值，缺省=待试算）。 */
  value_retention_ratio?: string;
}

export interface DashboardPagination {
  page: number;
  page_size: number;
  total: number;
}

/** 三域试算分值（R34-05：并列展示，缺域不出现、绝不求和）。 */
export interface DomainValuePayload {
  value_inc_tax: string;
  value_ex_tax: string;
}

/** 市场参考域（采购/销售）：可用 → 双税均价 + 样本元数据。 */
export interface DomainReferencePayload {
  available: boolean;
  inc_tax?: string;
  ex_tax?: string;
  sample_count?: number;
  order_count?: number;
  latest_order_date?: string | null;
  oldest_order_date?: string | null;
  low_confidence?: boolean;
  unavailable_reason?: string;
}

/** 领用已计成本参考域（R34-02）：单位成本 + 数量覆盖 + 来源证据。 */
export interface CostReferencePayload {
  available: boolean;
  unit_cost_inc_tax?: string;
  unit_cost_ex_tax?: string;
  sample_line_count?: number;
  issue_qty?: string;
  costed_qty?: string;
  uncosted_qty?: string;
  coverage_pct?: number | null;
  sources?: string[];
  latest_issue_date?: string | null;
  oldest_issue_date?: string | null;
  note?: string;
  unavailable_reason?: string;
}

/** 返件实收口径：maintenance_rkd_return_line line_status=active，按业务日入期。 */
export interface SummaryCards {
  receipt_qty: { total: string; part: string; machine: string };
  bad_receipt_qty: { total: string; scrap: string };
  /** 好件领用 = 实际领用量（未采集件况）。 */
  issued_qty: { total: string; note: string };
  /** 同期返还率 = 实收÷实际领用；分母≤0 为 null（无率，不是 0）。 */
  return_rate_pct: number | null;
  rate_numerator: string;
  rate_denominator: string;
  project_count: number;
  /** 金额卡：仅对通过利润门的调用方出现（admin/data_profit）。
   * 无比例（awaiting_ratio）时 bad_estimated_value 键不出现（待试算，不是 0）。 */
  valuation?: {
    state: "ready" | "awaiting_ratio" | "pending";
    estimated_bad_qty: string;
    pending_bad_qty: string;
    awaiting_ratio_bad_qty: string;
    coverage_pct: number | null;
    retention_ratio: string | null;
    domains: Record<string, DomainValuePayload>;
    policy_version: string;
    note: string;
    bad_estimated_value?: { inc_tax: string; ex_tax: string };
    finished_estimated_value?: { inc_tax: string; ex_tax: string };
  };
}

export interface SummaryResponse {
  range: { start: string | null; end: string };
  cards: SummaryCards;
  trend: { date: string; receipt_qty: string; issued_qty: string }[];
  condition_structure: { test_result: string; qty: string }[];
  top_projects: {
    project_id: string;
    project_code: string | null;
    display_name: string;
    receipt_qty: string;
    bad_receipt_qty: string;
    issued_qty: string;
  }[];
  notes: string[];
}

export interface ProjectValuation {
  state: "ready" | "awaiting_ratio" | "pending";
  pending_bad_qty: string;
  awaiting_ratio_bad_qty: string;
  coverage_pct: number | null;
  /** 仅设置了试算比例时出现（awaiting_ratio 时键不存在）。 */
  bad_estimated_value_inc_tax?: string;
  bad_estimated_value_ex_tax?: string;
}

export interface ProjectRow {
  project_id: string;
  project_code: string | null;
  display_name: string;
  business_type: string | null;
  receipt_qty: string;
  bad_receipt_qty: string;
  scrap_receipt_qty: string;
  issued_qty: string;
  /** 免返领用量：单独一列，不冲减任何量（R33 第 3 条）。 */
  exempt_issued_qty: string;
  pending_requirement_qty: string;
  unlinked_receipt_qty: string;
  return_rate_pct: number | null;
  rate_numerator: string;
  rate_denominator: string;
  no_return_note: string | null;
  valuation?: ProjectValuation;
}

export interface PartValuation {
  state: "ready" | "awaiting_ratio" | "pending";
  estimated_bad_qty: string;
  pending_bad_qty: string;
  awaiting_ratio_bad_qty: string;
  pending_reasons: Record<string, string>;
  coverage_pct: number | null;
  /** 以下键仅设置了试算比例且已估时出现。 */
  bad_estimated_value_inc_tax?: string;
  bad_estimated_value_ex_tax?: string;
  /** 成品按参考全值另列（不混入坏件主卡）；仅行级返回。 */
  finished_estimated_value_inc_tax?: string;
  finished_estimated_value_ex_tax?: string;
  domains?: Record<string, DomainValuePayload>;
}

export interface PartRow {
  part_id: number | null;
  pn: string;
  description: string | null;
  unit: string | null;
  receipt_qty: string;
  bad_receipt_qty: string;
  issued_qty: string;
  project_count: number;
  return_rate_pct: number | null;
  rate_numerator: string;
  rate_denominator: string;
  valuation?: PartValuation;
  /** 三域交易参考并列展示（采购/销售/领用已计成本）；仅金额可见账号返回。 */
  references?: {
    purchase: DomainReferencePayload;
    sales: DomainReferencePayload;
    cost: CostReferencePayload;
    chosen: "purchase" | "sales" | null;
    chosen_reason: string | null;
  };
}

/** R34 估价政策（D-0024）：无预设系数；比例是请求级试算参数。 */
export interface ValuationPolicy {
  policy_version: string;
  retention_ratio: string | null;
  retention_ratio_required: boolean;
  /** 快捷候选（十进制串，如 ["0.5","0.3","0.2"]）；非默认值，不代选。 */
  quick_options: string[];
  retention_rule: string;
  window_days: number;
  window_rule: string;
  half_life_days: string;
  weighting: string;
  three_domain_rule: string;
  unknown_condition_rule: string;
  finished_goods_rule: string;
  low_sample_order_count: number;
  price_priority: string;
  notes: string;
}

export interface ValuationResponse {
  range: { start: string | null; end: string };
  policy: ValuationPolicy;
  note: string;
  cards: {
    estimated_bad_qty: string;
    pending_bad_qty: string;
    awaiting_ratio_bad_qty: string;
    coverage_pct: number | null;
    /** 仅设置了试算比例时出现（否则待试算，不是 0）。 */
    bad_estimated_value?: { inc_tax: string; ex_tax: string };
  };
  rows: PartRow[];
  pagination: DashboardPagination;
}

export interface ReceiptLineRow {
  rkd_line_id: string;
  occurred_at: string | null;
  project_id: string;
  project_code: string | null;
  source_order_id: string | null;
  source_order_no: string | null;
  part_id: number | null;
  pn: string;
  description: string | null;
  receipt_kind: string;
  unit: string | null;
  test_result: string | null;
  qty: string;
  source: string;
  review_required: boolean;
  valuation?: {
    state: "ready" | "pending" | "awaiting_ratio";
    classification?: string;
    chosen?: "purchase" | "sales";
    factor?: string;
    price_inc_tax?: string;
    price_ex_tax?: string;
    value_inc_tax?: string;
    value_ex_tax?: string;
    reason?: string;
    domains?: Record<string, {
      available: boolean;
      price_inc_tax?: string;
      price_ex_tax?: string;
      unit_cost_inc_tax?: string;
      unit_cost_ex_tax?: string;
      value_inc_tax?: string;
      value_ex_tax?: string;
      reason?: string;
    }>;
  };
}

export interface QuantityCheckRow {
  source_order_id: string;
  order_no: string | null;
  project_id: string | null;
  project_code: string | null;
  display_name: string;
  order_date: string | null;
  demand_type: string | null;
  warehouse: string | null;
  demand_qty: string | null;
  received_qty: string | null;
  difference_qty: string | null;
  over_received_qty: string | null;
  basis_state: string;
  reason_codes: string[];
  undated_receipt_qty: string | null;
  no_return_note: string | null;
}

export interface QuantityChecksResponse {
  cards: {
    checked_demand_qty: string;
    received_qty: string;
    known_difference_qty: string;
    over_received_qty: string;
    pending_check_order_count: number;
    inactive_order_count: number;
    unassigned_receipt_qty: string;
    note: string;
  };
  rows: QuantityCheckRow[];
  unassigned_receipts: {
    project_id: string;
    project_code: string | null;
    display_name: string;
    received_qty: string;
  }[];
  pagination: DashboardPagination;
}

/** M01 期间下单：制单日期入期的有效需求单原 qty + 去重单数（纯数量域）。 */
export interface OrderRow {
  project_id: string;
  project_code: string | null;
  display_name: string;
  business_type: string | null;
  order_qty: string;
  order_count: number;
}

export interface OrdersResponse {
  range?: { start: string | null; end: string };
  cards: { order_qty: string; order_count: number };
  rows: OrderRow[];
}

/** M03 每日每项目已计消耗：数量全计；金额/覆盖/来源键仅金额可见时出现。 */
export interface ConsumptionDailyRow {
  issue_date: string;
  project_id: string;
  project_code: string | null;
  display_name: string;
  issued_qty: string;
  line_count: number;
  cost_amount_ex_tax?: string;
  cost_amount_inc_tax?: string;
  costed_qty?: string;
  uncosted_qty?: string;
  coverage_pct?: number | null;
  sources?: string[];
}

export interface ConsumptionResponse {
  range?: { start: string | null; end: string };
  cards: {
    issued_qty: string;
    cost_amount_ex_tax?: string;
    cost_amount_inc_tax?: string;
    uncosted_qty?: string;
    coverage_pct?: number | null;
    sources?: string[];
    note?: string;
  };
  rows: ConsumptionDailyRow[];
  pagination: DashboardPagination;
}

/** M03 领用明细行（日×项目下钻）：成本证据键仅金额可见时出现。 */
export interface ConsumptionIssueRow {
  issue_line_id: number;
  issue_id: number;
  issue_no: string | null;
  issue_date: string;
  project_id: string;
  project_code: string | null;
  display_name: string;
  part_id: number | null;
  pn: string;
  qty: string;
  cost_source?: string | null;
  algorithm_version?: string | null;
  reference_side?: string | null;
  cost_amount_ex_tax?: string;
  cost_amount_inc_tax?: string;
  unit_cost_ex_tax?: string;
  unit_cost_inc_tax?: string;
}

export interface DashboardOptions {
  range_presets: string[];
  default_range: string;
  default_date_rule: string;
  sorts: Record<string, string[]>;
  projects: {
    project_id: string;
    project_code: string | null;
    display_name: string;
    business_type: string | null;
  }[];
  amounts_visible: boolean;
  max_export_rows: number;
  export_kinds: string[];
  /** R34 政策说明（无预设系数；比例可空）；仅金额可见账号返回。 */
  valuation_policy?: ValuationPolicy;
}

/** projects/parts/valuation/quantity-checks 共用分页信封：rows + pagination。 */
export interface PagedRows<T> {
  range?: { start: string | null; end: string };
  rows: T[];
  pagination: DashboardPagination;
}

export const fetchDashboardOptions = () =>
  api.get<DashboardOptions>(`${BASE}/options`).then((r) => r.data);

export const fetchDashboardSummary = (params: DashboardFilters) =>
  api.get<SummaryResponse>(`${BASE}/summary`, { params }).then((r) => r.data);

export const fetchDashboardProjects = (
  params: DashboardFilters & { sort?: string; page?: number; page_size?: number },
) => api.get<PagedRows<ProjectRow>>(`${BASE}/projects`, { params }).then((r) => r.data);

export const fetchDashboardParts = (
  params: DashboardFilters & { sort?: string; page?: number; page_size?: number },
) => api.get<PagedRows<PartRow>>(`${BASE}/parts`, { params }).then((r) => r.data);

export const fetchDashboardValuation = (
  params: DashboardFilters & { sort?: string; page?: number; page_size?: number },
) => api.get<ValuationResponse>(`${BASE}/valuation`, { params }).then((r) => r.data);

export const fetchDashboardReceipts = (
  params: DashboardFilters & {
    test_result?: string;
    source?: string;
    sort?: string;
    page?: number;
    page_size?: number;
  },
) =>
  api
    .get<PagedRows<ReceiptLineRow>>(`${BASE}/receipts`, { params })
    .then((r) => r.data);

export const fetchDashboardQuantityChecks = (
  params: DashboardFilters & { sort?: string; page?: number; page_size?: number },
) =>
  api
    .get<QuantityChecksResponse>(`${BASE}/quantity-checks`, { params })
    .then((r) => r.data);

/** M01 期间下单（数量域，无金额键；不支持关键词 q）。 */
export const fetchDashboardOrders = (params: DashboardFilters) =>
  api.get<OrdersResponse>(`${BASE}/orders`, { params }).then((r) => r.data);

/** M03 每日每项目已计消耗（数量全计；金额键随利润门）。 */
export const fetchDashboardConsumption = (
  params: DashboardFilters & { page?: number; page_size?: number },
) => api.get<ConsumptionResponse>(`${BASE}/consumption`, { params }).then((r) => r.data);

/** M03 领用明细下钻：issue_date × project_id 定位到具体日×项目核对。 */
export const fetchDashboardConsumptionIssues = (
  params: DashboardFilters & {
    issue_date?: string;
    project_id?: string;
    page?: number;
    page_size?: number;
  },
) =>
  api
    .get<PagedRows<ConsumptionIssueRow>>(`${BASE}/consumption/issues`, { params })
    .then((r) => r.data);

export interface DashboardExportDownload {
  blob: Blob;
  filename: string;
}

export type DashboardExportKind =
  | "projects" | "parts" | "receipts" | "quantity_checks"
  | "orders" | "consumption_daily" | "consumption_issues";

/** 导出当前筛选集：CSV/XLSX；无金额权的服务端不含金额列（前端无需再裁剪）。 */
export const downloadDashboardExport = async (
  params: DashboardFilters & {
    kind: DashboardExportKind;
    format: "csv" | "xlsx";
    test_result?: string;
    source?: string;
  },
): Promise<DashboardExportDownload> => {
  const response = await api.get<Blob>(`${BASE}/export`, {
    params,
    responseType: "blob",
  });
  const disposition = String(response.headers["content-disposition"] ?? "");
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition)?.[1];
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition)?.[1];
  let filename = plain || "maintenance-dashboard.csv";
  if (encoded) {
    try {
      filename = decodeURIComponent(encoded.replace(/^"|"$/g, ""));
    } catch {
      // 非法 percent-encoding 时回退 ASCII filename。
    }
  }
  return { blob: response.data, filename };
};
