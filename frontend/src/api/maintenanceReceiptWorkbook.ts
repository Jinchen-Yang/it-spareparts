import { api } from "../api";

export interface ReceiptWorkbookPreview {
  valid: boolean;
  digest: string;
  replayed: boolean;
  updates: number;
  voids: number;
  rows: { row_no: number; receipt_id: string; action: string; fields?: string[];
    reason?: string; changes?: { field: string; label: string; before: unknown; after: unknown }[] }[];
}
export interface ReceiptWorkbookResult {
  replayed: boolean;
  updates: number;
  voids: number;
  rows: { row_no: number; receipt_id: string; action: string; version: number }[];
}
const base = (projectId: string) => `/maintenance/projects/stable/${encodeURIComponent(projectId)}/return-receipt-workbook`;
const upload = (file: File, digest?: string) => {
  const form = new FormData(); form.append("file", file);
  if (digest) form.append("digest", digest);
  return form;
};
export const downloadReceiptWorkbook = (id: string) => api.get<Blob>(`${base(id)}.xlsx`, { responseType: "blob" });
export const previewReceiptWorkbook = (id: string, file: File) => api.post<ReceiptWorkbookPreview>(`${base(id)}/validate`, upload(file));
export const applyReceiptWorkbook = (id: string, file: File, digest: string) => api.post<ReceiptWorkbookResult>(`${base(id)}/apply`, upload(file, digest));
