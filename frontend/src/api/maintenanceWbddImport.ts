import { api } from "../api";

/** WBDD 专用上传（plan v1.3 §4.1）。非 WBDD 文件后端 422 零写入。 */
export interface WbddImportReport {
  batch_id: number;
  file_hash: string;
  layout: "91" | "90";
  replayed: boolean;
  orders_inserted?: number;
  orders_updated?: number;
  fact_rows_inserted?: number;
  fact_rows_updated?: number;
  fact_rows_error?: number;
  headless_orders?: number;
  headless_order_ids_sample?: string[];
  rows_display_issue?: number;
  snapshot_diff?: {
    missing_orders: number;
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

export const uploadWbdd = (file: File, idempotencyKey: string) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<WbddImportReport>("/maintenance/wbdd-imports", form, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
};

export const getWbddLatest = () =>
  api.get<WbddLatest>("/maintenance/wbdd-imports/latest");

/** 已应用单据头行的项目重解析（M4-3：先传 RKD 后建归属也能关联）。 */
export const relinkDocProjects = () =>
  api.post<{ relinked: number; still_unlinked: number; out_of_scope: number }>(
    "/maintenance/doc-imports/relink-projects",
  );
