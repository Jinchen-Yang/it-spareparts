interface LocalMaintenancePermissions {
  action_maintenance_roundtrip_apply?: boolean;
  action_maintenance_project_manage?: boolean;
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
  const isAdmin = localStorage.getItem("role") === "admin";
  const permissions = readLocalPermissions();
  return {
    canApplyRoundtrip: isAdmin
      || permissions.action_maintenance_roundtrip_apply === true,
    canManageProject: isAdmin
      || permissions.action_maintenance_project_manage === true,
  };
}
