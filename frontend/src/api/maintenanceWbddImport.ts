import { domainApi, type ContextRequestTransport } from "../authz/domainTransport";
import { api } from "../api";

export interface WbddAddressReport {
  mode: "skip" | "upsert";
  /** 已解析单据的地址处理意图；并非数据库实际变更数。 */
  set: number;
  keep: number;
  clear: number;
  conflict: number;
  warnings: { order_no: string; data_rows: number[]; message: string }[];
}

/** WBDD 专用上传（plan v1.3 §4.1）。非 WBDD 文件后端 422 零写入。 */
export interface WbddImportReport {
  batch_id: number;
  file_hash: string;
  layout: "91" | "90" | "name";
  replayed: boolean;
  import_mode?: "skip" | "upsert";
  orders_inserted?: number;
  orders_updated?: number;
  /** 单头有效字段实际变化数（含新增）；null 或缺失表示未提供统计。 */
  orders_semantic_updated?: number | null;
  fact_rows_inserted?: number;
  fact_rows_updated?: number;
  fact_rows_skipped?: number;
  /** 旧 updated 是匹配已有行数；以下可选统计才描述有效值实际变化，null 表示未评估。 */
  fact_rows_semantic_updated?: number | null;
  fact_rows_unchanged?: number | null;
  /** 人工保护挡下不同源值的行数；可与 semantic_updated 重叠。 */
  fact_rows_protected?: number | null;
  protected_field_counts?: Record<string, number> | null;
  fact_rows_legacy_review?: number | null;
  fact_rows_error?: number;
  headless_orders?: number;
  headless_order_ids_sample?: string[];
  rows_display_issue?: number;
  address_report?: WbddAddressReport;
  snapshot_diff?: {
    missing_orders: number;
    missing_lines?: number;
    sample_order_nos: string[];
    window: { from: string; to: string } | null;
  };
  recompute?: Record<string, number>;
}

export interface WbddLatest {
  readiness: "ready" | "not_imported";
  as_of: string | null;
  orders_total: number | null;
  batch_id: number | null;
  uploaded_at: string | null;
  layout: string | null;
}

export const uploadWbdd = (file: File, idempotencyKey: string, capture?: ContextRequestTransport) => {
  const form = new FormData();
  form.append("file", file);
  return domainApi(capture).post<WbddImportReport>("/maintenance/wbdd-imports", form, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
};

export const getWbddLatest = ( capture?: ContextRequestTransport) =>
  domainApi(capture).get<WbddLatest>("/maintenance/wbdd-imports/latest");

/** D-27: immutable source snapshot + current facts produce reviewable candidates. */
export interface WbddMissingOrder {
  source_order_id: string;
  order_no: string;
  order_date: string | null;
  line_count: number;
  assigned_project_id: string | null;
  project_name?: string | null;
  linked_site_line_count?: number;
}

export interface WbddMissingLine {
  line_id: number;
  source_order_id: string;
  order_no: string;
  pn: string | null;
  qty: string | null;
  project_name?: string | null;
  linked_site_line_count: number;
}

export interface WbddMissing {
  readiness?: "ready" | "not_imported" | "snapshot_required" | "blocked";
  batch_id: number | null;
  uploaded_at: string | null;
  window?: { from: string; to: string } | null;
  missing_count: number;
  /** true＝清单被截断（只返回前 1000 条），页面要提示。 */
  truncated: boolean;
  missing_orders: WbddMissingOrder[];
  missing_lines?: WbddMissingLine[];
  missing_line_count?: number;
  total_candidate_count?: number;
  candidate_digest?: string | null;
  coverage?: {
    daily_windows: { date: string; from_sequence: number; to_sequence: number }[];
    source_order_count: number;
    blocked_order_count: number;
  };
  warnings?: string[];
  /** 库内同日期窗的活跃单总数（判断误报用）。 */
  db_active_in_window?: number | null;
  /** 缺失占比 = missing_count / db_active_in_window（2026-08-24 起仅展示，
   *  不再作为批量作废的禁用开关——可 100% 全量跟随修改）。 */
  missing_ratio?: number | null;
}

/**
 * 差异清单基于最近上传的不可变原件成员；历史缺少成员时要求重新上传。
 * 执行确认必须回传本次 candidate_digest，不能绕回普通手工作废接口。
 */
export const getWbddMissing = ( capture?: ContextRequestTransport) =>
  domainApi(capture).get<WbddMissing>("/maintenance/wbdd-imports/latest/missing");

export interface WbddReconcileRequest {
  expected_digest: string;
  source_order_ids: string[];
  line_ids: number[];
  reason: string;
  idempotency_key: string;
}

export interface WbddReconcileResult {
  voided_orders: number;
  voided_lines: number;
  voided_site_lines: number;
  voided_site_issues: number;
  replayed?: boolean;
}

export const reconcileWbdd = (batchId: number, payload: WbddReconcileRequest, capture?: ContextRequestTransport) =>
  domainApi(capture).post<WbddReconcileResult>(`/maintenance/wbdd-imports/${batchId}/reconcile`, payload);

/** 已应用单据头行的项目重解析（M4-3：先传 RKD 后建归属也能关联）。 */
export const relinkDocProjects = ( capture?: ContextRequestTransport) =>
  domainApi(capture).post<{ relinked: number; still_unlinked: number; out_of_scope: number }>(
    "/maintenance/doc-imports/relink-projects",
  );
