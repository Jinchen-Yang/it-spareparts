import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const { getMaintenanceProjectWorkspace, listMaintenanceProjectOperations } = vi.hoisted(() => ({
  getMaintenanceProjectWorkspace: vi.fn(),
  listMaintenanceProjectOperations: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return { ...actual, getMaintenanceProjectWorkspace, listMaintenanceProjectOperations };
});

import MaintenanceProjectUpdatesPage from "../MaintenanceProjectUpdatesPage";

const project = {
  project_id: "project-1",
  project_code: "XM-001",
  display_name: "移动维保项目",
  project_manager_id: "manager-1",
  lifecycle_status: "ongoing",
  is_active: true,
  version: 3,
  contracts: [{
    project_contract_id: "pc-1",
    contract_id: "contract-1",
    contract_no: "XSDD-001",
    contract_amount: 1000,
    contract_status: "已生效",
    status_mapping_state: "mapped",
    included_in_total: true,
    is_effective: true,
    amount_status: "available",
    received_amount: 600,
  }],
  metrics: {
    total_contract_amount: 1000,
    known_contract_amount: 1000,
    contract_amount_complete: true,
    received_amount: 600,
    site_requisition_known_cost: 300,
    approved_expense: 100,
    actual_project_cost_known: 400,
    cost_complete: true,
    missing_cost_lines: 0,
  },
  reminder_count: 0,
  as_of: "2026-08-08",
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  listMaintenanceProjectOperations.mockResolvedValue({
    data: { rows: [project], total: 1, page: 1, page_size: 200, as_of: "2026-08-08", data_version: "v1" },
  });
  getMaintenanceProjectWorkspace.mockResolvedValue({
    data: {
      project,
      requisitions: { rows: [], total: 0 },
      approved_expenses: { rows: [], total: 0 },
      reminders: [],
      workbook_preview: {
        protocol_version: "2.0",
        sheets: [
          { code: "overview", name: "01_总览", row_count: 1, ownership: "append_only" },
          { code: "site_requisitions", name: "02_备件消耗", row_count: 0, ownership: "system" },
          { code: "approved_expenses", name: "03_报销单", row_count: 0, ownership: "system" },
          { code: "manager_tracking", name: "04_项目经理追踪与提醒", row_count: 0, ownership: "system" },
        ],
        latest_tracking_month: "2026-08",
        last_exported_at: null,
        data_version: "v1",
      },
      as_of: "2026-08-08",
      data_version: "v1",
    },
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceProjectUpdatesPage", () => {
  it("选择项目后展示四表内容和同页下载上传操作", async () => {
    render(
      <MemoryRouter initialEntries={["/maintenance/updates?project_id=project-1"]}>
        <MaintenanceProjectUpdatesPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("移动维保项目")).toBeInTheDocument();
    const preview = screen.getByTestId("workbook-four-sheet-preview");
    expect(within(preview).getByText("01_总览")).toBeInTheDocument();
    expect(within(preview).getByText("仅回款表尾可追加")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传月度更新" })).toBeInTheDocument();
    await waitFor(() => expect(getMaintenanceProjectWorkspace).toHaveBeenCalledWith("project-1"));
  });

  it("旧下载链接只有合同时自动定位唯一关联项目", async () => {
    render(
      <MemoryRouter initialEntries={[
        "/maintenance/updates?from=reminders&contract=XSDD-001",
      ]}>
        <MaintenanceProjectUpdatesPage />
      </MemoryRouter>,
    );

    await waitFor(() => expect(listMaintenanceProjectOperations).toHaveBeenCalledWith(
      expect.objectContaining({ q: "XSDD-001" }),
    ));
    await waitFor(() => expect(getMaintenanceProjectWorkspace).toHaveBeenCalledWith("project-1"));
    expect(await screen.findByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
  });

  it("切换项目后立即移除旧项目的工作簿操作", async () => {
    const secondProject = {
      ...project,
      project_id: "project-2",
      project_code: "XM-002",
      display_name: "第二维保项目",
      contracts: [],
    };
    listMaintenanceProjectOperations.mockResolvedValue({
      data: {
        rows: [project, secondProject],
        total: 2,
        page: 1,
        page_size: 200,
        as_of: "2026-08-08",
        data_version: "v1",
      },
    });

    render(
      <MemoryRouter initialEntries={["/maintenance/updates?project_id=project-1"]}>
        <MaintenanceProjectUpdatesPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("button", { name: "下载完整四表" })).toBeInTheDocument();

    getMaintenanceProjectWorkspace.mockReturnValueOnce(new Promise(() => undefined));
    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(await screen.findByText("XM-002 · 第二维保项目"));

    await waitFor(() => expect(getMaintenanceProjectWorkspace).toHaveBeenCalledWith("project-2"));
    expect(screen.queryByRole("button", { name: "下载完整四表" })).toBeNull();
    expect(screen.queryByText("完整四表内容")).toBeNull();
  });
});
