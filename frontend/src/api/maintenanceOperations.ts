import { api } from "../api";

export type MaintenanceLifecycleStatus = "ongoing" | "ended" | "missing" | string;
export type ProjectReminderSeverity = "info" | "warning" | "critical";

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
  reminder_count: number;
  as_of: string;
}

export interface MaintenanceProjectOperationsDirectory {
  rows: MaintenanceProjectOperationsSummary[];
  total: number;
  page: number;
  page_size: number;
  as_of: string;
  data_version: string;
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
): MaintenanceContractSummary {
  const amountRestricted = contract.amount_status === "restricted";
  return {
    ...contract,
    contract_amount: amountRestricted ? null : finiteNumberOrNull(contract.contract_amount),
    contract_amount_basis: incTaxBasisOrNull(contract.contract_amount_basis),
    received_amount: finiteNumberOrNull(contract.received_amount),
  };
}

function normalizeMetrics(metrics: MaintenanceOperationsMetrics): MaintenanceOperationsMetrics {
  const contractRestricted = metrics.contract_amount_complete === null;
  const costRestricted = metrics.cost_complete === null;
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
    approved_expense: costRestricted
      ? null : finiteNumberOrNull(metrics.approved_expense),
    approved_expense_ex_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.approved_expense_ex_tax),
    approved_expense_inc_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.approved_expense_inc_tax),
    actual_project_cost_known: costRestricted
      ? null : finiteNumberOrNull(metrics.actual_project_cost_known),
    actual_project_cost_known_ex_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.actual_project_cost_known_ex_tax),
    actual_project_cost_known_inc_tax: costRestricted
      ? null : finiteNumberOrNull(metrics.actual_project_cost_known_inc_tax),
    cost_progress_basis: incTaxBasisOrNull(metrics.cost_progress_basis),
    cost_rate_lower_bound_pct: costRestricted
      ? null : finiteNumberOrNull(metrics.cost_rate_lower_bound_pct),
    cost_status: costRestricted ? null : metrics.cost_status,
  };
}

function normalizeProjectSummary(
  project: MaintenanceProjectOperationsSummary,
): MaintenanceProjectOperationsSummary {
  return {
    ...project,
    manual_source_order_count: Number.isInteger(project.manual_source_order_count)
      && project.manual_source_order_count >= 0
      ? project.manual_source_order_count
      : 0,
    contracts: Array.isArray(project.contracts) ? project.contracts.map(normalizeContract) : [],
    metrics: normalizeMetrics(project.metrics),
  };
}

function normalizeOperationsDirectory(
  data: MaintenanceProjectOperationsDirectory,
): MaintenanceProjectOperationsDirectory {
  if (!data || !Array.isArray(data.rows)) return data;
  return { ...data, rows: data.rows.map(normalizeProjectSummary) };
}

function normalizeWorkspace(data: MaintenanceProjectWorkspace): MaintenanceProjectWorkspace {
  if (!data?.project) return data;
  const project = normalizeProjectSummary(data.project);
  const costRestricted = project.metrics.cost_complete === null;
  return {
    ...data,
    project,
    collection_snapshots: {
      ...data.collection_snapshots,
      rows: Array.isArray(data.collection_snapshots?.rows)
        ? data.collection_snapshots.rows.map((row) => ({
          ...row,
          cumulative_amount: finiteNumberOrNull(row.cumulative_amount),
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
          };
        })
        : [],
    },
    approved_expenses: {
      ...data.approved_expenses,
      rows: Array.isArray(data.approved_expenses?.rows)
        ? data.approved_expenses.rows.map((row) => ({
          ...row,
          amount: costRestricted ? null : finiteNumberOrNull(row.amount),
          amount_ex_tax: costRestricted ? null : finiteNumberOrNull(row.amount_ex_tax),
          amount_inc_tax: costRestricted ? null : finiteNumberOrNull(row.amount_inc_tax),
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
}

export const listMaintenanceProjectOperations = (
  params: MaintenanceOperationsListParams = {},
) => api.get<MaintenanceProjectOperationsDirectory>("/maintenance/projects/stable/operations", {
  params: { ...params, include_inactive: params.include_inactive ?? false },
}).then((response) => ({ ...response, data: normalizeOperationsDirectory(response.data) }));

const projectBase = (projectId: string) =>
  `/maintenance/projects/stable/${encodeURIComponent(projectId)}`;

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
