import api from "../api";

export interface ReplenishmentCapabilities {
  enabled: boolean;
  beta: true;
  can_view_price: boolean;
  can_create: boolean;
  can_review: boolean;
  workflow_mode: string;
  stage: string;
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

/** 补库申请可归属的维保项目（后端按账号授权返回）。 */
export interface ReplenishmentProject {
  project_id: string;
  project_code: string;
  display_name: string;
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
  /** 提交时冻结的系统三查快照（schema_version/as_of/checks/anomaly_count）。 */
  screening: {
    schema_version: number;
    as_of: string;
    lookback_days: number;
    checks: Array<{ name: string; passed: boolean; detail?: unknown }>;
    anomaly_count: number;
    auto_review?: {
      decision: "approved" | "rejected";
      reason_code: string;
    };
    recommendations?: Array<{
      part_id: number;
      pn_std: string;
      description: string | null;
      pool_group_id: number | null;
      pool_name: string | null;
      score: number;
      match_reason: string | null;
    }>;
  } | null;
  latest_sales: Record<string, unknown> | null;
  /** 后端 Decimal 序列化可能为字符串，展示前需 Number() 归一。 */
  pool_floor_ex_tax: number | string | null;
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
  is_legacy_project_unbound: boolean;
  project: ReplenishmentProject | null;
  status: "draft" | "submitted" | "needs_revision" | "approved";
  workflow_mode: string;
  stage: string;
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
  project: ReplenishmentProject | null;
  status: ReplenishmentApplication["status"];
  workflow_mode: string;
  stage: string;
  version: number;
  latest_version_no: number;
  updated_at: string;
}

/** 一次性原子提交的明细行（quantity 为整数）。 */
export interface AtomicLineInput {
  part_id: number;
  quantity: number;
  special_note?: string | null;
}

export interface ReplenishmentCartDraftLine {
  draft_line_id: string;
  line_no: number;
  part_id: number;
  pn_std: string;
  description: string | null;
  brand: string | null;
  unit: string | null;
  quantity: number;
  special_note: string | null;
}

export interface ReplenishmentCartDraft {
  draft_id: string;
  project_id: string;
  request_note: string | null;
  client_request_id: string;
  version: number;
  created_at: string;
  updated_at: string;
  lines: ReplenishmentCartDraftLine[];
}

export const getReplenishmentCapabilities = () =>
  api.get<ReplenishmentCapabilities>("/replenishment-beta/capabilities");

export const searchReplenishmentCatalog = (q: string, page = 1, pageSize = 20) =>
  api.get<{ items: CatalogPart[]; total: number; page: number; page_size: number }>(
    "/replenishment-beta/catalog",
    { params: { q: q || undefined, page, page_size: pageSize } },
  );

/** 当前账号可选的维保项目（销售经理=本人项目；admin=全部活动项目）。 */
export const getReplenishmentProjects = () =>
  api.get<{ items: ReplenishmentProject[] }>("/replenishment-beta/projects");

export const listReplenishmentApplications = (page = 1, pageSize = 20) =>
  api.get<{
    items: ApplicationSummary[];
    total: number;
    page: number;
    page_size: number;
  }>("/replenishment-beta/applications", { params: { page, page_size: pageSize } });

export const getReplenishmentApplication = (applicationId: string) =>
  api.get<ReplenishmentApplication>(`/replenishment-beta/applications/${applicationId}`);

/**
 * 一次性原子提交（Issue #260）：创建即提交，事务内完成三查并冻结证据。
 * 相同 client_request_id + 相同内容 → 幂等返回既有申请（idempotent=true）。
 */
export const createReplenishmentApplication = (input: {
  client_request_id: string;
  project_id: string;
  request_note?: string | null;
  lines: AtomicLineInput[];
}) =>
  api.post<ReplenishmentApplication & { idempotent?: boolean }>(
    "/replenishment-beta/applications",
    input,
  );

export const getReplenishmentCartDraft = (projectId: string) =>
  api.get<{ draft: ReplenishmentCartDraft | null }>(
    `/replenishment-beta/cart-drafts/${encodeURIComponent(projectId)}`,
  );

export const replaceReplenishmentCartDraft = (projectId: string, input: {
  expected_version?: number | null;
  request_note?: string | null;
  lines: AtomicLineInput[];
}) => api.put<{ draft: ReplenishmentCartDraft }>(
  `/replenishment-beta/cart-drafts/${encodeURIComponent(projectId)}`,
  input,
);

export const deleteReplenishmentCartDraft = (projectId: string, expectedVersion?: number) =>
  api.delete<{ deleted: boolean }>(
    `/replenishment-beta/cart-drafts/${encodeURIComponent(projectId)}`,
    { params: expectedVersion ? { expected_version: expectedVersion } : undefined },
  );

export const submitReplenishmentCartDraft = (projectId: string, expectedVersion: number) =>
  api.post<ReplenishmentApplication & { idempotent?: boolean }>(
    `/replenishment-beta/cart-drafts/${encodeURIComponent(projectId)}/submit`,
    { expected_version: expectedVersion },
  );

export const applyReplenishmentRevision = (applicationId: string, input: {
  expected_application_version: number;
  client_request_id: string;
  resolutions: Array<{
    request_line_id: string;
    action: "replace" | "remove";
    part_id?: number;
    quantity?: number;
    special_note?: string | null;
  }>;
}) => api.post<ReplenishmentApplication & { idempotent?: boolean }>(
  `/replenishment-beta/applications/${encodeURIComponent(applicationId)}/revisions`,
  input,
);

/** 导出系统三查复核包 Excel（submitted/approved/needs_revision 均可，#11）。 */
export const downloadSystemScreeningWorkbook = (applicationId: string) =>
  api.get<Blob>(
    `/replenishment-beta/applications/${encodeURIComponent(applicationId)}/exports/system-screening.xlsx`,
    { responseType: "blob" },
  );
