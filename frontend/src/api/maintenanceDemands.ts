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

/**
 * 手工需求行写操作（PATCH / clear / create）的公共结果形状：
 * 后端回整份行快照 + OCC digest（写后新版本 token，供下一次编辑携带）。
 * create 额外带 order_no（手工单会新建单头）与 replayed（幂等重放命中）。
 */
export interface DemandLineWriteResult {
  changed: boolean;
  /** 写后行快照的 sha256 OCC token（下次编辑/撤销必填）。 */
  digest: string;
  raw_line_id: string;
  part_id: number | null;
  qty: string | null;
  return_qty: string | null;
  serial_numbers: string | null;
  description: string | null;
  pn_raw: string | null;
  pn_std: string | null;
  edited_source: string;
  manual_override: Record<string, { value: unknown; source_value: unknown; updated_by: string; updated_at: string }>;
  order_no?: string;
  replayed?: boolean;
}

/**
 * 页面直改一条明细行（v1.36 Phase E）。
 * expected_digest 必填：读行（GET lines）时的 OCC token；行已被他人改过 → 409，
 * 由调用方给「重新加载最新数据」入口，绝不静默重试覆盖。
 */
export const patchDemandLine = (
  rawLineId: string,
  updates: Record<string, unknown>,
  reason: string,
  expectedDigest: string,
) => api.patch<DemandLineWriteResult>(
  `/maintenance/demands/lines/${encodeURIComponent(rawLineId)}`,
  { updates, reason, expected_digest: expectedDigest },
);

export interface DemandLineCreateInput {
  /** YYYY-MM-DD（服务层 fromisoformat 解析）。 */
  order_date: string;
  project_id: string;
  pn_std: string;
  qty: number;
  return_qty?: number;
  serial_numbers?: string | null;
  description?: string | null;
  reason: string;
  /**
   * 幂等键（必填，8–128 字符 [A-Za-z0-9._:-]+）：网络丢响应重试同 payload
   * 复用同 key（后端比对完整请求指纹），改内容必须换新 key。
   */
  idempotency_key: string;
}

export const createDemandLine = (input: DemandLineCreateInput) =>
  api.post<DemandLineWriteResult>("/maintenance/demands/lines", input);

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
  /** OCC token：编辑/撤销时作为 expected_digest 回传（服务端 409 校验）。 */
  digest: string;
}

export const listDemandLines = (sourceOrderId: string) =>
  api.get<{ items: DemandLineRow[] }>(
    `/maintenance/demands/orders/${encodeURIComponent(sourceOrderId)}/lines`,
  );

/**
 * 撤销一个字段的 override（恢复氚云原值 source_value 快照）。
 * expected_digest 必填（OCC）；PN 是一组身份，后端成对恢复 pn_std/pn_raw/part_id。
 */
export const clearDemandLineOverride = (
  rawLineId: string,
  fieldName: string,
  reason: string,
  expectedDigest: string,
) => api.post<DemandLineWriteResult>(
  `/maintenance/demands/lines/${encodeURIComponent(rawLineId)}/clear-override`,
  { field_name: fieldName, reason, expected_digest: expectedDigest },
);
