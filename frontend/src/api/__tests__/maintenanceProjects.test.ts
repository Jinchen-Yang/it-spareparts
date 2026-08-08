import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();

vi.mock("../../api", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    patch: (...args: unknown[]) => patch(...args),
  },
}));

import {
  archiveMaintenanceProject,
  createMaintenanceProject,
  getMaintenanceProject,
  listMaintenanceProjects,
  restoreMaintenanceProject,
  updateMaintenanceProject,
} from "../maintenanceProjects";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("maintenance project master API", () => {
  it("loads active and archived projects from the stable directory", () => {
    listMaintenanceProjects();
    expect(get).toHaveBeenCalledWith("/maintenance/projects/stable", {
      params: { include_inactive: true },
    });
  });

  it("forwards server pagination without dropping the inactive-project flag", () => {
    listMaintenanceProjects({ page: 2, page_size: 50 });
    expect(get).toHaveBeenCalledWith("/maintenance/projects/stable", {
      params: { include_inactive: true, page: 2, page_size: 50 },
    });
  });

  it("loads one project exactly when recovering from a version conflict", () => {
    getMaintenanceProject("project-1");
    expect(get).toHaveBeenCalledWith("/maintenance/projects/stable/project-1");
  });

  it("creates a project without writing an unsettled lifecycle status", () => {
    createMaintenanceProject({
      project_code: "XM-001",
      display_name: "一号项目",
      project_manager_id: "manager-1",
      reason: "新合同立项",
    });
    expect(post).toHaveBeenCalledWith("/maintenance/projects/stable", {
      project_code: "XM-001",
      display_name: "一号项目",
      project_manager_id: "manager-1",
      reason: "新合同立项",
    });
  });

  it("updates by version and forwards explicit null only when clearing the manager", () => {
    updateMaintenanceProject("project-1", {
      version: 3,
      display_name: "更正后的项目名",
      project_manager_id: null,
      reason: "负责人离任",
    });
    expect(patch).toHaveBeenCalledWith("/maintenance/projects/stable/project-1", {
      version: 3,
      display_name: "更正后的项目名",
      project_manager_id: null,
      reason: "负责人离任",
    });
  });

  it("archives and restores by version with an auditable reason", () => {
    archiveMaintenanceProject("project-1", { version: 3, reason: "项目已结束" });
    restoreMaintenanceProject("project-1", { version: 4, reason: "项目重新启动" });

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/maintenance/projects/stable/project-1/archive",
      { version: 3, reason: "项目已结束" },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/projects/stable/project-1/restore",
      { version: 4, reason: "项目重新启动" },
    );
  });
});
