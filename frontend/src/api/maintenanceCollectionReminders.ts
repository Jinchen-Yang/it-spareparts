import { api } from "../api";

/**
 * 回款计划提醒专用 API（车道 C1）。
 *
 * 契约：.ai/contracts/maintenance-collections/collection-reminders-api-v1.yaml
 * 本模块只服务 reminder-only 口径，不得混入维护旧财务回款快照的
 * maintenanceOperations.ts，也不得把 planned_amount 转成 JavaScript number。
 *
 * 服务端完整路径带 /api 前缀，前端 axios baseURL=/api，这里只写 /maintenance/...
 */

export type CollectionDatePrecision = "day" | "month";
export type CollectionFollowUpStatus = "pending" | "handled";
export type CollectionReminderState =
  | "needs_review"
  | "handled"
  | "incomplete"
  | "overdue"
  | "due_this_month"
  | "upcoming";
export type CollectionFollowUpAction = "handle" | "reschedule" | "reopen";
export type CollectionOwnerScope = "me" | "all";
export type CollectionAmountVisibility = "visible" | "restricted";
export type CollectionBatchStatus = "valid" | "error" | "applied" | "expired";
export type CollectionBindingStatus = "reviewed";
export type CollectionPreviewBindingStatus = "reviewed" | "pending_review";
export type CollectionMilestoneChange =
  | "create"
  | "update"
  | "unchanged"
  | "source_missing";
export type CollectionIssueSeverity = "warning" | "blocker";

export interface CollectionManagerAssignment {
  username: string | null;
  display_name: string | null;
}

export interface CollectionServicePeriod {
  service_start: string | null;
  service_end: string | null;
  completeness_state: string;
}

export interface CollectionContractRef {
  project_contract_id: string;
  contract_no: string | null;
  relation_status: string;
  lifecycle_status: string;
  version: number;
}

export interface CollectionProjectRef {
  project_id: string;
  project_code: string;
  display_name: string;
  lifecycle_status: string;
  version: number;
  manager_assignment: CollectionManagerAssignment;
  service_period: CollectionServicePeriod;
  contracts: CollectionContractRef[];
}

export interface CollectionReminderCounts {
  total: number;
  needs_review: number;
  handled: number;
  incomplete: number;
  overdue: number;
  due_this_month: number;
  upcoming: number;
}

export interface CollectionActionableMilestone {
  milestone_id: string;
  project_contract_id: string;
  contract_no: string | null;
  sequence: number;
  planned_month: string | null;
  planned_amount: string | null;
  reminder_state: CollectionReminderState;
  version: number;
}

export interface CollectionDirectoryRow {
  project: CollectionProjectRef;
  reminder_counts: CollectionReminderCounts;
  next_actionable_milestone: CollectionActionableMilestone | null;
}

export interface CollectionReminderDirectoryResponse {
  rows: CollectionDirectoryRow[];
  total: number;
  page: number;
  page_size: number;
  owner_scope: CollectionOwnerScope;
  allowed_owner_scopes: CollectionOwnerScope[];
  as_of: string;
  data_version: string;
  amount_visibility: CollectionAmountVisibility;
}

export interface CollectionMilestoneOperation {
  operation_id: string;
  action: CollectionFollowUpAction;
  reason: string | null;
  actor_display_name: string;
  created_at: string;
  result_version: number;
}

export interface CollectionMilestoneRow {
  milestone_id: string;
  project_contract_id: string;
  contract_no: string | null;
  sequence: number;
  planned_date: string | null;
  date_precision: CollectionDatePrecision;
  planned_month: string | null;
  planned_amount: string | null;
  completeness_state: string;
  follow_up_status: CollectionFollowUpStatus;
  reminder_state: CollectionReminderState;
  follow_up_review_required: boolean;
  followed_up_by: string | null;
  followed_up_at: string | null;
  follow_up_note: string | null;
  last_operation: CollectionMilestoneOperation | null;
  version: number;
}

export interface CollectionProjectDetailResponse {
  project: CollectionProjectRef;
  summary: CollectionReminderCounts;
  rows: CollectionMilestoneRow[];
  as_of: string;
  data_version: string;
  amount_visibility: CollectionAmountVisibility;
}

export interface CollectionFollowUpRequest {
  expected_version: number;
  idempotency_key: string;
  action: CollectionFollowUpAction;
  planned_month?: string | null;
  note?: string | null;
  reason?: string | null;
}

export interface CollectionFollowUpResponse {
  row: CollectionMilestoneRow;
  data_version: string;
  idempotent_replay: boolean;
}

export interface CollectionImportIssue {
  code: string;
  severity: CollectionIssueSeverity;
  row_key: string | null;
  sequence: number | null;
  message: string;
}

export interface CollectionMilestoneDiff {
  sequence: number;
  planned_month: string | null;
  planned_amount: string | null;
  change: CollectionMilestoneChange;
  expected_milestone_version: number | null;
}

export interface CollectionPreviewBinding {
  status: CollectionPreviewBindingStatus;
  project_id: string | null;
  project_version: number | null;
  project_contract_id: string | null;
  project_contract_version: number | null;
  existing_binding_version: number | null;
}

export interface CollectionPreviewOrder {
  row_key: string;
  external_order_no: string;
  source_project_name: string | null;
  binding: CollectionPreviewBinding;
  milestone_diffs: CollectionMilestoneDiff[];
  warning_codes: string[];
  blocker_codes: string[];
}

export interface CollectionPreviewCounts {
  projects: number;
  milestones: number;
  bound: number;
  pending_binding: number;
  blockers: number;
  warnings: number;
  create: number;
  update: number;
  unchanged: number;
  source_missing: number;
}

export interface CollectionPreviewResponse {
  batch_id: string;
  batch_version: number;
  data_version: string;
  status: CollectionBatchStatus;
  contract_version: string;
  file_sha256: string;
  counts: CollectionPreviewCounts;
  rows: CollectionPreviewOrder[];
  issues: CollectionImportIssue[];
  can_apply: boolean;
  expires_at: string;
}

export interface CollectionBindingOptionContract {
  project_contract_id: string;
  contract_no: string | null;
  relation_status: string;
  lifecycle_status: string;
  version: number;
}

export interface CollectionBindingOptionProject {
  project_id: string;
  project_code: string;
  display_name: string;
  version: number;
  contracts: CollectionBindingOptionContract[];
}

export interface CollectionBindingOptionsResponse {
  batch_id: string;
  rows: CollectionBindingOptionProject[];
  total: number;
  page: number;
  page_size: number;
  q: string;
}

export interface CollectionApplyBinding {
  row_key: string;
  external_order_no: string;
  project_id: string;
  project_version: number;
  project_contract_id: string;
  project_contract_version: number;
  existing_binding_version: number | null;
  reason: string | null;
}

export interface CollectionApplyRequest {
  expected_batch_version: number;
  expected_data_version: string;
  bindings: CollectionApplyBinding[];
}

export interface CollectionApplyCounts {
  created: number;
  updated: number;
  unchanged: number;
  source_missing: number;
  needs_review: number;
}

export interface CollectionApplyResponse {
  batch_id: string;
  batch_version: number;
  data_version: string;
  status: CollectionBatchStatus;
  counts: CollectionApplyCounts;
  idempotent_replay: boolean;
  applied_at: string;
}

export interface CollectionReminderSearchRequest {
  q?: string;
  owner_scope?: CollectionOwnerScope;
  reminder_state?: CollectionReminderState | null;
  page?: number;
  page_size?: number;
}

const collectionMilestonesPath = (projectId: string) =>
  `/maintenance/projects/stable/${encodeURIComponent(projectId)}/collection-milestones`;

const collectionMilestoneBase = (milestoneId: string) =>
  `/maintenance/collection-milestones/${encodeURIComponent(milestoneId)}`;

const collectionPlanImportBase = (batchId: string) =>
  `/maintenance/collection-plan-imports/${encodeURIComponent(batchId)}`;

export const searchCollectionReminders = (
  body: CollectionReminderSearchRequest = {},
  options: { signal?: AbortSignal } = {},
) => {
  const request = {
    q: body.q?.trim() || "",
    owner_scope: body.owner_scope ?? "me",
    reminder_state: body.reminder_state ?? null,
    page: body.page ?? 1,
    page_size: body.page_size ?? 24,
  };
  return options.signal
    ? api.post<CollectionReminderDirectoryResponse>(
      "/maintenance/collection-reminders/search",
      request,
      { signal: options.signal },
    )
    : api.post<CollectionReminderDirectoryResponse>(
      "/maintenance/collection-reminders/search",
      request,
    );
};

export const getCollectionMilestones = (
  projectId: string,
  options: { signal?: AbortSignal } = {},
) => options.signal
  ? api.get<CollectionProjectDetailResponse>(collectionMilestonesPath(projectId), {
    signal: options.signal,
  })
  : api.get<CollectionProjectDetailResponse>(collectionMilestonesPath(projectId));

export const followUpCollectionMilestone = (
  milestoneId: string,
  body: CollectionFollowUpRequest,
) => api.post<CollectionFollowUpResponse>(
  `${collectionMilestoneBase(milestoneId)}/follow-ups`,
  body,
);

export const previewCollectionPlan = (file: File, idempotencyKey: string) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<CollectionPreviewResponse>(
    "/maintenance/collection-plan-imports/preview",
    form,
    {
      headers: { "Idempotency-Key": idempotencyKey },
      timeout: 120000,
    },
  );
};

export const searchCollectionPlanBindingOptions = (
  batchId: string,
  params: { q: string; page?: number; page_size?: number },
  options: { signal?: AbortSignal } = {},
) => {
  const query = {
    q: params.q.trim(),
    page: params.page ?? 1,
    page_size: params.page_size ?? 20,
  };
  return options.signal
    ? api.get<CollectionBindingOptionsResponse>(
      `${collectionPlanImportBase(batchId)}/binding-options`,
      { params: query, signal: options.signal },
    )
    : api.get<CollectionBindingOptionsResponse>(
      `${collectionPlanImportBase(batchId)}/binding-options`,
      { params: query },
    );
};

export const applyCollectionPlan = (
  batchId: string,
  body: CollectionApplyRequest,
) => api.post<CollectionApplyResponse>(
  `${collectionPlanImportBase(batchId)}/apply`,
  body,
);

export const downloadCollectionPlanSourceFile = (batchId: string) =>
  api.get<Blob>(`${collectionPlanImportBase(batchId)}/source-file`, {
    responseType: "blob",
  });

/**
 * 金额展示格式化器：只接受十进制定点字符串或 null。
 *
 * - 只做合法十进制校验与千分位展示，不做任何 JavaScript 浮点计算；
 * - 不调用接收 number 的 money() 等既有金额 helper；
 * - null / 非法字符串返回 null，由调用方展示“无权限查看”等脱敏文案。
 */
export function formatDecimalAmount(value: string | null): string | null {
  if (value == null) return null;
  const normalized = value.trim();
  if (!/^[+-]?\d+(\.\d+)?$/.test(normalized)) return null;
  const negative = normalized.startsWith("-");
  const unsigned = normalized.replace(/^[+-]/, "");
  const [integerPart, fraction] = unsigned.split(".");
  const grouped = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}${fraction ? `.${fraction}` : ""}`;
}
