import { api } from "../api";
import type { MaintenanceOrderContact } from "./maintenanceOrderContact";

export interface MaintenanceDemandReference {
  kind: string;
  label: string;
  reference_id: string;
}

export interface MaintenanceDemandSummary {
  source_order_id: string;
  order_no: string;
  order_date: string | null;
  project: string | null;
  project_raw: string | null;
  linked_sales_order_no: string | null;
  line_count: number;
  downstream_references: MaintenanceDemandReference[];
  version_digest: string;
}

export interface MaintenanceDemandSearchInput {
  q?: string;
  page: number;
  page_size: number;
  /** 「含已作废」视图（#268 场景一）；默认 false 只看有效单。 */
  include_voided?: boolean;
}

export interface MaintenanceDemandSearchResult {
  items: MaintenanceDemandSearchRow[];
  total: number;
  page: number;
  page_size: number;
}

/** Contact fields belong to search reads, never deletion-intent snapshots. */
export interface MaintenanceDemandSearchRow extends MaintenanceDemandSummary, MaintenanceOrderContact {}

export type MaintenanceDemandDeleteIntentStatus =
  | "reviewed"
  | "armed_wait"
  | "executed"
  | "cancelled"
  | "conflicted"
  | "expired";

export interface MaintenanceDemandDeleteIntent {
  intent_id: string;
  status: MaintenanceDemandDeleteIntentStatus;
  selection_digest: string;
  reason: string;
  operated_by: string;
  header_count: number;
  line_count: number;
  created_at: string;
  not_before: string | null;
  expires_at: string;
  executed_at: string | null;
  items: MaintenanceDemandSummary[];
  result: MaintenanceDemandDeleteResult | null;
}

export interface MaintenanceDemandDeleteResult {
  intent_id: string;
  status: "executed";
  header_count: number;
  line_count: number;
  source_order_ids: string[];
  executed_at: string;
}

export interface MaintenanceDemandDeleteIntentInput {
  source_order_ids: string[];
  reason: string;
  idempotency_key: string;
}

/** #265 冻结契约：一键批量作废（跳过两阶段 arm 窗口）。 */
export interface MaintenanceDemandVoidFastInput {
  source_order_ids: string[];
  reason: string;
  idempotency_key?: string;
}

export interface MaintenanceDemandVoidFastResult {
  voided: number;
  results: {
    source_order_id: string;
    order_no: string;
    /** already_voided＝幂等命中（重复点击安全），不算错误。 */
    status: "voided" | "already_voided";
  }[];
}

export const searchMaintenanceDemands = (body: MaintenanceDemandSearchInput) =>
  api.post<MaintenanceDemandSearchResult>("/maintenance/demands/search", body);

/**
 * 一键批量作废（#265）：单事务墓碑 + 停用挂靠 + 审计。
 * 409＝任一单版本变化整批零删除（响应带冲突单号）；404＝未知单整批零写入。
 */
export const voidFastMaintenanceDemands = (body: MaintenanceDemandVoidFastInput) =>
  api.post<MaintenanceDemandVoidFastResult>("/maintenance/demands/void-fast", body);

/** 恢复已作废单（ADR-0003：清派生成本 + 待重算，由后端负责）。后端强制 reason 非空 + admin。 */
export const restoreMaintenanceDemand = (sourceOrderId: string, reason: string) =>
  api.post(`/maintenance/demands/${encodeURIComponent(sourceOrderId)}/restore`, { reason });

export const createMaintenanceDemandDeleteIntent = (
  body: MaintenanceDemandDeleteIntentInput,
) => api.post<MaintenanceDemandDeleteIntent>(
  "/maintenance/demands/delete-intents",
  body,
);

export const armMaintenanceDemandDeleteIntent = (intentId: string, digest: string) =>
  api.post<MaintenanceDemandDeleteIntent>(
    `/maintenance/demands/delete-intents/${intentId}/arm`,
    { digest },
  );

export const executeMaintenanceDemandDeleteIntent = (intentId: string, digest: string) =>
  api.post<MaintenanceDemandDeleteResult>(
    `/maintenance/demands/delete-intents/${intentId}/execute`,
    { digest },
  );

export const cancelMaintenanceDemandDeleteIntent = (intentId: string, digest: string) =>
  api.post<MaintenanceDemandDeleteIntent>(
    `/maintenance/demands/delete-intents/${intentId}/cancel`,
    { digest },
  );

// ---------- v1.36 Phase E：需求单行页面直改/直建 ----------

export interface DemandLinePatchResult {
  changed: boolean;
  raw_line_id: string;
  qty: string | null;
  return_qty: string | null;
  serial_numbers: string | null;
  description: string | null;
  pn_raw: string | null;
  pn_std: string | null;
  edited_source: string;
  manual_override: Record<string, { value: unknown; source_value: unknown; updated_by: string; updated_at: string }>;
}

export const patchDemandLine = (
  rawLineId: string,
  updates: Record<string, unknown>,
  reason: string,
) => api.patch<DemandLinePatchResult>(
  `/maintenance/demands/lines/${encodeURIComponent(rawLineId)}`,
  { updates, reason },
);

export const createDemandLine = (input: {
  order_date: string;
  project_id: string;
  pn_std: string;
  qty: number;
  return_qty?: number;
  serial_numbers?: string | null;
  description?: string | null;
  reason: string;
}) => api.post<DemandLinePatchResult & { order_no: string }>(
  "/maintenance/demands/lines", input,
);

export interface DemandLineRow {
  raw_line_id: string;
  order_raw_id: string;
  order_no: string | null;
  line_no: number | null;
  part_id: number | null;
  pn_std: string | null;
  pn_raw: string | null;
  description: string | null;
  qty: string | null;
  return_qty: string | null;
  serial_numbers: string | null;
  edited_source: string;
  manual_override: Record<string, { value: unknown; source_value: unknown; updated_by: string; updated_at: string }>;
  is_active: boolean;
}

export const listDemandLines = (sourceOrderId: string) =>
  api.get<{ items: DemandLineRow[] }>(
    `/maintenance/demands/orders/${encodeURIComponent(sourceOrderId)}/lines`,
  );

export const clearDemandLineOverride = (
  rawLineId: string,
  fieldName: string,
  reason: string,
) => api.post<DemandLinePatchResult>(
  `/maintenance/demands/lines/${encodeURIComponent(rawLineId)}/clear-override`,
  { field_name: fieldName, reason },
);
