import { api } from "../api";

export interface SourceCancellationCandidate {
  batch_id: number;
  source_order_id: string;
  order_no: string;
  data_status: string;
  executable: boolean;
  warnings: string[];
}

export interface SourceCancellationDemand {
  line_id: number;
  raw_line_id: string | null;
  pn: string | null;
  qty: string | null;
  is_active: boolean;
  manual_created: boolean;
  protected_fields: string[];
}

export interface SourceCancellationSiteLine {
  issue_line_id: string;
  issue_id: string;
  issue_no: string;
  issue_date: string | null;
  link_reason: string;
  pn: string | null;
  quantity: string;
  is_active: boolean;
  /** Reliable source-demand lineage only; these flags describe the demand. */
  source_demand_line_id: number | null;
  manual_created: boolean;
  protected_fields: string[];
}

export interface SourceCancellationDetail extends SourceCancellationCandidate {
  expected_digest: string;
  demands: SourceCancellationDemand[];
  exact_site_lines: SourceCancellationSiteLine[];
  unlinked_site_lines: SourceCancellationSiteLine[];
}

export interface SourceCancellationConfirmation {
  expected_digest: string;
  reason: string;
  idempotency_key: string;
}

export interface SourceCancellationResult {
  batch_id: number;
  source_order_id: string;
  voided_orders: number;
  voided_lines: number;
  voided_site_lines: number;
  voided_site_issues: number;
  replayed: boolean;
  confirmed_at: string;
  warnings: string[];
}

/** D-0022：一次取消确认回执的当前恢复状态与可恢复范围。 */
export interface SourceRestorationConflict {
  kind: string;
  id: string;
  reason: string;
}

export interface SourceRestorationDemand {
  line_id: number;
  raw_line_id: string | null;
  pn: string | null;
  qty: string | null;
}

export interface SourceRestorationSiteLine {
  issue_line_id: string;
  issue_id: string;
  issue_no: string | null;
  pn: string | null;
  quantity: string | null;
}

export interface SourceRestorationSiteIssue {
  issue_id: string;
  issue_no: string | null;
  voided_status: string;
  original_status: string | null;
}

export type SourceRestorationState = "restorable" | "conflict" | "restored" | "unavailable";

export interface SourceRestorationInfo {
  batch_id: number;
  source_order_id: string;
  order_no: string | null;
  confirmed_at: string | null;
  confirmed_by: string | null;
  confirm_reason: string | null;
  voided_lines: number;
  voided_site_lines: number;
  voided_site_issues: number;
  warnings: string[];
  conflicts: SourceRestorationConflict[];
  restored: boolean;
  restore_receipt: {
    batch_id: number;
    operated_by?: string;
    restored_at?: string;
    result?: SourceRestorationResult;
  } | null;
  state: SourceRestorationState;
  expected_digest: string | null;
  restorable_demands?: SourceRestorationDemand[];
  restorable_site_lines?: SourceRestorationSiteLine[];
  restorable_site_issues?: SourceRestorationSiteIssue[];
  context?: { revision: number | null };
}

export interface SourceRestorationResult {
  batch_id: number;
  source_order_id: string;
  status: "restored" | "already_restored";
  restored_at: string;
  restored_lines: number;
  restored_site_lines: number;
  restored_site_issues: number;
  tombstone_version: number;
  replayed: boolean;
  warnings: string[];
  restored_via?: string;
  restored_by?: string;
}

const projectPath = (projectId: string) =>
  `/maintenance/projects/${encodeURIComponent(projectId)}/source-cancellations`;
const candidatePath = (projectId: string, batchId: number, sourceOrderId: string) =>
  `${projectPath(projectId)}/${batchId}/${encodeURIComponent(sourceOrderId)}`;

export const listSourceCancellations = (projectId: string, params: {
  page: number;
  page_size: number;
}) => api.get<{
  items: SourceCancellationCandidate[];
  total: number;
  page: number;
  page_size: number;
}>(projectPath(projectId), { params });

export const getSourceCancellation = (projectId: string, batchId: number, sourceOrderId: string) =>
  api.get<SourceCancellationDetail>(candidatePath(projectId, batchId, sourceOrderId));

export const getSourceCancellationStatus = (
  projectId: string, batchId: number, sourceOrderId: string, idempotencyKey: string, sessionToken: string,
) => api.get<{ result: SourceCancellationResult | null }>(
  `${candidatePath(projectId, batchId, sourceOrderId)}/confirmation-status`,
  { params: { idempotency_key: idempotencyKey }, headers: { Authorization: `Bearer ${sessionToken}` } },
);

export const confirmSourceCancellation = (
  projectId: string, batchId: number, sourceOrderId: string, body: SourceCancellationConfirmation,
  sessionToken: string,
) => api.post<SourceCancellationResult>(`${candidatePath(projectId, batchId, sourceOrderId)}/confirm`, body, {
  // 此请求属于用户查看并确认的账号；异步拦截器不能用新登录账号替换它。
  headers: { Authorization: `Bearer ${sessionToken}` },
});

const restorationPath = (projectId: string, sourceOrderId: string) =>
  `${projectPath(projectId)}/restorations/${encodeURIComponent(sourceOrderId)}`;

export const listSourceRestorations = (projectId: string) =>
  api.get<{ items: SourceRestorationInfo[]; total: number }>(
    `${projectPath(projectId)}/restorations`);

export const getSourceRestoration = (projectId: string, sourceOrderId: string) =>
  api.get<SourceRestorationInfo>(restorationPath(projectId, sourceOrderId));

export const restoreSourceCancellation = (
  projectId: string, sourceOrderId: string, body: SourceCancellationConfirmation,
  sessionToken: string,
) => api.post<SourceRestorationResult>(`${restorationPath(projectId, sourceOrderId)}/restore`, body, {
  // 与确认作废同因：请求属于用户查看并确认的账号。
  headers: { Authorization: `Bearer ${sessionToken}` },
});
