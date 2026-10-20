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

export const confirmSourceCancellation = (
  projectId: string, batchId: number, sourceOrderId: string, body: SourceCancellationConfirmation,
  sessionToken: string,
) => api.post<SourceCancellationResult>(`${candidatePath(projectId, batchId, sourceOrderId)}/confirm`, body, {
  // 此请求属于用户查看并确认的账号；异步拦截器不能用新登录账号替换它。
  headers: { Authorization: `Bearer ${sessionToken}` },
});
