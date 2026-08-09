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
  contracts: MaintenanceContractSummary[];
  metrics: MaintenanceOperationsMetrics;
  reminder_count: number;
  manager_assignment: MaintenanceManagerAssignment | null;
  task_summary: MaintenanceProjectTaskSummary;
  missing_data_labels: string[];
  attachment_status: "not_integrated" | "missing" | "available" | string;
  as_of: string;
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
  as_of: string;
  data_version: string;
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
}

export interface MaintenanceManagerWorkbookStatus {
  report_month: string;
  project_count: number;
  scope_version: string;
  data_version: string;
  latest_batch: MaintenanceManagerWorkbookBatchStatus | null;
  acceptance_configuration: "configured" | "pending_business_configuration" | string;
  attachment_carrier: "pending_business_configuration" | string;
  approval_role: "pending_business_configuration" | string;
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
    total: number;
  };
  warnings: MaintenanceManagerWorkbookIssue[];
  errors: MaintenanceManagerWorkbookIssue[];
  unchanged: boolean;
  can_apply: boolean;
  already_applied: boolean;
  expires_at: string;
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
