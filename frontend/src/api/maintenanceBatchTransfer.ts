import { api } from "../api";
import type {
  BoardProjectLifecycle,
  BoardProjectSort,
  CardStatus,
} from "./maintenanceBossBoard";

/**
 * 维保批量导入/下载的可调整 API 边界。
 *
 * 后端实现尚未落库时也只需在这里调整路径或 DTO；页面不解析 Excel，也不会把
 * 浏览器端看到的 canonical values / mapping 在 apply 时重新提交给服务端。
 */
export const MAINTENANCE_BATCH_TRANSFER_BASE = "/maintenance/project-batch-transfer";

/**
 * ambiguous 只表示项目归属有多个候选；D-16 的 fail-closed / 冲突行（seed_required、snapshot_voided、
 * cross_file_same_contract、cumulative_unverifiable、constituent_blocked、collection_not_monotonic、
 * order_level_fail_closed、receipt_conflict、receipt_voided_upstream）一律是 invalid，计入「无效」筛选与计数。
 */
export type MaintenanceBatchMatchState =
  | "matched"
  | "ambiguous"
  | "unmatched"
  | "invalid";

export type MaintenanceBatchRowStatus =
  | "ready"
  | "unchanged"
  | "needs_review"
  | "blocked";

export type MaintenanceBatchAction =
  | "create_project"
  | "create_contract"
  | "update_contract"
  | "upsert_collection_snapshot"
  /** D-16：覆盖既有已确认累计——可勾选但默认不勾，用户须逐行确认。 */
  | "update_collection_snapshot"
  /** D-16：累计不变，只把新收款登记入台账。 */
  | "record_receipts"
  | "skip"
  | "block";

export type MaintenanceBatchMatchStrategy =
  | "exact_contract_no"
  | "explicit_project_id"
  | "candidate"
  | "none";

export interface MaintenanceBatchIssue {
  code: string;
  message: string;
  field?: string | null;
}

export interface MaintenanceBatchDetectedField {
  source_column: string;
  canonical_field: string | null;
  canonical_label: string | null;
  confidence: "exact" | "alias" | "inferred" | "unmapped";
  required: boolean;
  metric_basis?: string | null;
}

export interface MaintenanceBatchMappingConflict {
  source_columns: string[];
  canonical_field?: string | null;
  message: string;
}

export interface MaintenanceBatchCandidate {
  project_id: string;
  project_name: string;
  contract_id?: string | null;
  contract_no?: string | null;
  score?: number | null;
  reason?: string | null;
}

export interface MaintenanceBatchPreviewFile {
  file_id: string;
  filename: string;
  import_kind: "sales_contract" | "receipt" | string;
  source_sha256: string;
  detected_sheet: string | null;
  header_rows: number[];
  detected_fields: MaintenanceBatchDetectedField[];
  mapping_conflicts: MaintenanceBatchMappingConflict[];
}

export interface MaintenanceBatchPreviewRow {
  row_key: string;
  file_id: string;
  filename: string;
  detected_sheet?: string | null;
  source_row: number;
  /**
   * 仅用于人工预览；apply 请求绝不回传。收款单 receipt_conflict 行额外带本文件值
   * receipt_no / receipt_date / actual_amount（裁决体取这里，不解析文案）。
   */
  canonical: Record<string, string | number | boolean | null>;
  normalized_key: string | null;
  idempotency_key: string;
  matched_project_id: string | null;
  matched_project_name: string | null;
  matched_contract_id: string | null;
  match_strategy: MaintenanceBatchMatchStrategy;
  candidate_count: number;
  candidates?: MaintenanceBatchCandidate[];
  match_state: MaintenanceBatchMatchState;
  action: MaintenanceBatchAction;
  row_status: MaintenanceBatchRowStatus;
  /** 为 true 的行默认不勾选（覆盖既有累计），必须由用户显式勾选。 */
  requires_confirmation?: boolean;
  /**
   * 必须与本行一起勾选的行键：更早月份的累计行、本行新建所依赖的覆盖行（D-16）。
   * 只在后端应用时会硬拒的行上出现（create / update / record_receipts），只勾本行不勾依赖行
   * 后端整批拒绝；前端默认勾选时排除依赖未满足的行，覆盖行永不随依赖自动勾上。
   */
  depends_on_row_keys?: string[];
  /**
   * 人类可读提示（需同勾更早月份 / 依赖覆盖行 / 需先建账 / 累计无法核验 / 台账冲突 / 已在台账），
   * 须可见渲染而非悬停。
   */
  hint_messages?: string[];
  /**
   * 既有值：覆盖行是 cumulative_amount / status（confirmed / unconfirmed，决定「覆盖已确认 / 未确认累计」）
   * / source / import_batch_id / updated_at；receipt_conflict / receipt_voided_upstream 行是台账值
   * receipt_no / receipt_date / actual_amount。
   */
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  delta?: Record<string, unknown> | null;
  /** record_receipts 行带 info 码 record_receipts（『累计不变，只把 N 笔新收款登记入台账』），标签由它驱动。 */
  warnings: MaintenanceBatchIssue[];
  errors: MaintenanceBatchIssue[];
}

export interface MaintenanceBatchCounts {
  total: number;
  matched: number;
  ambiguous: number;
  unmatched: number;
  invalid: number;
  ready: number;
  /** 收款单已在台账、本次跳过的行数（D-16）。 */
  known?: number;
  /** 收款单号与台账金额/日期不一致、需人工裁决的行数（D-16）。 */
  receipt_conflicts?: number;
  /** 覆盖既有累计、需显式勾选的行数（D-16）。 */
  updates?: number;
}

export interface MaintenanceBatchPreviewResponse {
  schema_version: string;
  preview_id?: string | null;
  preview_token: string;
  payload_hash: string;
  data_version: string | number;
  expires_at: string;
  files: MaintenanceBatchPreviewFile[];
  rows: MaintenanceBatchPreviewRow[];
  summary: MaintenanceBatchCounts;
  can_apply: boolean;
}

export interface MaintenanceBatchApplyRequest {
  preview_token: string;
  payload_hash: string;
  data_version: string | number;
  row_keys: string[];
}

export interface MaintenanceBatchApplyRowResult {
  row_key: string;
  source_file: string;
  source_sheet: string | null;
  source_row: number;
  status: "applied" | "skipped" | "failed" | "conflict" | "not_applied";
  action: MaintenanceBatchAction;
  project_id?: string | null;
  contract_id?: string | null;
  entity_id?: string | null;
  message: string | null;
  error_code?: string | null;
  before_version?: string | number | null;
  after_version?: string | number | null;
  /** 回款源行聚合到月度快照时的可追踪键。 */
  aggregate_key?: string | null;
  project_contract_id?: string | null;
  report_month?: string | null;
  /** 覆盖回执：原值→新值、原来源/时间（D-16）。 */
  before_amount?: string | null;
  after_amount?: string | null;
  previous_source?: string | null;
  previous_import_batch_id?: string | null;
  previous_updated_at?: string | null;
  receipts_recorded?: number | null;
}

export interface MaintenanceBatchApplyResponse {
  batch_id: string;
  status: "done" | "partial" | "failed" | string;
  applied: number;
  skipped: number;
  blocked: number;
  project_ids: string[];
  invalidated_projects: string[];
  audit_ref: string;
  rows: MaintenanceBatchApplyRowResult[];
}

/** 台账冲突人工裁决（D-16 / REQ #56 #57）：以本文件值为准，作废台账原行并重建；绝不自动求和或猜重。 */
export interface MaintenanceBatchReceiptRulingRequest {
  contract_no: string;
  receipt_no: string;
  /** YYYY-MM-DD，取自文件值。 */
  receipt_date: string;
  /** 十进制字符串，取自文件值。 */
  actual_amount: string;
  /** 裁决原因，1~1000 字，必填。 */
  reason: string;
}

export interface MaintenanceBatchReceiptRulingMonth {
  report_month: string;
  current_cumulative: string | null;
  derived_cumulative: string | null;
}

export interface MaintenanceBatchReceiptRulingResponse {
  ruling_id: string;
  superseded_receipt_id: string;
  new_receipt_id: string;
  /** 裁决后累计口径变化的月份；快照不自动改写，下次预览以覆盖行呈现。 */
  affected_months: MaintenanceBatchReceiptRulingMonth[];
}

export interface MaintenanceBatchImportKindOption {
  key: string;
  label: string;
  description?: string | null;
  required_fields: string[];
  accepted_aliases: Record<string, string[]>;
  metric_basis?: Record<string, string>;
}

export interface MaintenanceBatchDownloadForm {
  key: string;
  label: string;
  description?: string | null;
  default_selected: boolean;
}

export interface MaintenanceBatchDownloadField {
  key: string;
  label: string;
  group: string;
  form_keys: string[];
  default_selected: boolean;
}

export interface MaintenanceBatchTransferOptions {
  can_import: boolean;
  can_download: boolean;
  max_files: number;
  accepted_extensions: string[];
  import_kinds: MaintenanceBatchImportKindOption[];
  download_forms: MaintenanceBatchDownloadForm[];
  download_fields: MaintenanceBatchDownloadField[];
  default_forms: string[];
  default_fields: string[];
}

export interface MaintenanceBatchDownloadInput {
  forms: string[];
  fields: string[];
  q?: string;
  lifecycle?: BoardProjectLifecycle;
  card_status?: CardStatus;
  sort?: BoardProjectSort;
  /** 所见即所得：与卡墙同一个业务类型筛选，否则下载下来是全量。 */
  business_type?: string;
}

export interface MaintenanceBatchDownloadResult {
  blob: Blob;
  filename: string;
}

export const getMaintenanceBatchTransferOptions = () =>
  api.get<MaintenanceBatchTransferOptions>(`${MAINTENANCE_BATCH_TRANSFER_BASE}/options`);

export const previewMaintenanceBatchTransfer = (files: File[]) => {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  return api.post<MaintenanceBatchPreviewResponse>(
    `${MAINTENANCE_BATCH_TRANSFER_BASE}/preview`,
    body,
  );
};

/**
 * apply 只消费冻结预览的 token、hash、CAS version 与被选中的 row keys。
 * 后端必须重新校验 token/CAS，不得信任或重解析客户端 mapping/canonical values。
 */
export const applyMaintenanceBatchTransfer = (body: MaintenanceBatchApplyRequest) =>
  api.post<MaintenanceBatchApplyResponse>(
    `${MAINTENANCE_BATCH_TRANSFER_BASE}/apply`,
    body,
  );

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 服务端 id 可能是整数或字符串，统一成非空字符串；其余形状一律拒绝。 */
function idText(value: unknown): string | null {
  if (typeof value === "string") return value.trim() ? value : null;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** 累计金额：十进制字符串 / 数字 / null；其余形状拒绝（返回 undefined）。 */
function decimalText(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return undefined;
}

function normalizeRulingMonth(value: unknown): MaintenanceBatchReceiptRulingMonth | null {
  if (!isRecord(value) || typeof value.report_month !== "string") return null;
  const current = decimalText(value.current_cumulative);
  const derived = decimalText(value.derived_cumulative);
  if (current === undefined || derived === undefined) return null;
  return { report_month: value.report_month, current_cumulative: current, derived_cumulative: derived };
}

/** 裁决回执 → 严格校验；任何形状不对都返回 null（调用方按「裁决结果不可信」处理，绝不猜）。 */
export function normalizeMaintenanceReceiptRuling(
  value: unknown,
): MaintenanceBatchReceiptRulingResponse | null {
  if (!isRecord(value) || !Array.isArray(value.affected_months)) return null;
  const rulingId = idText(value.ruling_id);
  const supersededId = idText(value.superseded_receipt_id);
  const newId = idText(value.new_receipt_id);
  if (!rulingId || !supersededId || !newId) return null;
  const months = value.affected_months.map(normalizeRulingMonth);
  if (months.some((month) => month === null)) return null;
  return {
    ruling_id: rulingId,
    superseded_receipt_id: supersededId,
    new_receipt_id: newId,
    affected_months: months as MaintenanceBatchReceiptRulingMonth[],
  };
}

/**
 * 台账冲突人工裁决：仅 admin / boss 且实名账号可调用（共享口令管理员 403）。
 * 只作废并重建台账行，不改快照；调用方须随后重新预览以看到覆盖行。
 */
export const ruleMaintenanceReceiptConflict = async (
  body: MaintenanceBatchReceiptRulingRequest,
): Promise<MaintenanceBatchReceiptRulingResponse> => {
  const { data } = await api.post<unknown>(`${MAINTENANCE_BATCH_TRANSFER_BASE}/receipt-rulings`, body);
  const ruling = normalizeMaintenanceReceiptRuling(data);
  if (!ruling) throw new Error("裁决回执格式无法识别，请重新预览核对台账后再试");
  return ruling;
};

function filenameFromDisposition(disposition: unknown): string {
  const value = String(disposition ?? "");
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(value)?.[1];
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(value)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.replace(/^"|"$/g, ""));
    } catch {
      // 非法 percent encoding 时继续使用 ASCII 文件名。
    }
  }
  return plain || "maintenance-batch-export.xlsx";
}

/** 下载当前筛选命中的全部项目，而不是浏览器里已经滚动加载的卡片。 */
export const downloadMaintenanceBatchTransfer = async (
  body: MaintenanceBatchDownloadInput,
): Promise<MaintenanceBatchDownloadResult> => {
  const response = await api.post<Blob>(
    `${MAINTENANCE_BATCH_TRANSFER_BASE}/download`,
    body,
    { responseType: "blob" },
  );
  return {
    blob: response.data,
    filename: filenameFromDisposition(response.headers["content-disposition"]),
  };
};
