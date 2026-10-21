/** 维保数据看板 MVP API 客户端（R33 / D-0022，T1–T4）。
 *
 * 数量与金额分离（R33 第 2 条）：无金额权的账号 summary/projects/parts/receipts
 * 响应里不出现任何金额字段（TS 类型上 valuation/references 为可选，页面按
 * amountsVisible 决定是否渲染金额卡/列，而不是拿 0 冒充）。
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
  /** all 或业务类型码 CSV（与 boss-board 同枚举）。 */
  business_type?: string;
  /** PN/描述关键词。 */
  q?: string;
}

export interface DashboardPagination {
  page: number;
  page_size: number;
  total: number;
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
  /** 金额卡：仅对通过利润门的调用方出现（admin/data_profit）。 */
  valuation?: {
    state: string;
    bad_estimated_value: { inc_tax: string; ex_tax: string };
    finished_estimated_value: { inc_tax: string; ex_tax: string };
    estimated_bad_qty: string;
    pending_bad_qty: string;
    coverage_pct: number | null;
    policy_version: string;
    note: string;
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
  bad_estimated_value_inc_tax: string;
  bad_estimated_value_ex_tax: string;
  pending_bad_qty: string;
  coverage_pct: number | null;
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
  valuation?: {
    bad_estimated_value_inc_tax: string;
    bad_estimated_value_ex_tax: string;
    estimated_bad_qty: string;
    pending_bad_qty: string;
    pending_reasons: Record<string, string>;
    coverage_pct: number | null;
  };
  /** 双域交易参考并列展示；仅金额可见账号返回。 */
  references?: {
    purchase: DomainReferencePayload;
    sales: DomainReferencePayload;
    chosen: "purchase" | "sales" | null;
    chosen_reason: string | null;
  };
}

export interface ValuationPolicy {
  policy_version: string;
  effective_from: string;
  condition_factors: Record<string, string>;
  unknown_condition_rule: string;
  low_sample_order_count: number;
  price_priority: string;
  notes: string;
}

export interface ValuationResponse {
  range: { start: string | null; end: string };
  policy: ValuationPolicy;
  note: string;
  cards: {
    bad_estimated_value: { inc_tax: string; ex_tax: string };
    estimated_bad_qty: string;
    pending_bad_qty: string;
    coverage_pct: number | null;
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
    state: "ready" | "pending";
    chosen?: "purchase" | "sales";
    factor?: string;
    price_inc_tax?: string;
    price_ex_tax?: string;
    value_inc_tax?: string;
    value_ex_tax?: string;
    reason?: string;
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
  /** 系数政策说明；仅金额可见账号返回。 */
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

export interface DashboardExportDownload {
  blob: Blob;
  filename: string;
}

/** 导出当前筛选集：CSV/XLSX；无金额权的服务端不含金额列（前端无需再裁剪）。 */
export const downloadDashboardExport = async (
  params: DashboardFilters & {
    kind: "projects" | "parts" | "receipts" | "quantity_checks";
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
