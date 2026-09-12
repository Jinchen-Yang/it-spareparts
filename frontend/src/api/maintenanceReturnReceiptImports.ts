import { api } from "../api";

export type ReturnImportAction = "create" | "unchanged" | "change" | "pending" | "excluded" | "invalid";
export interface ReturnImportRow {
  row_key: string;
  head_no: string;
  pn: string;
  qty: string;
  condition: string | null;
  project_id: string | null;
  project_name: string | null;
  wbdd_no: string | null;
  kind: "part" | "machine" | "component";
  parent_row_key: string | null;
  review_required: boolean;
  action: ReturnImportAction;
  reason: string;
  possible_duplicates?: { receipt_id: string; qty: string; occurred_at: string | null; receipt_date: string | null; version: number }[];
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
}
export interface ReturnImportJob {
  batch_id: string;
  status: "queued" | "processing" | "ready" | "failed" | "cancelled" | "applied";
  filename?: string;
  error?: { code: string; message: string } | null;
  summary?: Record<ReturnImportAction | "blocking_errors", number>;
  rows?: ReturnImportRow[];
  /** 非返件业务的原始单据数量，按入库类别分组。 */
  excluded_reasons?: Record<string, number>;
  possible_duplicates_count?: number;
  rows_total?: number;
  offset?: number;
  limit?: number;
  plan_hash?: string;
  preview_token?: string;
}
const base = "/maintenance/doc-imports/return-receipts/jobs";
const jobPath = (id: string) => `${base}/${encodeURIComponent(id)}`;
export const uploadReturnReceiptImport = (file: File, idempotencyKey: string) => {
  const data = new FormData();
  data.append("file", file);
  return api.post<ReturnImportJob>(base, data, {
    headers: { "Idempotency-Key": idempotencyKey }, timeout: 120_000,
  });
};
export const getReturnReceiptImport = (id: string, page = 1) =>
  api.get<ReturnImportJob>(jobPath(id), { params: { offset: (page - 1) * 100, limit: 100 } });
export const cancelReturnReceiptImport = (id: string) => api.post<ReturnImportJob>(`${jobPath(id)}/cancel`);
export const retryReturnReceiptImport = (id: string) => api.post<ReturnImportJob>(`${jobPath(id)}/retry`);
export const applyReturnReceiptImport = (id: string, input: {
  plan_hash: string; preview_token: string; confirm_changes: boolean; confirm_possible_duplicates?: boolean; reason?: string;
}) => api.post<ReturnImportJob>(`${jobPath(id)}/apply`, input, { timeout: 120_000 });

export const downloadReturnReceiptOriginal = (id: string) =>
  api.get<Blob>(`${jobPath(id)}/original`, { responseType: "blob", timeout: 120_000 });
