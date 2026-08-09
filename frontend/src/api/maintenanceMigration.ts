import { api } from "../api";

export type MigrationRunStatus = "previewed" | "reconciled" | "approved";

export interface MigrationHistoricalBaselineInput {
  amount_ex_tax: string;
  amount_inc_tax: string;
  evidence_hash: string;
}

export interface MigrationOpeningBalanceInput {
  balance_key: string;
  pn?: string | null;
  quantity: string;
  evidence_hash: string;
}

export interface MigrationProjectInput {
  project_id: string;
  cutover_date: string;
  historical_mode: "approved_cost_baseline" | "stable_site_issues";
  historical_baseline?: MigrationHistoricalBaselineInput | null;
  opening_balances: MigrationOpeningBalanceInput[];
}

export interface MigrationPreviewInput {
  idempotency_key: string;
  reason: string;
  projects: MigrationProjectInput[];
}

export interface MigrationBlocker {
  code: string;
  entity_id?: string | null;
  detail: string;
}

export interface MigrationProjectPreview {
  project_id: string;
  cutover_date: string;
  source_snapshot_hash: string;
  source_coverage: {
    warehouse_source_ready: boolean;
    project_version: number;
    [key: string]: unknown;
  };
  evidence_summary: {
    historical_baseline: number;
    historical_site_issues: number;
    post_cutover_site_issues: number;
    expenses: number;
    opening_balances: number;
    inventory_movements: number;
    [key: string]: number;
  };
  cost: {
    historical_baseline_ex_tax: string;
    historical_baseline_inc_tax: string;
    post_cutover_consumption_ex_tax: string;
    post_cutover_consumption_inc_tax: string;
    approved_expense_ex_tax: string;
    approved_expense_inc_tax: string;
    total_ex_tax: string;
    total_inc_tax: string;
  };
  inventory: Array<{
    balance_key: string;
    opening_quantity: string;
    delivery_quantity: string;
    available_receipt_quantity: string;
    closing_quantity: string;
    ignored_site_issue_quantity: string;
    ignored_return_registration_quantity: string;
  }>;
  approval_blockers: MigrationBlocker[];
  can_approve: boolean;
}

export interface MigrationPlanDetail {
  plan_id: string;
  project_id: string;
  cutover_date: string;
  historical_mode: string;
  blocker_count: number;
  status: MigrationRunStatus;
  reconciled_by: string | null;
  reconciled_at: string | null;
  reconciliation_reason: string | null;
  version: number;
  cost: {
    historical_ex_tax: string;
    historical_inc_tax: string;
    post_cutover_ex_tax: string;
    post_cutover_inc_tax: string;
    approved_expense_ex_tax: string;
    approved_expense_inc_tax: string;
    total_ex_tax: string;
    total_inc_tax: string;
  };
  historical_baseline: null | {
    baseline_id: string;
    amount_ex_tax: string;
    amount_inc_tax: string;
    evidence_hash: string;
    approval_state: "pending" | "approved";
    approved_by: string | null;
    approved_at: string | null;
    approval_reason: string | null;
    version: number;
  };
  opening_balances: Array<{
    opening_balance_id: string;
    balance_key: string;
    pn: string | null;
    quantity: string;
    evidence_hash: string;
    approval_state: "pending" | "approved";
    approved_by: string | null;
    approved_at: string | null;
    approval_reason: string | null;
    version: number;
  }>;
  discrepancies: Array<{
    discrepancy_id: string;
    code: string;
    entity_id: string | null;
    severity: "blocker" | "warning";
    status: "open" | "resolved";
    detail: { detail?: string };
    resolved_by: string | null;
    version: number;
  }>;
}

export type MigrationEvidenceSection =
  | "historical_site_issues"
  | "post_cutover_site_issues"
  | "expenses"
  | "opening_balances"
  | "inventory_movements";

export type MigrationEvidenceRow = Record<string, unknown>;

export interface MigrationEvidencePage {
  run_id: string;
  project_id: string;
  section: MigrationEvidenceSection;
  source_snapshot_hash: string;
  items: MigrationEvidenceRow[];
  total: number;
  page: number;
  page_size: number;
}

export interface MigrationProjectSignoff {
  project_id: string;
  expected_plan_version: number;
  reason: string;
  historical_baseline: null | {
    baseline_id: string;
    expected_version: number;
  };
  opening_balances: Array<{
    opening_balance_id: string;
    expected_version: number;
  }>;
}

export interface MigrationRunDetail {
  run_id: string;
  status: MigrationRunStatus;
  rule_version: string;
  request_fingerprint: string;
  source_snapshot_hash: string;
  preview: {
    input_fingerprint: string;
    approval_blocker_count: number;
    can_approve: boolean;
    projects: MigrationProjectPreview[];
    production_activation_included: false;
  };
  manifest: Record<string, unknown> | null;
  manifest_hash: string | null;
  manifest_key_id: string | null;
  created_by: string;
  reconciled_by: string | null;
  approved_by: string | null;
  version: number;
  created_at: string;
  plans: MigrationPlanDetail[];
  events: Array<{
    event_id: string;
    action: "preview" | "reconcile" | "approve";
    from_status: MigrationRunStatus | null;
    to_status: MigrationRunStatus;
    reason: string;
    operated_by: string;
    operated_at: string;
  }>;
  production_activation_included: false;
}

export interface MigrationRunSummary {
  run_id: string;
  status: MigrationRunStatus;
  rule_version: string;
  source_snapshot_hash: string;
  manifest_key_id?: string | null;
  blocker_count: number;
  created_by: string;
  reconciled_by: string | null;
  approved_by: string | null;
  version: number;
  created_at: string;
}

export const previewMaintenanceMigration = (body: MigrationPreviewInput) =>
  api.post<MigrationRunDetail>("/maintenance/migration-runs/preview", body);

export const searchMaintenanceMigrationRuns = (body: {
  statuses: MigrationRunStatus[];
  page: number;
  page_size: number;
}) => api.post<{
  items: MigrationRunSummary[];
  total: number;
  page: number;
  page_size: number;
}>("/maintenance/migration-runs/search", body);

export const getMaintenanceMigrationRun = (runId: string) =>
  api.get<MigrationRunDetail>(`/maintenance/migration-runs/${runId}`);

export const getMaintenanceMigrationEvidence = (
  runId: string,
  projectId: string,
  params: {
    section: MigrationEvidenceSection;
    page: number;
    page_size: number;
  },
) => api.get<MigrationEvidencePage>(
  `/maintenance/migration-runs/${runId}/projects/${projectId}/evidence`,
  { params },
);

export const getMaintenanceMigrationManifest = (runId: string) =>
  api.get<Record<string, unknown>>(`/maintenance/migration-runs/${runId}/manifest`);

export const reconcileMaintenanceMigrationRun = (
  runId: string,
  body: {
    expected_version: number;
    operation_key: string;
    reason: string;
    project_signoffs: MigrationProjectSignoff[];
  },
) => api.post<MigrationRunDetail>(
  `/maintenance/migration-runs/${runId}/reconcile`,
  body,
);

export const approveMaintenanceMigrationRun = (
  runId: string,
  body: {
    expected_version: number;
    operation_key: string;
    reason: string;
    supplied_fingerprint: string;
  },
) => api.post<MigrationRunDetail>(
  `/maintenance/migration-runs/${runId}/approve`,
  body,
);
