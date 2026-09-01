import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Modal, message } from "antd";

const assignMaintenanceProjectManager = vi.fn();
const archiveMaintenanceProjectManager = vi.fn();
const searchMaintenanceManagerAccounts = vi.fn();

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceOperations",
  );
  return {
    ...actual,
    assignMaintenanceProjectManager: (...args: unknown[]) =>
      assignMaintenanceProjectManager(...args),
    archiveMaintenanceProjectManager: (...args: unknown[]) =>
      archiveMaintenanceProjectManager(...args),
    searchMaintenanceManagerAccounts: (...args: unknown[]) =>
      searchMaintenanceManagerAccounts(...args),
  };
});

import ProjectManagerAssignmentControl from "../ProjectManagerAssignmentControl";

const project = {
  project_id: "project-salesperson-sync",
  project_manager_id: "来源负责人原文",
  manager_assignment: {
    assignment_id: "assignment-current",
    project_id: "project-salesperson-sync",
    responsibility_type: "primary_manager" as const,
    user_id: 7,
    username: "manager-current",
    display_name: "当前负责人",
    account_status: "active" as const,
    source_manager_text: "来源负责人原文",
    version: 3,
    assigned_at: "2026-08-30T00:00:00Z",
    archived_at: null,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  searchMaintenanceManagerAccounts.mockResolvedValue({
    data: {
      rows: [{
        user_id: 9,
        username: "manager-target",
        display_name: "目标负责人",
        is_active: true,
      }],
      total: 1,
      page: 1,
      page_size: 30,
    },
  });
  assignMaintenanceProjectManager.mockResolvedValue({ data: {} });
});

afterEach(() => {
  cleanup();
  Modal.destroyAll();
  message.destroy();
});

async function openAndFillAssignment() {
  const onChanged = vi.fn().mockResolvedValue(true);
  render(
    <ProjectManagerAssignmentControl
      project={project}
      canManage
      onChanged={onChanged}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "管理负责人" }));
  const dialog = await screen.findByRole("dialog", { name: "项目负责人账号映射" });
  await waitFor(() => expect(searchMaintenanceManagerAccounts).toHaveBeenCalled());
  fireEvent.mouseDown(within(dialog).getByRole("combobox", { name: "选择负责人账号" }));
  fireEvent.click(await screen.findByText(/目标负责人.*manager-target/));
  fireEvent.change(within(dialog).getByLabelText("负责人映射或改派原因"), {
    target: { value: "负责人交接" },
  });
  return { dialog, onChanged };
}

describe("project manager assignment salesperson sync", () => {
  it("defaults salesperson sync on and sends true in the reassignment request", async () => {
    const { dialog } = await openAndFillAssignment();

    expect(within(dialog).getByRole("checkbox", { name: "同时同步销售人员" }))
      .toBeChecked();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认改派" }));

    await waitFor(() => expect(assignMaintenanceProjectManager).toHaveBeenCalledWith(
      "project-salesperson-sync",
      {
        user_id: 9,
        expected_assignment_id: "assignment-current",
        expected_assignment_version: 3,
        sync_salesperson: true,
        reason: "负责人交接",
      },
    ));
  });

  it("allows salesperson sync to be disabled and sends false", async () => {
    const { dialog } = await openAndFillAssignment();
    const checkbox = within(dialog).getByRole("checkbox", { name: "同时同步销售人员" });

    fireEvent.click(checkbox);

    expect(checkbox).not.toBeChecked();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认改派" }));
    await waitFor(() => expect(assignMaintenanceProjectManager).toHaveBeenCalledWith(
      "project-salesperson-sync",
      expect.objectContaining({ sync_salesperson: false }),
    ));
  });
});
