import { api } from "../api";
import {
  readMaintenanceCapabilities,
  type MaintenanceCapabilities,
} from "../components/maintenance/maintenancePermissions";

export type MaintenanceLifecycleStatus = "ongoing" | "ended" | "missing" | string;
export type ProjectReminderSeverity = "info" | "warning" | "critical";

export interface MaintenanceManagerAssignment {
  assignment_id: string;
  project_id: string;
  responsibility_type: "primary_manager";
  user_id: number;
  username: string;
  display_name: string | null;
  account_status: "active" | "inactive";
  source_manager_text: string | null;
  version: number;
  assigned_at: string;
  archived_at: string | null;
}

export interface MaintenanceProjectTask {
  task_id: string;
  project_id: string;
  rule_key: string;
  severity: ProjectReminderSeverity;
  title: string;
  detail: string | null;
  entity_id: string | null;
  task_type: string;
  due_date: string | null;
  due_state: "completed" | "overdue" | "due_today" | "upcoming" | "none";
  is_overdue: boolean;
  status: "open" | "pending" | "completed" | string;
  owner: string | null;
  generated_by: "system";
  close_basis: string;
}

export interface MaintenanceProjectTaskSummary {
  primary: MaintenanceProjectTask | null;
  open_count: number;
  overdue_count: number;
  rows: MaintenanceProjectTask[];
}

export interface MaintenanceContractSummary {
  project_contract_id: string;
  contract_id: string;
  contract_no: string;
  contract_amount: number | null;
  contract_amount_basis: "inc_tax" | null;
  contract_status: string | null;
  status_mapping_state: "mapped" | "unmapped";
  included_in_total: boolean;
  is_effective: boolean;
  amount_status: "available" | "missing" | "restricted" | string;
  received_amount: number | null;
}

export interface MaintenanceOperationsMetrics {
  total_contract_amount: number | null;
  known_contract_amount: number | null;
  contract_amount_basis: "inc_tax" | null;
  contract_amount_complete: boolean | null;
  received_amount: number | null;
  collection_progress_pct: number | null;
  site_requisition_known_cost: number | null;
  site_requisition_known_cost_ex_tax: number | null;
  site_requisition_known_cost_inc_tax: number | null;
  site_requisition_priced_cost_ex_tax?: number | null;
  site_requisition_priced_cost_inc_tax?: number | null;
  sales_estimate_cost_ex_tax?: number | null;
  sales_estimate_cost_inc_tax?: number | null;
  sales_estimate_lines?: number | null;
  cost_progress_includes_sales_estimate?: boolean | null;
  cost_progress_label?: "priced_cost_including_sales_estimate" | "priced_cost_without_sales_estimate" | string | null;
  approved_expense: number | null;
  approved_expense_ex_tax: number | null;
  approved_expense_inc_tax: number | null;
  actual_project_cost_known: number | null;
  actual_project_cost_known_ex_tax: number | null;
  actual_project_cost_known_inc_tax: number | null;
  cost_progress_basis: "inc_tax" | null;
  cost_rate_lower_bound_pct: number | null;
  cost_status: "normal" | "yellow" | "red" | "unknown" | null;
  cost_complete: boolean | null;
  missing_cost_lines: number | null;
}

export interface MaintenanceProjectOperationsSummary {
  project_id: string;
  project_code: string;
  display_name: string;
  project_manager_id: string | null;
  lifecycle_status: MaintenanceLifecycleStatus;
  is_active: boolean;
  version: number;
  manual_source_order_count: number;
  contracts: MaintenanceContractSummary[];
  metrics: MaintenanceOperationsMetrics;
  return_rate?: MaintenanceReturnRate | null;
  reminder_count: number;
  manager_assignment: MaintenanceManagerAssignment | null;
  task_summary: MaintenanceProjectTaskSummary;
  missing_data_labels: string[];
  attachment_status: "missing" | "available" | string;
  manager_tracking?: MaintenanceManagerTracking;
  as_of: string;
}

export interface MaintenanceManagerTracking {
  service_period: {
    service_start: string | null;
    service_end: string | null;
    completeness_state: "complete" | "start_only" | "end_only" | "empty" | string;
  };
  next_collection_milestone: {
    project_contract_id: string;
    contract_no: string | null;
    sequence: number;
    planned_date: string | null;
    planned_amount: number | null;
    overdue_days: number;
    is_overdue: boolean;
  } | null;
  acceptance: {
    deliverable_id: string | null;
    due_date: string | null;
    submission_status: "not_submitted" | "submitted" | string;
    approval_status: "not_reviewed" | "approved" | "rejected" | string;
    configuration_state: "configured" | "pending_business_configuration" | string;
    rejection_reason: string | null;
    attachment_count: number;
    overdue_days: number;
    is_overdue: boolean;
    version: number;
  };
}

export interface MaintenanceProjectOperationsDirectory {
  rows: MaintenanceProjectOperationsSummary[];
  total: number;
  page: number;
  page_size: number;
  as_of: string;
  data_version: string;
  owner_scope: "me" | "all";
  filters?: {
    task_type: string | null;
    task_status: string | null;
    due_from: string | null;
    due_to: string | null;
  };
}

export interface MaintenanceSiteRequisitionRow {
  line_id: string;
  order_no: string;
  order_date: string | null;
  contract_no: string | null;
  pn: string | null;
  description: string | null;
  quantity: number | null;
  unit_cost: number | null;
  cost_amount: number | null;
  unit_cost_ex_tax: number | null;
  unit_cost_inc_tax: number | null;
  cost_amount_ex_tax: number | null;
  cost_amount_inc_tax: number | null;
  cost_source: string | null;
  cost_evidence_kind?: "purchase_evidence" | "sales_estimate" | "manual_confirmed" | "missing" | string | null;
  cost_is_estimate?: boolean | null;
  cost_source_label?: string | null;
  cost_status: "available" | "missing" | "restricted" | "not_counted" | string;
}

export interface MaintenanceApprovedExpenseRow {
  expense_id: string;
  expense_no?: string | null;
  expense_ref?: string | null;
  expense_date: string | null;
  contract_no: string | null;
  applicant?: string | null;
  category?: string | null;
  expense_reason?: string | null;
  amount: number | null;
  amount_ex_tax: number | null;
  amount_inc_tax: number | null;
  approval_status: "approved" | string;
}

export interface MaintenanceCollectionSnapshotRow {
  collection_id: string;
  project_contract_id: string;
  contract_no: string | null;
  report_month: string;
  cumulative_amount: number | null;
  receipt_reference: string | null;
  status: "confirmed" | "unconfirmed" | "void" | string;
  remark: string | null;
  version: number;
}

export interface MaintenanceProjectReminder {
  reminder_id: string;
  type: string;
  severity: ProjectReminderSeverity;
  title: string;
  detail: string | null;
  due_date: string | null;
}

export interface MaintenanceWorkbookSheetPreview {
  code: "overview" | "site_requisitions" | "approved_expenses" | "manager_tracking";
  name: string;
  row_count: number;
  ownership: "system" | "append_only";
}

export interface MaintenanceWorkbookPreview {
  protocol_version: string;
  sheets: MaintenanceWorkbookSheetPreview[];
  latest_tracking_month: string | null;
  last_exported_at: string | null;
  data_version: string;
}

export interface MaintenanceProjectWorkspace {
  project: MaintenanceProjectOperationsSummary;
  collection_snapshots: {
    rows: MaintenanceCollectionSnapshotRow[];
    total: number;
    page: number;
    page_size: number;
  };
  requisitions: {
    rows: MaintenanceSiteRequisitionRow[];
    total: number;
    page: number;
    page_size: number;
  };
  approved_expenses: {
    rows: MaintenanceApprovedExpenseRow[];
    total: number;
    page: number;
    page_size: number;
  };
  reminders: MaintenanceProjectReminder[];
  workbook_preview: MaintenanceWorkbookPreview;
  return_rate?: MaintenanceReturnRate | null;
  as_of: string;
  data_version: string;
}

export type SiteIssueWorkflowStatus = "draft" | "confirmed" | "corrected" | "void";

export interface SiteIssueAdapterState {
  key: string;
  state?: "unavailable" | "synthetic_ready" | string;
  production_ready: boolean;
  detail?: string;
}

export interface SiteIssueCandidate {
  delivery_line_id: string;
  source_order_id: string;
  source_line_id: string;
  delivery_no: string;
  delivery_date: string;
  part_id: number;
  pn: string;
  serial_number: string | null;
  delivered_quantity: string;
  confirmed_quantity: string;
  available_quantity: string;
  mapping_state: string;
  mapping_version: string;
}

export interface SiteIssueCandidateDirectory {
  adapter: SiteIssueAdapterState;
  rows: SiteIssueCandidate[];
  total: number;
  page: number;
  page_size: number;
}

export interface SiteIssueLine {
  issue_line_id: string;
  line_no: number;
  part_id: number;
  pn: string;
  quantity: string;
  delivery_line_id: string | null;
  source_order_id: string | null;
  source_line_id: string | null;
  serial_number: string | null;
  /** 行级返还规则：true=免返，false=必须返还，null=继承项目默认。 */
  no_return: boolean | null;
  cost_source: string | null;
  cost_source_label: string | null;
  cost_is_estimate: boolean;
  cost_amount_ex_tax: string | null;
  cost_amount_inc_tax: string | null;
  available_quantity?: string;
  requested_quantity?: string;
  cost_gap?: boolean;
  version: number;
}

export interface SiteIssueDocument {
  issue_id: string;
  project_id: string;
  issue_no: string;
  issue_date: string;
  workflow_status: SiteIssueWorkflowStatus;
  receiver: string;
  issued_by: string;
  site_location: string;
  version: number;
  lines: SiteIssueLine[];
  inventory_effect?: "none";
  idempotent_replay?: boolean;
  return_obligation_event?: {
    event_id: string;
    event_type: string;
    issue_version: number;
  } | null;
}

export interface SiteIssuePreview extends SiteIssueDocument {
  can_confirm: boolean;
  blockers: string[];
  inventory_effect: "none";
}

export interface SiteIssueDirectory {
  project_id: string;
  rows: SiteIssueDocument[];
  total: number;
  page: number;
  page_size: number;
  adapter: SiteIssueAdapterState;
}

export interface SiteIssueLineInput {
  delivery_line_id: string;
  quantity: number;
}

export interface SiteIssueDraftInput {
  idempotency_key: string;
  issue_date: string;
  receiver: string;
  issued_by: string;
  site_location: string;
  lines: SiteIssueLineInput[];
  reason: string;
}

export interface SiteIssuePatchInput {
  project_id: string;
  version: number;
  idempotency_key: string;
  issue_date?: string;
  receiver?: string;
  issued_by?: string;
  site_location?: string;
  lines?: SiteIssueLineInput[];
  reason: string;
}

export interface SiteIssueCommandInput {
  project_id: string;
  version: number;
  idempotency_key: string;
  reason: string;
}

export type MaintenanceReturnRateStatus =
  | "available"
  | "basis_incomplete"
  | "no_return_required"
  | "not_ready";

export interface MaintenanceReturnRate {
  project_id: string;
  status: MaintenanceReturnRateStatus;
  /** 官方口径数据基础：rkd_inbound（已导入收货入库单）/ rkd_not_imported（未导入，暂不发布）。 */
  official_basis: "rkd_inbound" | "rkd_not_imported" | null;
  /** 官方返还率，服务端返回定点两位小数的百分数字符串（如 "50.00"），null = 暂不发布。 */
  official_rate_pct: string | null;
  registered_rate_pct: string | null;
  warehouse_confirmed_rate_pct: string | null;
  required_quantity: string;
  registered_quantity: string;
  warehouse_confirmed_quantity: string;
  official_returned_quantity: string | null;
  outstanding_quantity: string;
  exempt_quantity: string;
  pending_quantity: string;
  required_count: number;
  exempt_count: number;
  pending_count: number;
  rkd_imported?: boolean;
  /** 返还 PN 不在领用清单中的透明警告（项目级口径不改变公式，仅提示人工复核）。 */
  pn_mismatch_warning?: string[];
  business_assumption: string;
}

export type MaintenanceReturnObligationClassification =
  | "required"
  | "exempt"
  | "pending_category";

export interface MaintenanceReturnObligation {
  obligation_id: string;
  project_id: string;
  issue_id: string;
  issue_no: string | null;
  issue_line_id: string;
  delivery_line_id: string;
  part_id: number;
  pn: string;
  source_quantity: string;
  required_quantity: string;
  classification: MaintenanceReturnObligationClassification;
  category_id_snapshot: number | null;
  category_major_snapshot: string | null;
  category_minor_snapshot: string | null;
  rule_version: string;
  source_issue_version: number;
  registered_quantity: string;
  warehouse_confirmed_quantity: string;
  remaining_quantity: string;
  is_active: boolean;
  version: number;
}

export interface MaintenanceReturnObligationDirectory {
  project_id: string;
  rows: MaintenanceReturnObligation[];
  total: number;
  page: number;
  page_size: number;
  return_rate: MaintenanceReturnRate;
}

export interface MaintenanceReturnCategory {
  category_id: number;
  category_major: string;
  category_minor: string | null;
}

export type MaintenanceBadReturnStatus =
  | "draft"
  | "submitted"
  | "in_transit"
  | "warehouse_confirmed"
  | "void";

export interface MaintenanceBadReturnLine {
  return_line_id: string;
  line_no: number;
  obligation_id: string;
  part_id: number;
  pn: string;
  quantity: string;
}

export interface MaintenanceBadReturn {
  return_id: string;
  return_no: string;
  replaces_return_id: string | null;
  project_id: string;
  status: MaintenanceBadReturnStatus;
  logistics_reference: string | null;
  warehouse_reference: string | null;
  inbound_reference: string | null;
  note: string | null;
  created_by: string;
  submitted_at: string | null;
  in_transit_at: string | null;
  warehouse_confirmed_at: string | null;
  voided_at: string | null;
  version: number;
  lines: MaintenanceBadReturnLine[];
  inventory_effect: "none";
  cost_effect: "none";
  idempotent_replay?: boolean;
}

export interface MaintenanceBadReturnDirectory {
  project_id: string;
  rows: MaintenanceBadReturn[];
  total: number;
  page: number;
  page_size: number;
}

export interface MaintenanceBadReturnCommandInput {
  project_id: string;
  version: number;
  idempotency_key: string;
  reason: string;
}

export interface MaintenanceReturnObligationSearchInput {
  project_id: string;
  q?: string;
  classifications?: MaintenanceReturnObligationClassification[];
  active_only?: boolean;
  page?: number;
  page_size?: number;
}

export interface MaintenanceWorkbookValidation {
  validation_token: string;
  project_id: string;
  data_version: string;
  filename: string;
  preview: MaintenanceWorkbookPreview;
  changes: Record<string, number>;
  warnings: string[];
  errors: string[];
  can_apply: boolean;
}

export interface MaintenanceWorkbookApplyInput {
  validation_token: string;
  data_version: string;
}

export interface MaintenanceWorkbookApplyResult {
  applied: boolean;
  changed_rows: number;
  data_version: string;
  warnings?: string[];
}

export interface MaintenanceManagerWorkbookBatchStatus {
  batch_id: string;
  status: "valid" | "error" | "applied" | "expired" | string;
  created_at: string;
  expires_at: string;
  applied_at: string | null;
  result: MaintenanceManagerWorkbookApplyResult | null;
  scope_matches_current: boolean;
}

export interface MaintenanceManagerWorkbookStatus {
  report_month: string;
  project_count: number;
  scope_version: string;
  data_version: string;
  latest_batch: MaintenanceManagerWorkbookBatchStatus | null;
  acceptance_configuration: "configured" | "pending_business_configuration" | string;
  attachment_carrier: "controlled_business_file" | "pending_business_configuration" | string;
  approval_role: "admin_only_pending_business_configuration" | "pending_business_configuration" | string;
}

export interface MaintenanceManagerWorkbookIssue {
  code: string;
  message: string;
  sheet: string | null;
  row: number | null;
  column: string | null;
}

export interface MaintenanceManagerWorkbookValidation {
  validation_token: string;
  batch_id: string;
  status: "valid" | "error" | "applied" | "expired" | string;
  report_month: string;
  data_version: string;
  file_sha256: string;
  changes: {
    service_periods: number;
    planned_collection_milestones: number;
    acceptance_due_dates: number;
    total: number;
  };
  items: MaintenanceManagerWorkbookChangePreview[];
  warnings: MaintenanceManagerWorkbookIssue[];
  errors: MaintenanceManagerWorkbookIssue[];
  unchanged: boolean;
  can_apply: boolean;
  already_applied: boolean;
  expires_at: string;
}

export interface MaintenanceManagerWorkbookChangePreview {
  kind: "service_period" | "planned_collection_milestone" | "acceptance_due_date" | string;
  project_id: string;
  project_code: string | null;
  project_name: string | null;
  project_contract_id?: string | null;
  contract_no: string | null;
  sequence: number | null;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
}

export interface MaintenanceManagerWorkbookApplyResult {
  applied: boolean;
  replayed: boolean;
  batch_id: string;
  changed_rows: number;
  project_count: number;
  warnings: number;
  report_month: string;
}

export interface MaintenanceAcceptanceAttachment {
  file_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  uploaded_by: string;
  uploaded_at: string;
}

export interface MaintenanceAcceptanceDeliverable {
  deliverable_id: string | null;
  project_id: string;
  deliverable_type: "acceptance_report";
  due_date: string | null;
  submission_status: "not_submitted" | "submitted" | string;
  submitted_at: string | null;
  submitted_by: string | null;
  approval_status: "not_reviewed" | "approved" | "rejected" | string;
  approved_at: string | null;
  approved_by: string | null;
  rejection_reason: string | null;
  configuration_state: "configured" | "pending_business_configuration" | string;
  version: number;
  review_policy: "admin_only_pending_business_role_configuration" | string;
  attachments: MaintenanceAcceptanceAttachment[];
}

export interface MaintenanceAcceptanceSearchRow {
  project_id: string;
  project_code: string;
  display_name: string;
  acceptance: MaintenanceAcceptanceDeliverable;
}

export interface MaintenanceAcceptanceDirectory {
  rows: MaintenanceAcceptanceSearchRow[];
  total: number;
  page: number;
  page_size: number;
}

export interface MaintenanceAcceptanceMutationResult {
  replayed: boolean;
  project_id: string;
  deliverable_id: string;
  version: number;
  submission_status?: string;
  approval_status?: string;
  rejection_reason?: string | null;
  file_id?: string;
}

export interface MaintenanceCostReference {
  source: "direct_purchase" | "linked_purchase" | "purchase_window" | "sales_window" | "manual" | string;
  document_no: string | null;
  document_date: string | null;
  distance_days: number | null;
  weighted_unit_price: number | null;
  sample_lines: number;
  sample_quantity: number | null;
  note?: string | null;
}

export interface MaintenanceCostGap {
  line_id: string;
  version: number;
  project_id: string;
  project_code: string;
  order_no: string;
  order_date: string | null;
  contract_no: string | null;
  pn: string | null;
  description: string | null;
  quantity: number | null;
  current_unit_cost: number | null;
  cost_source?: string | null;
  algorithm_version?: string | null;
  price_basis?: string | null;
  references: MaintenanceCostReference[];
}

export interface MaintenanceCostGapDirectory {
  rows: MaintenanceCostGap[];
  total: number;
  page: number;
  page_size: number;
  data_version: string;
}

export interface MaintenanceCostGapUpdate {
  line_id: string;
  version: number;
  unit_cost_ex_tax: number;
  evidence: string;
  reason: string;
}

export interface MaintenanceCostGapUpdateResult {
  issue_line_id: string;
  version: number;
  unit_cost: number | null;
  cost_amount: number | null;
  unit_cost_ex_tax: number | null;
  unit_cost_inc_tax: number | null;
  cost_amount_ex_tax: number | null;
  cost_amount_inc_tax: number | null;
  cost_source: string | null;
  manual_applied: boolean;
  resolution: "manual" | "automatic_evidence" | string;
}

export interface MaintenanceCostGapRecomputeInput {
  reason: string;
}

export interface MaintenanceCostGapRecomputeResult {
  resolved: number;
  remaining: number;
  data_version: string;
}

function finiteNumberOrNull(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" && typeof value !== "string") return null;
  const normalized = typeof value === "string" ? value.trim() : value;
  if (normalized === "") return null;
  if (
    typeof normalized === "string"
    && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(normalized)
  ) return null;
  if (typeof normalized === "string" && normalized.includes(".")) {
    const mantissa = normalized.toLowerCase().split("e", 1)[0];
    const significantDigits = mantissa
      .replace(/^[+-]/, "")
      .replace(".", "")
      .replace(/^0+/, "")
      .replace(/0+$/, "");
    if (significantDigits.length > 15) return null;
  }
  const parsed = typeof normalized === "number" ? normalized : Number(normalized);
  if (!Number.isFinite(parsed) || Math.abs(parsed) > Number.MAX_SAFE_INTEGER) return null;
  return parsed;
}

function incTaxBasisOrNull(value: unknown): "inc_tax" | null {
  return value === "inc_tax" ? "inc_tax" : null;
}

function normalizeContract(
  contract: MaintenanceContractSummary,
  visibility: MaintenanceCapabilities,
): MaintenanceContractSummary {
  const amountRestricted = !visibility.canViewContract
    || contract.amount_status === "restricted";
  return {
    ...contract,
    contract_amount: amountRestricted ? null : finiteNumberOrNull(contract.contract_amount),
    contract_amount_basis: incTaxBasisOrNull(contract.contract_amount_basis),
    received_amount: amountRestricted ? null : finiteNumberOrNull(contract.received_amount),
  };
}

function normalizeMetrics(
  metrics: MaintenanceOperationsMetrics,
  visibility: MaintenanceCapabilities,
): MaintenanceOperationsMetrics {
  // Completeness is a business fact, not an authorization signal.  In particular,
  // cost_complete is null when expense facts are hidden even though site-issue cost
  // and its evidence remain legitimately visible to a cost-only account.
  const contractRestricted = !visibility.canViewContract;
  const costRestricted = !visibility.canViewCost;
  const expenseRestricted = !visibility.canViewExpense;
  const aggregateCostRestricted = costRestricted || expenseRestricted;
  return {
    ...metrics,
    total_contract_amount: contractRestricted
      ? null : finiteNumberOrNull(metrics.total_contract_amount),
    known_contract_amount: contractRestricted
      ? null : finiteNumberOrNull(metrics.known_contract_amount),
    contract_amount_basis: incTaxBasisOrNull(metrics.contract_amount_basis),
    received_amount: finiteNumberOrNull(metrics.received_amount),
    collection_progress_pct: contractRestricted
      ? null : finiteNumberOrNull(metrics.collection_progress_pct),
    site_requisition_known_cost: costRestricted
      ? null : finiteNumberOrNull(metrics.site_requisition_known_cost),
    site_requisition_known_cost_ex_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.site_requisition_known_cost_ex_tax),
    site_requisition_known_cost_inc_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.site_requisition_known_cost_inc_tax),
    site_requisition_priced_cost_ex_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.site_requisition_priced_cost_ex_tax),
    site_requisition_priced_cost_inc_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.site_requisition_priced_cost_inc_tax),
    sales_estimate_cost_ex_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.sales_estimate_cost_ex_tax),
    sales_estimate_cost_inc_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.sales_estimate_cost_inc_tax),
    sales_estimate_lines: costRestricted
      ? null : finiteNumberOrNull(metrics.sales_estimate_lines),
    cost_progress_includes_sales_estimate: costRestricted
      ? null
      : typeof metrics.cost_progress_includes_sales_estimate === "boolean"
        ? metrics.cost_progress_includes_sales_estimate
        : null,
    cost_progress_label: costRestricted
      ? null
      : typeof metrics.cost_progress_label === "string"
        ? metrics.cost_progress_label
        : null,
    approved_expense: expenseRestricted
      ? null : finiteNumberOrNull(metrics.approved_expense),
    approved_expense_ex_tax: expenseRestricted
      ? null : finiteNumberOrNull(metrics.approved_expense_ex_tax),
    approved_expense_inc_tax: expenseRestricted
      ? null : finiteNumberOrNull(metrics.approved_expense_inc_tax),
    actual_project_cost_known: aggregateCostRestricted
      ? null : finiteNumberOrNull(metrics.actual_project_cost_known),
    actual_project_cost_known_ex_tax: aggregateCostRestricted
      ? null : finiteNumberOrNull(metrics.actual_project_cost_known_ex_tax),
    actual_project_cost_known_inc_tax: aggregateCostRestricted
      ? null : finiteNumberOrNull(metrics.actual_project_cost_known_inc_tax),
    cost_progress_basis: incTaxBasisOrNull(metrics.cost_progress_basis),
    cost_rate_lower_bound_pct: !visibility.canViewFinancial
      ? null : finiteNumberOrNull(metrics.cost_rate_lower_bound_pct),
    cost_status: visibility.canViewFinancial ? metrics.cost_status : null,
    cost_complete: aggregateCostRestricted ? null : metrics.cost_complete,
    missing_cost_lines: costRestricted
      ? null : finiteNumberOrNull(metrics.missing_cost_lines),
  };
}

function normalizeProjectSummary(
  project: MaintenanceProjectOperationsSummary,
  visibility: MaintenanceCapabilities,
): MaintenanceProjectOperationsSummary {
  return {
    ...project,
    manual_source_order_count: Number.isInteger(project.manual_source_order_count)
      && project.manual_source_order_count >= 0
      ? project.manual_source_order_count
      : 0,
    contracts: Array.isArray(project.contracts)
      ? project.contracts.map((contract) => normalizeContract(contract, visibility))
      : [],
    metrics: normalizeMetrics(project.metrics, visibility),
  };
}

function normalizeOperationsDirectory(
  data: MaintenanceProjectOperationsDirectory,
): MaintenanceProjectOperationsDirectory {
  if (!data || !Array.isArray(data.rows)) return data;
  const visibility = readMaintenanceCapabilities();
  return {
    ...data,
    rows: data.rows.map((project) => normalizeProjectSummary(project, visibility)),
  };
}

function normalizeWorkspace(data: MaintenanceProjectWorkspace): MaintenanceProjectWorkspace {
  if (!data?.project) return data;
  const visibility = readMaintenanceCapabilities();
  const project = normalizeProjectSummary(data.project, visibility);
  const costRestricted = !visibility.canViewCost;
  const contractRestricted = !visibility.canViewContract;
  const expenseRestricted = !visibility.canViewExpense;
  return {
    ...data,
    project,
    collection_snapshots: {
      ...data.collection_snapshots,
      rows: Array.isArray(data.collection_snapshots?.rows)
        ? data.collection_snapshots.rows.map((row) => ({
          ...row,
          cumulative_amount: contractRestricted
            ? null : finiteNumberOrNull(row.cumulative_amount),
          receipt_reference: contractRestricted ? null : row.receipt_reference,
          remark: contractRestricted ? null : row.remark,
        }))
        : [],
    },
    requisitions: {
      ...data.requisitions,
      rows: Array.isArray(data.requisitions?.rows)
        ? data.requisitions.rows.map((row) => {
          const rowCostRestricted = costRestricted || row.cost_status === "restricted";
          return {
            ...row,
            quantity: finiteNumberOrNull(row.quantity),
            unit_cost: rowCostRestricted ? null : finiteNumberOrNull(row.unit_cost),
            cost_amount: rowCostRestricted ? null : finiteNumberOrNull(row.cost_amount),
            unit_cost_ex_tax: rowCostRestricted
              ? null : finiteNumberOrNull(row.unit_cost_ex_tax),
            unit_cost_inc_tax: rowCostRestricted
              ? null : finiteNumberOrNull(row.unit_cost_inc_tax),
            cost_amount_ex_tax: rowCostRestricted
              ? null : finiteNumberOrNull(row.cost_amount_ex_tax),
            cost_amount_inc_tax: rowCostRestricted
              ? null : finiteNumberOrNull(row.cost_amount_inc_tax),
            cost_source: rowCostRestricted ? null : row.cost_source,
            cost_evidence_kind: rowCostRestricted
              ? null : (row.cost_evidence_kind ?? null),
            cost_is_estimate: rowCostRestricted
              ? null
              : typeof row.cost_is_estimate === "boolean"
                ? row.cost_is_estimate
                : null,
            cost_source_label: rowCostRestricted
              ? null : (row.cost_source_label ?? null),
            cost_status: rowCostRestricted ? "restricted" : row.cost_status,
          };
        })
        : [],
    },
    approved_expenses: expenseRestricted
      ? { ...data.approved_expenses, rows: [], total: 0 }
      : {
        ...data.approved_expenses,
        rows: Array.isArray(data.approved_expenses?.rows)
          ? data.approved_expenses.rows.map((row) => ({
            ...row,
            amount: finiteNumberOrNull(row.amount),
            amount_ex_tax: finiteNumberOrNull(row.amount_ex_tax),
            amount_inc_tax: finiteNumberOrNull(row.amount_inc_tax),
          }))
          : [],
      },
  };
}

function normalizeCostGap(gap: MaintenanceCostGap): MaintenanceCostGap {
  return {
    ...gap,
    quantity: finiteNumberOrNull(gap.quantity),
    current_unit_cost: finiteNumberOrNull(gap.current_unit_cost),
    references: Array.isArray(gap.references)
      ? gap.references.map((reference) => ({
        ...reference,
        distance_days: finiteNumberOrNull(reference.distance_days),
        weighted_unit_price: finiteNumberOrNull(reference.weighted_unit_price),
        sample_quantity: finiteNumberOrNull(reference.sample_quantity),
      }))
      : [],
  };
}

function normalizeCostGapDirectory(
  data: MaintenanceCostGapDirectory,
): MaintenanceCostGapDirectory {
  if (!data || !Array.isArray(data.rows)) return data;
  return { ...data, rows: data.rows.map(normalizeCostGap) };
}

function normalizeCostGapUpdateResult(
  data: MaintenanceCostGapUpdateResult,
): MaintenanceCostGapUpdateResult {
  return {
    ...data,
    unit_cost: finiteNumberOrNull(data.unit_cost),
    cost_amount: finiteNumberOrNull(data.cost_amount),
    unit_cost_ex_tax: finiteNumberOrNull(data.unit_cost_ex_tax),
    unit_cost_inc_tax: finiteNumberOrNull(data.unit_cost_inc_tax),
    cost_amount_ex_tax: finiteNumberOrNull(data.cost_amount_ex_tax),
    cost_amount_inc_tax: finiteNumberOrNull(data.cost_amount_inc_tax),
  };
}

export interface MaintenanceOperationsListParams {
  page?: number;
  page_size?: number;
  q?: string;
  lifecycle?: string;
  reminder?: string;
  include_inactive?: boolean;
  owner_scope?: "me" | "all";
  task_type?: string;
  task_status?: "open" | "pending" | "completed";
  due_from?: string;
  due_to?: string;
}

export const listMaintenanceProjectOperations = (
  params: MaintenanceOperationsListParams = {},
  options: { signal?: AbortSignal } = {},
) => {
  const request = {
    ...params,
    q: params.q?.trim() || "",
    include_inactive: params.include_inactive ?? false,
  };
  const response = options.signal
    ? api.post<MaintenanceProjectOperationsDirectory>(
      "/maintenance/projects/stable/operations/search",
      request,
      { signal: options.signal },
    )
    : api.post<MaintenanceProjectOperationsDirectory>(
      "/maintenance/projects/stable/operations/search",
      request,
    );
  return response.then((result) => ({
    ...result,
    data: normalizeOperationsDirectory(result.data),
  }));
};

export interface MaintenanceManagerAccount {
  user_id: number;
  username: string;
  display_name: string | null;
  is_active: boolean;
}

export interface MaintenanceManagerAccountDirectory {
  rows: MaintenanceManagerAccount[];
  total: number;
  page: number;
  page_size: number;
}

export interface MaintenanceManagerAssignmentInput {
  user_id: number;
  expected_assignment_id?: string | null;
  expected_assignment_version?: number | null;
  reason: string;
}

const projectBase = (projectId: string) =>
  `/maintenance/projects/stable/${encodeURIComponent(projectId)}`;

export const searchMaintenanceManagerAccounts = (
  input: { q?: string; page?: number; page_size?: number } = {},
  options: { signal?: AbortSignal } = {},
) => {
  const request = {
    q: input.q?.trim() || "",
    page: input.page ?? 1,
    page_size: input.page_size ?? 20,
  };
  return options.signal
    ? api.post<MaintenanceManagerAccountDirectory>(
      "/maintenance/project-manager-assignments/search",
      request,
      { signal: options.signal },
    )
    : api.post<MaintenanceManagerAccountDirectory>(
      "/maintenance/project-manager-assignments/search",
      request,
    );
};

export const assignMaintenanceProjectManager = (
  projectId: string,
  input: MaintenanceManagerAssignmentInput,
) => api.post<MaintenanceManagerAssignment>(
  `${projectBase(projectId)}/manager-assignment`,
  input,
);

export const archiveMaintenanceProjectManager = (
  assignmentId: string,
  input: { version: number; reason: string },
) => api.post<MaintenanceManagerAssignment>(
  `/maintenance/project-manager-assignments/${encodeURIComponent(assignmentId)}/archive`,
  input,
);

export interface MaintenanceWorkspaceParams {
  collection_page?: number;
  collection_page_size?: number;
  requisition_page?: number;
  requisition_page_size?: number;
  expense_page?: number;
  expense_page_size?: number;
}

export const getMaintenanceProjectWorkspace = (
  projectId: string,
  params: MaintenanceWorkspaceParams = {},
) =>
  api.get<MaintenanceProjectWorkspace>(`${projectBase(projectId)}/workspace`, { params })
    .then((response) => ({ ...response, data: normalizeWorkspace(response.data) }));

export const downloadMaintenanceProjectWorkbook = (projectId: string) =>
  api.get<Blob>(`${projectBase(projectId)}/workbook`, { responseType: "blob" });

export const downloadMaintenanceWorkbookValidationErrors = (validationToken: string) =>
  api.get<Blob>(
    `/maintenance/workbook-validations/${encodeURIComponent(validationToken)}/errors.xlsx`,
    { responseType: "blob" },
  );

export const validateMaintenanceProjectWorkbook = (projectId: string, file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<MaintenanceWorkbookValidation>(
    `${projectBase(projectId)}/workbook/validate`,
    form,
    { timeout: 120000 },
  );
};

export const applyMaintenanceProjectWorkbook = (
  projectId: string,
  input: MaintenanceWorkbookApplyInput,
) => api.post<MaintenanceWorkbookApplyResult>(
  `${projectBase(projectId)}/workbook/apply`,
  input,
);

export const getMaintenanceManagerWorkbookStatus = (reportMonth: string) =>
  api.get<MaintenanceManagerWorkbookStatus>(
    "/maintenance/project-manager/workbooks/v3/status",
    { params: { report_month: reportMonth } },
  );

export const downloadMaintenanceManagerWorkbook = (reportMonth: string) =>
  api.get<Blob>("/maintenance/project-manager/workbooks/v3", {
    params: { report_month: reportMonth },
    responseType: "blob",
  });

export const validateMaintenanceManagerWorkbook = (
  reportMonth: string,
  file: File,
) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<MaintenanceManagerWorkbookValidation>(
    "/maintenance/project-manager/workbooks/v3/validate",
    form,
    { params: { report_month: reportMonth }, timeout: 120000 },
  );
};

export const applyMaintenanceManagerWorkbook = (
  input: { validation_token: string; data_version: string },
) => api.post<MaintenanceManagerWorkbookApplyResult>(
  "/maintenance/project-manager/workbooks/v3/apply",
  input,
);

export const searchMaintenanceAcceptance = (
  input: {
    q?: string;
    submission_status?: "not_submitted" | "submitted" | "not_configured";
    approval_status?: "not_reviewed" | "approved" | "rejected";
    page?: number;
    page_size?: number;
  } = {},
) => api.post<MaintenanceAcceptanceDirectory>(
  "/maintenance/acceptance-deliverables/search",
  {
    q: input.q?.trim() || "",
    submission_status: input.submission_status,
    approval_status: input.approval_status,
    page: input.page ?? 1,
    page_size: input.page_size ?? 24,
  },
);

export const getMaintenanceAcceptance = (projectId: string) =>
  api.get<MaintenanceAcceptanceDeliverable>(`${projectBase(projectId)}/acceptance`);

export const uploadMaintenanceAcceptanceAttachment = (
  projectId: string,
  input: { file: File; idempotencyKey?: string },
) => {
  // 2026-08-25 客户口径：一个上传口——只传文件本身（无版本握手）。
  const form = new FormData();
  form.append("file", input.file);
  return api.post<MaintenanceAcceptanceMutationResult>(
    `${projectBase(projectId)}/acceptance/attachments`,
    form,
    {
      headers: input.idempotencyKey
        ? { "Idempotency-Key": input.idempotencyKey }
        : undefined,
      timeout: 120000,
    },
  );
};

export const deleteMaintenanceAcceptanceAttachment = (
  projectId: string,
  fileId: string,
) => api.delete<{ file_id: string; archived: boolean }>(
  `${projectBase(projectId)}/acceptance/attachments/${encodeURIComponent(fileId)}`,
);

export const submitMaintenanceAcceptance = (
  projectId: string,
  input: { expected_version: number; idempotencyKey: string },
) => api.post<MaintenanceAcceptanceMutationResult>(
  `${projectBase(projectId)}/acceptance/submit`,
  { expected_version: input.expected_version },
  { headers: { "Idempotency-Key": input.idempotencyKey } },
);

export const downloadMaintenanceAcceptanceAttachment = (fileId: string) =>
  api.get<Blob>(`/maintenance/acceptance-files/${encodeURIComponent(fileId)}`, {
    responseType: "blob",
  });

// ---- 验收需求清单（2026-08-21 客户反馈：验收需求/是否完成 两列 Excel 导入） ----

export interface MaintenanceAcceptanceChecklistItem {
  item_id: string;
  row_no: number;
  requirement: string;
  done: boolean | null;
}

export interface MaintenanceAcceptanceChecklistCurrent {
  batch_id: string;
  filename: string;
  uploaded_by: string;
  applied_by: string;
  applied_at: string | null;
  item_rows: number;
  done_rows: number;
  todo_rows: number;
  items: MaintenanceAcceptanceChecklistItem[];
}

export interface MaintenanceAcceptanceChecklist {
  current: MaintenanceAcceptanceChecklistCurrent | null;
  history: {
    batch_id: string;
    filename: string;
    applied_by: string;
    applied_at: string | null;
    item_rows: number;
  }[];
}

export interface MaintenanceAcceptanceChecklistPreview {
  batch_id: string;
  file_hash: string;
  item_rows: number;
  issue_rows: number;
  done_rows: number;
  todo_rows: number;
  issues: string[];
  will_replace_rows: number;
}

export const getMaintenanceAcceptanceChecklist = (projectId: string) =>
  api.get<MaintenanceAcceptanceChecklist>(
    `${projectBase(projectId)}/acceptance/checklist`);

export const downloadAcceptanceChecklistTemplate = (projectId: string) =>
  api.get<Blob>(
    `${projectBase(projectId)}/acceptance/checklist/template`,
    { responseType: "blob" },
  );

export const previewMaintenanceAcceptanceChecklist = (
  projectId: string,
  input: { file: File; idempotencyKey: string },
) => {
  const form = new FormData();
  form.append("file", input.file);
  return api.post<MaintenanceAcceptanceChecklistPreview>(
    `${projectBase(projectId)}/acceptance/checklist/preview`,
    form,
    { headers: { "Idempotency-Key": input.idempotencyKey }, timeout: 120000 },
  );
};

export const applyMaintenanceAcceptanceChecklist = (batchId: string) =>
  api.post<{ batch_id: string; item_rows: number; done_rows: number;
             todo_rows: number; replaced_batch_id: string | null }>(
    `/maintenance/acceptance-checklist/${encodeURIComponent(batchId)}/apply`);

export const listMaintenanceCostGaps = (
  projectId: string,
  params: { page?: number; page_size?: number } = {},
) => api.get<MaintenanceCostGapDirectory>(`${projectBase(projectId)}/cost-gaps`, { params })
  .then((response) => ({ ...response, data: normalizeCostGapDirectory(response.data) }));

export const recomputeMaintenanceCostGaps = (
  projectId: string,
  input: MaintenanceCostGapRecomputeInput,
) => api.post<MaintenanceCostGapRecomputeResult>(
  `${projectBase(projectId)}/cost-gaps/recompute`,
  input,
);

export const updateMaintenanceCostGap = (
  projectId: string,
  input: MaintenanceCostGapUpdate,
) => api.patch<MaintenanceCostGapUpdateResult>(`${projectBase(projectId)}/cost-gaps`, input)
  .then((response) => ({ ...response, data: normalizeCostGapUpdateResult(response.data) }));

export const searchSiteIssueCandidates = (
  projectId: string,
  input: { q?: string; page?: number; page_size?: number } = {},
) => api.post<SiteIssueCandidateDirectory>(
  `/maintenance/site-issues/projects/${encodeURIComponent(projectId)}/candidates/search`,
  {
    ...(input.q?.trim() ? { q: input.q.trim() } : {}),
    page: input.page ?? 1,
    page_size: input.page_size ?? 50,
  },
);

export const searchSiteIssues = (
  input: {
    project_id: string;
    q?: string;
    workflow_statuses?: SiteIssueWorkflowStatus[];
    page?: number;
    page_size?: number;
  },
) => api.post<SiteIssueDirectory>("/maintenance/site-issues/search", {
  project_id: input.project_id,
  ...(input.q?.trim() ? { q: input.q.trim() } : {}),
  workflow_statuses: input.workflow_statuses
    ?? ["draft", "confirmed", "corrected", "void"],
  page: input.page ?? 1,
  page_size: input.page_size ?? 20,
});

export const createSiteIssueDraft = (
  projectId: string,
  input: SiteIssueDraftInput,
) => api.post<SiteIssueDocument>(
  `/maintenance/site-issues/projects/${encodeURIComponent(projectId)}`,
  input,
);

const siteIssueBase = (issueId: string) =>
  `/maintenance/site-issues/${encodeURIComponent(issueId)}`;

export const patchSiteIssue = (
  issueId: string,
  input: SiteIssuePatchInput,
) => api.patch<SiteIssueDocument>(siteIssueBase(issueId), input);

export const previewSiteIssue = (
  issueId: string,
  input: Pick<SiteIssueCommandInput, "project_id" | "version">,
) => api.post<SiteIssuePreview>(`${siteIssueBase(issueId)}/preview`, input);

export const confirmSiteIssue = (
  issueId: string,
  input: SiteIssueCommandInput,
) => api.post<SiteIssueDocument>(`${siteIssueBase(issueId)}/confirm`, input);

export const voidSiteIssue = (
  issueId: string,
  input: SiteIssueCommandInput,
) => api.post<SiteIssueDocument>(`${siteIssueBase(issueId)}/void`, input);

export const searchMaintenanceReturnObligations = (
  input: MaintenanceReturnObligationSearchInput,
) => api.post<MaintenanceReturnObligationDirectory>(
  "/maintenance/return-obligations/search",
  {
    project_id: input.project_id,
    ...(input.q?.trim() ? { q: input.q.trim() } : {}),
    ...(input.classifications ? { classifications: input.classifications } : {}),
    ...(input.active_only == null ? {} : { active_only: input.active_only }),
    page: input.page ?? 1,
    page_size: input.page_size ?? 50,
  },
);

export const searchMaintenanceBadReturns = (input: {
  project_id: string;
  statuses?: MaintenanceBadReturnStatus[];
  page?: number;
  page_size?: number;
}) => api.post<MaintenanceBadReturnDirectory>("/maintenance/bad-returns/search", {
  project_id: input.project_id,
  ...(input.statuses ? { statuses: input.statuses } : {}),
  page: input.page ?? 1,
  page_size: input.page_size ?? 20,
});

export const createMaintenanceBadReturnDraft = (input: {
  project_id: string;
  idempotency_key: string;
  replaces_return_id?: string;
  lines: { obligation_id: string; quantity: number }[];
  note?: string;
  reason: string;
}) => api.post<MaintenanceBadReturn>("/maintenance/bad-returns", input);

const badReturnBase = (returnId: string) =>
  `/maintenance/bad-returns/${encodeURIComponent(returnId)}`;

export const submitMaintenanceBadReturn = (
  returnId: string,
  input: MaintenanceBadReturnCommandInput,
) => api.post<MaintenanceBadReturn>(`${badReturnBase(returnId)}/submit`, input);

export const markMaintenanceBadReturnInTransit = (
  returnId: string,
  input: MaintenanceBadReturnCommandInput & { logistics_reference: string },
) => api.post<MaintenanceBadReturn>(`${badReturnBase(returnId)}/in-transit`, input);

export const confirmMaintenanceBadReturnWarehouse = (
  returnId: string,
  input: MaintenanceBadReturnCommandInput & {
    warehouse_reference: string;
    inbound_reference?: string;
  },
) => api.post<MaintenanceBadReturn>(`${badReturnBase(returnId)}/warehouse-confirm`, input);

export const voidMaintenanceBadReturn = (
  returnId: string,
  input: MaintenanceBadReturnCommandInput,
) => api.post<MaintenanceBadReturn>(`${badReturnBase(returnId)}/void`, input);

export const listMaintenanceReturnCategories = () => api.get<{
  categories: MaintenanceReturnCategory[];
}>("/maintenance/return-categories");

export const resolveMaintenanceReturnObligationCategory = (
  obligationId: string,
  input: {
    project_id: string;
    version: number;
    category_id: number;
    idempotency_key: string;
    reason: string;
  },
) => api.post<MaintenanceReturnObligation>(
  `/maintenance/return-obligations/${encodeURIComponent(obligationId)}/resolve-category`,
  input,
);

// ===== 前置库 / 官方返还率 / 收回清单 / 坏件变卖 / 工作簿 v3 / 回款凭证 / 报销对账 =====
// 全部挂在项目稳定路径下：登录 + page_maintenance + 项目范围；
// 金额字段按 data_purchase_cost / data_profit 脱敏，缺数据一律 null，前端不得伪造。

export interface MaintenanceFrontStockRow {
  stock_id: string;
  part_id: number;
  pn: string;
  description: string | null;
  warehouse_name: string;
  qty: number;
  unit_cost_ex_tax: number | null;
  unit_cost_inc_tax: number | null;
  value_ex_tax: number | null;
  value_inc_tax: number | null;
  last_inbound_at: string | null;
  age_days: number | null;
  last_consumed_at: string | null;
  days_since_last_consumption: number | null;
  /** 入库超 90 天且（从未领用或最近领用也超 90 天）；新入库未领用件不算超期。 */
  stale_90d: boolean;
}

export type MaintenanceFrontStockCompleteness =
  | "complete"
  | "incomplete"
  | "not_visible";

export interface MaintenanceFrontStockSummary {
  project_id: string;
  rows: MaintenanceFrontStockRow[];
  total_qty: number;
  total_value_ex_tax: number | null;
  total_value_inc_tax: number | null;
  value_completeness: MaintenanceFrontStockCompleteness;
  /** 当前账号是否可见成本/估值；false 时金额字段恒为 null。 */
  cost_visible: boolean;
  stale_90d_count: number;
}

export const getMaintenanceProjectFrontStock = (projectId: string) =>
  api.get<MaintenanceFrontStockSummary>(`${projectBase(projectId)}/front-stock`);

/** 官方返还率（独立端点；与 workspace 内嵌的 return_rate 同源同形状）。 */
export const getMaintenanceProjectReturnRate = (projectId: string) =>
  api.get<MaintenanceReturnRate>(`${projectBase(projectId)}/return-rate`);

export interface MaintenanceRecoveryGoodReturnedRow {
  source_ref: string;
  qty: number;
  occurred_at: string | null;
  reason: string | null;
}

export interface MaintenanceRecoveryBadReturnedRow {
  head_no: string;
  pn: string;
  part_id: number;
  qty: number;
  test_result: string | null;
  occurred_at: string | null;
}

export interface MaintenanceRecoverySummary {
  project_id: string;
  good_returned: MaintenanceRecoveryGoodReturnedRow[];
  good_returned_total_qty: number;
  bad_returned: MaintenanceRecoveryBadReturnedRow[];
  bad_returned_total_qty: number;
  remaining_stock: MaintenanceFrontStockRow[];
  remaining_total_qty: number;
}

export const getMaintenanceProjectRecoverySummary = (projectId: string) =>
  api.get<MaintenanceRecoverySummary>(`${projectBase(projectId)}/recovery-summary`);

export interface MaintenanceSalvageRow {
  salvage_id: string;
  pn: string;
  part_id: number | null;
  qty: number;
  /** 变卖收入（含税，登记原值）。 */
  revenue: number;
  salvage_date: string;
  buyer_note: string | null;
  reason: string | null;
  operated_by: string;
  is_active: boolean;
  version: number;
  stock_deducted: boolean;
  /** 登记时冻结的成本证据（含税）；缺成本不按 0。 */
  cost_basis_inc_tax: number | null;
  cost_source_ref: string | null;
  cost_algorithm_version: string | null;
  /** 贡献毛利（含税）= 收入 − 冻结成本 × 数量；缺成本为 null。 */
  margin: number | null;
}

export interface MaintenanceSalvageDirectory {
  project_id: string;
  rows: MaintenanceSalvageRow[];
  active_count: number;
  total_revenue: number;
  total_margin: number | null;
  margin_completeness: "complete" | "incomplete";
}

/** 坏件变卖清单：读端要求 data_profit（含成本与毛利），无权限 403。 */
export const listMaintenanceSalvages = (projectId: string) =>
  api.get<MaintenanceSalvageDirectory>(`${projectBase(projectId)}/salvages`);

/** 项目工作簿 v3 导出（要求同时具备 data_profit 与 data_purchase_cost）。 */
export const downloadMaintenanceProjectWorkbookV3 = (projectId: string) =>
  api.get<Blob>(`${projectBase(projectId)}/workbook-v3.xlsx`, { responseType: "blob" });

export interface MaintenanceCollectionEvidenceRow {
  evidence_id: string;
  file_id: string;
  md5: string;
  sha256: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  uploaded_by: string;
  uploaded_at: string;
  is_active: boolean;
}

export interface MaintenanceCollectionEvidenceDirectory {
  milestone_id: string;
  rows: MaintenanceCollectionEvidenceRow[];
}

export interface MaintenanceCollectionEvidenceUploadResult {
  evidence_id: string;
  file_id: string;
  milestone_id: string;
  md5: string;
  replayed: boolean;
  object_key?: string;
  sha256?: string;
  size_bytes?: number;
  original_filename?: string;
  mime_type?: string;
  /** 上传凭证即关闭回款提醒：true = 本节点已闭环。 */
  closed?: boolean;
  close_reason?: string | null;
}

export const listMaintenanceCollectionEvidence = (milestoneId: string) =>
  api.get<MaintenanceCollectionEvidenceDirectory>(
    `/maintenance/collection-milestones/${encodeURIComponent(milestoneId)}/evidence`,
  );

export const uploadMaintenanceCollectionEvidence = (
  milestoneId: string,
  file: File,
) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<MaintenanceCollectionEvidenceUploadResult>(
    `/maintenance/collection-milestones/${encodeURIComponent(milestoneId)}/evidence`,
    form,
    { timeout: 120000 },
  );
};

export type MaintenanceExpenseReconcileStatus =
  | "matched"
  | "mismatch"
  | "unresolved"
  | "ledger_only"
  | "bxd_only"
  | "formal_only";

export interface MaintenanceExpenseReconcileRow {
  bxd_no: string;
  status: MaintenanceExpenseReconcileStatus;
  conclusion_basis: string | null;
  /** 氚云 BXD 原值与正式费用事实是否一致（第三源证据，不参与结论）。 */
  bxd_aligned: boolean;
  ledger_amount: number | null;
  bxd_amount: number | null;
  formal_amount: number | null;
  ledger_present: boolean;
  bxd_present: boolean;
  formal_present: boolean;
  ledger_row_count: number;
  bxd_line_count: number;
  formal_row_count: number;
  ledger_project_name: string | null;
}

export interface MaintenanceExpenseReconcileDirectory {
  rows: MaintenanceExpenseReconcileRow[];
  limit: number;
  offset: number;
  matched: number;
  mismatch: number;
  unresolved: number;
  ledger_only: number;
  bxd_only: number;
  formal_only: number;
}

/** 报销对账（仅 admin/boss + data_profit）。 */
export const getMaintenanceExpenseReconcile = (
  params: { limit?: number; offset?: number } = {},
) => api.get<MaintenanceExpenseReconcileDirectory>(
  "/maintenance/reconcile/expenses",
  { params },
);
