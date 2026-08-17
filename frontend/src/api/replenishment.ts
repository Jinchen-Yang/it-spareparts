import api from "../api";

export interface ReplenishmentCapabilities {
  enabled: boolean;
  beta: true;
  can_view_price: boolean;
  can_create: boolean;
  can_review: boolean;
  stable_path: string;
  data_contract: string;
}

export interface PriceStats {
  weighted_avg: number | null;
  total_qty: number | null;
  order_count: number;
  line_count: number;
  latest_date: string | null;
}

export interface PoolSnapshot {
  group_id: number | null;
  name: string | null;
  version: number | null;
}

export interface CatalogPart {
  part_id: number;
  pn_std: string;
  description: string | null;
  brand: string | null;
  unit: string | null;
  needs_review: boolean;
  pool: PoolSnapshot;
  price_window: {
    date_from: string;
    date_to: string;
    days: number;
    basis: string;
  };
  purchase: PriceStats | null;
  sales: PriceStats | null;
}

export interface ReviewFeedback {
  decision: "approved" | "rejected";
  reason: string | null;
}

export interface ReplenishmentLine extends Omit<CatalogPart, "needs_review"> {
  line_id: string;
  request_line_id: string;
  source_line_id: string | null;
  line_no: number;
  quantity: number;
  special_note: string | null;
  review: ReviewFeedback | null;
}

export interface ReplenishmentVersion {
  version_id: string;
  version_no: number;
  parent_version_id: string | null;
  status: "draft" | "submitted";
  warehouse: string | null;
  request_note: string | null;
  content_digest: string | null;
  submitted_by: string | null;
  submitted_at: string | null;
  lines: ReplenishmentLine[];
  review: {
    review_id: string;
    external_reference: string | null;
    summary_note: string | null;
    approved_count: number;
    rejected_count: number;
    reviewed_at: string;
  } | null;
}

export interface ReplenishmentApplication {
  application_id: string;
  application_no: string;
  owner_username: string;
  owner_display_name: string | null;
  salesperson_name_snapshot: string | null;
  status: "draft" | "submitted" | "needs_revision" | "approved";
  version: number;
  latest_version_no: number;
  created_at: string;
  updated_at: string;
  versions: ReplenishmentVersion[];
}

export interface ApplicationSummary {
  application_id: string;
  application_no: string;
  owner_display_name: string | null;
  status: ReplenishmentApplication["status"];
  version: number;
  latest_version_no: number;
  updated_at: string;
}

export const getReplenishmentCapabilities = () =>
  api.get<ReplenishmentCapabilities>("/replenishment-beta/capabilities");

export const searchReplenishmentCatalog = (q: string, page = 1, pageSize = 20) =>
  api.get<{ items: CatalogPart[]; total: number; page: number; page_size: number }>(
    "/replenishment-beta/catalog",
    { params: { q: q || undefined, page, page_size: pageSize } },
  );

export const listReplenishmentApplications = (page = 1, pageSize = 20) =>
  api.get<{
    items: ApplicationSummary[];
    total: number;
    page: number;
    page_size: number;
  }>("/replenishment-beta/applications", { params: { page, page_size: pageSize } });

export const getReplenishmentApplication = (applicationId: string) =>
  api.get<ReplenishmentApplication>(`/replenishment-beta/applications/${applicationId}`);

export const createReplenishmentApplication = (warehouse?: string, requestNote?: string) =>
  api.post<ReplenishmentApplication>("/replenishment-beta/applications", {
    warehouse: warehouse || null,
    request_note: requestNote || null,
  });

export const updateReplenishmentDraft = (
  applicationId: string,
  input: { expected_version: number; warehouse?: string; request_note?: string },
) => api.patch<ReplenishmentApplication>(`/replenishment-beta/applications/${applicationId}`, input);

export const addReplenishmentLine = (
  applicationId: string,
  input: { expected_version: number; part_id: number; quantity: number; special_note?: string | null },
) => api.post<ReplenishmentApplication>(
  `/replenishment-beta/applications/${applicationId}/lines`,
  input,
);

export const updateReplenishmentLine = (
  applicationId: string,
  lineId: string,
  input: { expected_version: number; part_id: number; quantity: number; special_note?: string | null },
) => api.patch<ReplenishmentApplication>(
  `/replenishment-beta/applications/${applicationId}/lines/${lineId}`,
  input,
);

export const removeReplenishmentLine = (
  applicationId: string,
  lineId: string,
  expectedVersion: number,
) => api.delete<ReplenishmentApplication>(
  `/replenishment-beta/applications/${applicationId}/lines/${lineId}`,
  { params: { expected_version: expectedVersion } },
);

export const submitReplenishmentApplication = (applicationId: string, expectedVersion: number) =>
  api.post<ReplenishmentApplication>(`/replenishment-beta/applications/${applicationId}/submit`, {
    expected_version: expectedVersion,
  });

export const startReplenishmentRevision = (applicationId: string, expectedVersion: number) =>
  api.post<ReplenishmentApplication>(`/replenishment-beta/applications/${applicationId}/revision`, {
    expected_version: expectedVersion,
  });

export const downloadManualReviewWorkbook = (applicationId: string) =>
  api.get<Blob>(`/replenishment-beta/applications/${applicationId}/exports/manual-review.xlsx`, {
    responseType: "blob",
  });

export const downloadWbddSubsetWorkbook = (applicationId: string) =>
  api.get<Blob>(`/replenishment-beta/applications/${applicationId}/exports/wbdd-subset.xlsx`, {
    responseType: "blob",
  });

/** 审核通过后的四列采购清单导出（PN / 数量 / 采购金额参考 / 销售金额参考）。 */
export const downloadPurchaseListWorkbook = (applicationId: string) =>
  api.get<Blob>(
    `/replenishment-beta/applications/${applicationId}/exports/purchase-list.xlsx`,
    { responseType: "blob" },
  );
