import { beforeEach, describe, expect, it } from "vitest";

import { readMaintenanceCapabilities } from "../maintenancePermissions";

describe("maintenance permissions", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("keeps the legacy stable maintenance admin bypass without a Beta allowlist bit", () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      action_maintenance_warehouse_manage: true,
    }));

    const capabilities = readMaintenanceCapabilities();

    expect(capabilities.canUseBeta).toBe(true);
    expect(capabilities.canManageProject).toBe(true);
    expect(capabilities.canUseManagerWorkbook).toBe(true);
    expect(capabilities.canManageWarehouse).toBe(true);
  });

  it("keeps collection reminder access behind its explicit Beta allowlist and actions", () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      action_maintenance_collection_follow_up: true,
      action_maintenance_collection_plan_import: true,
      data_purchase_cost: true,
      data_profit: true,
    }));

    let capabilities = readMaintenanceCapabilities();
    expect(capabilities.canViewCollectionReminders).toBe(false);
    expect(capabilities.canFollowUpCollection).toBe(false);
    expect(capabilities.canImportCollectionPlan).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance_beta: true,
      action_maintenance_collection_follow_up: true,
      action_maintenance_collection_plan_import: true,
      data_purchase_cost: true,
      data_profit: true,
    }));

    capabilities = readMaintenanceCapabilities();
    expect(capabilities.canViewCollectionReminders).toBe(true);
    expect(capabilities.canFollowUpCollection).toBe(true);
    expect(capabilities.canImportCollectionPlan).toBe(true);
  });
});
