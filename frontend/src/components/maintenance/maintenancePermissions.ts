interface LocalMaintenancePermissions {
  page_maintenance?: boolean;
  page_maintenance_beta?: boolean;
  data_customer?: boolean;
  data_purchase_cost?: boolean;
  data_profit?: boolean;
  own_customers_only?: boolean;
  action_maintenance_roundtrip_apply?: boolean;
  action_maintenance_manager_workbook_apply?: boolean;
  action_maintenance_project_manage?: boolean;
  action_maintenance_demand_delete?: boolean;
  action_maintenance_site_issue_manage?: boolean;
  action_maintenance_bad_return_manage?: boolean;
  action_maintenance_acceptance_submit?: boolean;
  action_maintenance_acceptance_review?: boolean;
  action_maintenance_warehouse_manage?: boolean;
  action_maintenance_migration_review?: boolean;
}

export interface MaintenanceCapabilities {
  canUseBeta: boolean;
  canViewCost: boolean;
  canViewContract: boolean;
  canViewExpense: boolean;
  canViewProfit: boolean;
  canViewFinancial: boolean;
  canDownloadRoundtrip: boolean;
  canApplyRoundtrip: boolean;
  canManageProject: boolean;
  canDeleteDemand: boolean;
  canManageSiteIssues: boolean;
  canManageBadReturns: boolean;
  canUseManagerWorkbook: boolean;
  canApplyManagerWorkbook: boolean;
  canSubmitAcceptance: boolean;
  canReviewAcceptance: boolean;
  canManageWarehouse: boolean;
  canReviewMigration: boolean;
}

function readLocalPermissions(): LocalMaintenancePermissions {
  try {
    const value = JSON.parse(localStorage.getItem("permissions") || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

type ReminderFilterRequirement =
  | "none"
  | "contract_amount"
  | "unit_cost"
  | "contract_and_expense"
  | "all_financial";

const contractCompletenessFilters = new Set([
  "completeness:no_effective_contracts",
  "completeness:duplicate_effective_contract",
  "completeness:unmapped_contract_status",
  "completeness:missing_contract_amount",
  "completeness:cross_project_contract_conflict",
]);

const costCompletenessFilters = new Set([
  "completeness:missing_consumption_cost",
  "completeness:unmapped_site_issue_status",
]);

const expenseCompletenessFilters = new Set([
  "completeness:unmapped_expense_status",
  "completeness:expense_data_not_ready",
  "completeness:expense_readiness_in_future",
]);

function reminderFilterRequirement(
  reminder: string | undefined,
): ReminderFilterRequirement {
  if (!reminder || reminder === "项目经理月度更新" || reminder.startsWith("manager_update:")) {
    return "none";
  }
  if (
    reminder === "cost"
    || reminder.startsWith("cost:")
    || costCompletenessFilters.has(reminder)
  ) return "unit_cost";
  if (
    reminder === "info"
    || reminder === "collection"
    || reminder.startsWith("collection:")
    || contractCompletenessFilters.has(reminder)
  ) return "contract_amount";
  if (expenseCompletenessFilters.has(reminder)) return "contract_and_expense";
  if (
    reminder === "all"
    || reminder === "warning"
    || reminder === "critical"
    || reminder === "completeness"
    || reminder === "cost_ratio"
    || reminder.startsWith("completeness:")
    || reminder.startsWith("cost_ratio:")
  ) return "all_financial";
  return "none";
}

export function readMaintenanceCapabilities(): MaintenanceCapabilities {
  const role = localStorage.getItem("role") || "";
  const isAdmin = role === "admin";
  const permissions = readLocalPermissions();
  const scopedSales = !isAdmin && (
    permissions.own_customers_only === true
    || (permissions.own_customers_only == null && role === "sales")
  );
  const canViewCost = isAdmin || permissions.data_purchase_cost === true;
  const canViewProfit = isAdmin || (
    canViewCost && permissions.data_profit === true
  );
  const canViewContract = canViewProfit;
  const canViewExpense = canViewProfit;
  const canViewFinancial = canViewCost && canViewContract && canViewExpense;
  const canUseBeta = isAdmin || (
    permissions.page_maintenance === true
    && permissions.page_maintenance_beta === true
  );
  const canDownloadRoundtrip = isAdmin || (
    !scopedSales
    && permissions.page_maintenance === true
    && permissions.data_customer === true
    && permissions.data_purchase_cost === true
    && permissions.data_profit === true
  );
  return {
    canUseBeta,
    canViewCost,
    canViewContract,
    canViewExpense,
    canViewProfit,
    canViewFinancial,
    canDownloadRoundtrip,
    canApplyRoundtrip: canDownloadRoundtrip && (
      isAdmin || permissions.action_maintenance_roundtrip_apply === true
    ),
    canUseManagerWorkbook: canUseBeta && canViewContract,
    canApplyManagerWorkbook: canUseBeta && canViewContract && (
      isAdmin || (
        permissions.action_maintenance_manager_workbook_apply === true
      )
    ),
    canSubmitAcceptance: canUseBeta && (
      isAdmin || permissions.action_maintenance_acceptance_submit === true
    ),
    canReviewAcceptance: canUseBeta && (
      isAdmin || permissions.action_maintenance_acceptance_review === true
    ),
    canManageProject: canUseBeta && (isAdmin || (
      permissions.data_purchase_cost === true
      && permissions.action_maintenance_project_manage === true
    )),
    // This high-risk action is real-account only.  Shared admin credentials
    // deliberately receive an explicit false from the backend, so unlike
    // ordinary admin capabilities the UI must not bypass the permission map.
    canDeleteDemand: canUseBeta
      && permissions.action_maintenance_demand_delete === true,
    canManageSiteIssues: canUseBeta && (isAdmin || (
      permissions.data_purchase_cost === true
      && permissions.action_maintenance_site_issue_manage === true
    )),
    canManageBadReturns: canUseBeta && (
      isAdmin || permissions.action_maintenance_bad_return_manage === true
    ),
    canManageWarehouse: canUseBeta
      && permissions.action_maintenance_warehouse_manage === true,
    canReviewMigration: canUseBeta && (
      permissions.data_purchase_cost === true
      && permissions.data_profit === true
      && permissions.action_maintenance_migration_review === true
    ),
  };
}

export function canUseMaintenanceReminderFilter(reminder: string | undefined): boolean {
  const requirement = reminderFilterRequirement(reminder);
  if (requirement === "none") return true;
  const {
    canViewContract,
    canViewCost,
    canViewExpense,
    canViewFinancial,
  } = readMaintenanceCapabilities();
  if (requirement === "unit_cost") return canViewCost;
  if (requirement === "contract_amount") return canViewContract;
  if (requirement === "contract_and_expense") return canViewContract && canViewExpense;
  return canViewFinancial;
}
