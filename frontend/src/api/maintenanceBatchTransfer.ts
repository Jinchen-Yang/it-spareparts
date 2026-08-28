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
  /** 仅用于人工预览；apply 请求绝不回传。 */
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
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  delta?: Record<string, unknown> | null;
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
