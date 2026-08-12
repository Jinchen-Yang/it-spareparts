export interface MaintenanceCapabilities {
  canDownloadRoundtrip: boolean;
  canApplyRoundtrip: boolean;
  canManageProject: boolean;
  canReviewMigration: boolean;
  canDeleteDemand: boolean;
  canViewCost: boolean;
  canViewContract: boolean;
  canViewExpense: boolean;
  canViewFinancial: boolean;
}

interface LocalMaintenancePermissions {
  page_maintenance?: boolean;
  data_customer?: boolean;
  data_purchase_cost?: boolean;
  data_profit?: boolean;
  own_customers_only?: boolean;
  action_maintenance_roundtrip_apply?: boolean;
  action_maintenance_project_manage?: boolean;
  action_maintenance_migration_review?: boolean;
  action_maintenance_demand_delete?: boolean;
}

function readLocalPermissions(): LocalMaintenancePermissions {
  try {
    const value = JSON.parse(localStorage.getItem("permissions") || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

export function readMaintenanceCapabilities() {
  const role = localStorage.getItem("role") || "";
  const isAdmin = role === "admin";
  const permissions = readLocalPermissions();
  const scopedSales = !isAdmin && (
    permissions.own_customers_only === true
    || (permissions.own_customers_only == null && role === "sales")
  );
  const canDownloadRoundtrip = isAdmin || (
    !scopedSales
    && permissions.page_maintenance === true
    && permissions.data_customer === true
    && permissions.data_purchase_cost === true
    && permissions.data_profit === true
  );
  return {
    canDownloadRoundtrip,
    canApplyRoundtrip: canDownloadRoundtrip && (
      isAdmin || permissions.action_maintenance_roundtrip_apply === true
    ),
    canManageProject: isAdmin || (
      permissions.page_maintenance === true
      && permissions.data_purchase_cost === true
      && permissions.action_maintenance_project_manage === true
    ),
    canReviewMigration: isAdmin || (
      permissions.page_maintenance === true
      && permissions.action_maintenance_migration_review === true
    ),
    canDeleteDemand: isAdmin || (
      permissions.page_maintenance === true
      && permissions.action_maintenance_demand_delete === true
    ),
    canViewCost: isAdmin || permissions.data_purchase_cost === true,
    canViewContract: isAdmin || permissions.page_maintenance === true,
    canViewExpense: isAdmin || permissions.data_profit === true,
    canViewFinancial: isAdmin || permissions.data_profit === true,
  };
}
