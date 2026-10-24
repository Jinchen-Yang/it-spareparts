import { afterEach, describe, expect, it } from "vitest";
import {
  readMaintenancePermissionSnapshot, readMaintenanceWorkbookCapabilities,
  resolveMaintenanceWorkbookCapabilities,
} from "../maintenancePermissions";

afterEach(() => localStorage.clear());

describe("F2 independent permission counterexamples", () => {
  it("exhaustive standard-role matrix: global visibility needs page and both money keys", () => {
    for (const role of ["readonly", "sales", "purchaser", "maintenance_manager", "boss"]) {
      for (let mask = 0; mask < 16; mask++) {
        const permissions = {
          page_maintenance: !!(mask & 1), data_profit: !!(mask & 2),
          data_purchase_cost: !!(mask & 4), action_maintenance_expense_collection_upload: !!(mask & 8),
        };
        // Explicit rule allowlists, independently enumerated from D-33/D-03.
        const result = resolveMaintenanceWorkbookCapabilities({ role, permissions, project: {} });
        expect(result.global.canDownload, `${role}/${mask}`).toBe([7, 15].includes(mask));
        expect(result.global.canUpload, `${role}/${mask}`).toBe(mask === 15);
        expect(result.project).toEqual(result.global); // only legacy payload fallback
        const manager = resolveMaintenanceWorkbookCapabilities({ role, permissions, project: { detailCanEdit: true } });
        expect(manager.global).toEqual(result.global); // project exception never leaks globally
        expect(manager.project).toEqual({ canDownload: !!(mask & 1), canUpload: !!(mask & 1) });
      }
    }
  });

  it("all server source disagreements revoke upload including admin; money-only download remains", () => {
    for (const role of ["admin", "readonly", "sales"]) {
      for (const detailCanEdit of [undefined, false, true]) {
        for (const boardCanEdit of [undefined, false, true]) {
          const result = resolveMaintenanceWorkbookCapabilities({ role, permissions: {
            page_maintenance: true, data_profit: true, data_purchase_cost: true,
            action_maintenance_expense_collection_upload: true,
          }, project: { detailCanEdit, boardCanEdit } });
          expect(result.project.canDownload).toBe(true);
          expect(result.project.canUpload).toBe(![detailCanEdit, boardCanEdit].includes(false));
        }
      }
    }
  });

  it("empty admin graph bypasses ordinary gates but cannot create an unloaded project", () => {
    expect(resolveMaintenanceWorkbookCapabilities({ role: "admin", permissions: {} })).toEqual({
      global: { canDownload: true, canUpload: true }, project: { canDownload: false, canUpload: false },
    });
    expect(resolveMaintenanceWorkbookCapabilities({ role: "admin", permissions: {}, project: { detailCanEdit: false } }).project)
      .toEqual({ canDownload: true, canUpload: false });
  });

  it.each(["null", "false", "0", '"admin"', "{invalid"])("malformed stored graph %s fails closed", (stored) => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", stored);
    expect(readMaintenanceWorkbookCapabilities({}).global).toEqual({ canDownload: false, canUpload: false });
    expect(readMaintenanceWorkbookCapabilities({}).project).toEqual({ canDownload: false, canUpload: false });
  });

  it("scope-only and role-only changes invalidate ownership even with unchanged booleans", () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({ page_maintenance: true, data_profit: true,
      data_purchase_cost: true, own_maintenance_projects_only: false }));
    const first = readMaintenancePermissionSnapshot();
    const capability = readMaintenanceWorkbookCapabilities({});
    localStorage.setItem("permissions", JSON.stringify({ page_maintenance: true, data_profit: true,
      data_purchase_cost: true, own_maintenance_projects_only: true }));
    const second = readMaintenancePermissionSnapshot();
    expect(second).not.toBe(first);
    expect(readMaintenanceWorkbookCapabilities({})).toEqual(capability);
    localStorage.setItem("role", "readonly");
    expect(readMaintenancePermissionSnapshot()).not.toBe(second);
    expect(readMaintenanceWorkbookCapabilities({})).toEqual(capability);
  });
});
