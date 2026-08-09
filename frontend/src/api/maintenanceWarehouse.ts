import { api } from "../api";

export interface WarehouseImportPreview {
  import_id: string;
  preview_token: string;
  filename: string;
  source_file_hash: string;
  adapter_key: string;
  adapter_version: string;
  version_state: "known" | "unknown_version";
  header_signature: string;
  data_row_count: number;
  document_count: number;
  line_count: number;
  adapter_ambiguity_counts: Record<string, number>;
  can_apply: boolean;
}

export interface WarehouseImportResult {
  import_id: string;
  adapter_key: string;
  adapter_version: string;
  version_state: "known" | "unknown_version";
  document_count: number;
  line_count: number;
  ambiguity_count: number;
  new_document_count: number;
  new_line_count: number;
  new_link_count: number;
  idempotent_replay: boolean;
  writes: Record<string, number>;
}

export interface WarehouseDocumentSummary {
  document_id: string;
  document_type: "shipment" | "return" | "receipt";
  source_document_id: string;
  document_no: string | null;
  document_date: string | null;
  raw_status: string | null;
  normalized_status: string;
  line_count: number;
  open_ambiguity_count: number;
}

export interface WarehouseLinkCandidate {
  target_type: string;
  target_id: string;
  label?: string;
}

export interface WarehouseAmbiguitySummary {
  ambiguity_id: string;
  import_id: string;
  ambiguity_type: string;
  field_code: string | null;
  source_row: number | null;
  status: "open" | "resolved";
  version: number;
  candidates: WarehouseLinkCandidate[];
  resolution: Record<string, unknown> | null;
  resolution_reason: string | null;
  resolved_by: string | null;
  document: null | {
    document_id: string;
    document_type: string;
    document_no: string | null;
    source_document_id: string;
  };
}

export interface WarehouseSearchResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export const previewWarehouseImport = (file: File) => {
  const body = new FormData();
  body.append("file", file);
  return api.post<WarehouseImportPreview>("/maintenance/warehouse-imports/preview", body);
};

export const applyWarehouseImport = (
  preview: WarehouseImportPreview,
  file: File,
  reason: string,
) => {
  const body = new FormData();
  body.append("file", file);
  body.append("preview_token", preview.preview_token);
  body.append("reason", reason);
  return api.post<WarehouseImportResult>(
    `/maintenance/warehouse-imports/${preview.import_id}/apply`,
    body,
  );
};

export const searchWarehouseDocuments = (body: {
  q?: string;
  document_type?: string;
  page: number;
  page_size: number;
}) => api.post<WarehouseSearchResult<WarehouseDocumentSummary>>(
  "/maintenance/warehouse-documents/search",
  body,
);

export const searchWarehouseAmbiguities = (body: {
  q?: string;
  status?: string;
  ambiguity_type?: string;
  page: number;
  page_size: number;
}) => api.post<WarehouseSearchResult<WarehouseAmbiguitySummary>>(
  "/maintenance/warehouse-ambiguities/search",
  body,
);

export const resolveWarehouseAmbiguity = (
  ambiguityId: string,
  body: {
    version: number;
    reason: string;
    decision: "acknowledge" | "link";
    link_kind?: string;
    target_type?: string;
    target_id?: string;
  },
) => api.post(
  `/maintenance/warehouse-ambiguities/${ambiguityId}/resolve`,
  body,
);
