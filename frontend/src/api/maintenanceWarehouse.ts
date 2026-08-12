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
  header_pairs: WarehouseHeaderPair[];
  header_diff: WarehouseHeaderDiff;
  data_row_count: number;
  document_count: number;
  line_count: number;
  adapter_ambiguity_counts: Record<string, number>;
  can_apply: boolean;
}

export interface WarehouseHeaderPair {
  position: number;
  internal_code: string;
  business_label: string;
}

export interface WarehouseHeaderDiff {
  state: "approved_exact" | "approved_baseline_unavailable" | "unapproved_difference";
  baseline_signature: string | null;
  added: Array<{ position: number; internal_code: string }>;
  removed: Array<{ position: number; internal_code: string }>;
  moved: Array<{ internal_code: string; from_position: number; to_position: number }>;
  label_changed: Array<{
    internal_code: string;
    position: number;
    approved_label_hash: string;
    current_label_hash: string;
  }>;
}

export interface WarehouseBatchEvidence {
  import_id: string;
  filename: string;
  source_file_hash: string;
  adapter_key: string;
  adapter_version: string;
  version_state: "known" | "unknown_version";
  header_signature: string;
  header_pairs: WarehouseHeaderPair[];
  header_diff: WarehouseHeaderDiff | null;
  applied_by: string;
  applied_at: string;
}

export interface WarehouseLinkEvidence {
  link_id: string;
  line_id: string | null;
  link_kind: string;
  target_type: string;
  target_id: string;
  stable_key_kind: string;
  stable_key_hash: string;
  source: "automatic" | "manual";
  status: "active" | "superseded";
  supersedes_link_id: string | null;
  version: number;
  reason: string;
  operated_by: string;
  created_at: string;
}

export interface WarehouseAuditEvidence {
  event_id: string;
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown>;
  reason: string;
  operated_by: string;
  occurred_at: string;
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
  eligible_line_count: number;
  project_link_state:
    | "ready"
    | "not_applicable"
    | "not_confirmed"
    | "missing_order_link"
    | "missing_project_link"
    | "ambiguous_active_links"
    | "assignment_contract_unavailable"
    | "assignment_mismatch";
  open_ambiguity_count: number;
  batch: WarehouseBatchEvidence | null;
  links: WarehouseLinkEvidence[];
}

export interface WarehouseLinkCandidate {
  target_type: string;
  target_id: string;
  label?: string;
}

export interface WarehouseConflictEvidence {
  before_fingerprint: string;
  after_fingerprint: string;
  changed_fields: Array<{
    field_code: string;
    before: unknown;
    after: unknown;
  }>;
}

export interface WarehouseAmbiguitySummary {
  ambiguity_id: string;
  import_id: string;
  ambiguity_type: string;
  field_code: string | null;
  source_row: number | null;
  value_hash: string | null;
  status: "open" | "resolved";
  version: number;
  candidates: WarehouseLinkCandidate[];
  evidence: WarehouseConflictEvidence | null;
  resolution: Record<string, unknown> | null;
  resolution_reason: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  batch: WarehouseBatchEvidence | null;
  links: WarehouseLinkEvidence[];
  history: WarehouseAuditEvidence[];
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
    decision: "acknowledge" | "link" | "retain_existing";
    link_kind?: string;
    target_type?: string;
    target_id?: string;
  },
) => api.post(
  `/maintenance/warehouse-ambiguities/${ambiguityId}/resolve`,
  body,
);
