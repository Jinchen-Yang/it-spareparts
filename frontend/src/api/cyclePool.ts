import { api } from "../api";

/**
 * 坏件循环待修池接口（D-0058 / cycle-repair-api.md）。
 * 用户接口走 snake_case。旧的逐件工位动作已删除，不要在这里加回来。
 */

export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 维修员与主管的待修池行。只有这四个字段。 */
export interface RepairPoolItem {
  part_id: number;
  pn: string;
  description: string | null;
  pending_qty: number;
}

/** 提交成功体。维修员列表在此基础上另有作废字段，仍不含来源和 delivery_id。 */
export interface RepairRecord {
  record_id: string;
  record_no: string;
  part_id: number;
  pn: string;
  description: string | null;
  fixed_qty: number;
  scrap_qty: number;
  status: string;
  version: number;
  created_at: string;
  operator: string;
  voided_at?: string | null;
  void_reason?: string | null;
}

export interface RepairSource {
  receipt_id: string;
  qty: number;
  occurred_at: string | null;
  line_qty: number;
  test_result: string | null;
  project_id: string | null;
}

/** 主管明细里的出站意图。unit_id 是发给商城的正整数，不是 UUID。 */
export interface RepairSellIntent {
  delivery_id: string;
  unit_id: number;
  status: string;
  qty: number;
  attempts: number;
  block_code: string | null;
  last_error_code: string | null;
  receipt_status: string | null;
  next_attempt_at: string | null;
  shop_closeout: string | null;
}

export interface RepairRecordDetail extends RepairRecord {
  sources: RepairSource[];
  sell_intent: RepairSellIntent | null;
}

export interface RepairSummaryItem {
  part_id: number;
  pn: string;
  description: string | null;
  pending_qty: number;
  shop_qty: number;
  scrap_qty: number;
}

export interface SubmitRepairBody {
  part_id: number;
  fixed_qty: number;
  scrap_qty: number;
}

export interface VoidRepairBody {
  expected_version: number;
  reason: string;
  shop_stock_manually_reduced: boolean;
}

export interface AttestShopBody {
  expected_version: number;
}

export interface RepairListQuery {
  q?: string;
  page?: number;
  page_size?: number;
}

export type RepairSellIntentResponse =
  | RepairSellIntent
  | { record_id: string; status: "not_queued" };

function query(params: object): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== ""),
  );
}

function idempotency(key: string) {
  return { headers: { "Idempotency-Key": key } };
}

function recordPath(recordId: string, suffix = ""): string {
  return `/cycle/repair-records/${encodeURIComponent(recordId)}${suffix}`;
}

export async function listRepairPool(params: RepairListQuery = {}): Promise<PageResult<RepairPoolItem>> {
  const { data } = await api.get<PageResult<RepairPoolItem>>("/cycle/pool", { params: query(params) });
  return data;
}

export async function submitRepairRecord(body: SubmitRepairBody, idempotencyKey: string): Promise<RepairRecord> {
  const { data } = await api.post<RepairRecord>("/cycle/repair-records", body, idempotency(idempotencyKey));
  return data;
}

export async function listRepairRecords(params: RepairListQuery & {
  status?: "active" | "voided";
  part_id?: number;
} = {}): Promise<PageResult<RepairRecord>> {
  const { data } = await api.get<PageResult<RepairRecord>>("/cycle/repair-records", { params: query(params) });
  return data;
}

export async function getRepairRecord(recordId: string): Promise<RepairRecordDetail> {
  const { data } = await api.get<RepairRecordDetail>(recordPath(recordId));
  return data;
}

export async function voidRepairRecord(
  recordId: string,
  body: VoidRepairBody,
  idempotencyKey: string,
): Promise<RepairRecord> {
  const { data } = await api.post<RepairRecord>(recordPath(recordId, "/void"), body, idempotency(idempotencyKey));
  return data;
}

/** 主管单独看推送状态。页面明细用记录详情里的 sell_intent，这条保留给契约上的独立读。 */
export async function getRepairSellIntent(recordId: string): Promise<RepairSellIntentResponse> {
  const { data } = await api.get<RepairSellIntentResponse>(recordPath(recordId, "/sell-intent"));
  return data;
}

export async function listRepairSummary(params: RepairListQuery & {
  part_ids?: string;
} = {}): Promise<PageResult<RepairSummaryItem>> {
  const { data } = await api.get<PageResult<RepairSummaryItem>>("/cycle/summary", { params: query(params) });
  return data;
}

export async function attestShopAdjustment(
  recordId: string,
  body: AttestShopBody,
  idempotencyKey: string,
): Promise<RepairRecord> {
  const { data } = await api.post<RepairRecord>(
    recordPath(recordId, "/attest-shop-adjustment"),
    body,
    idempotency(idempotencyKey),
  );
  return data;
}
