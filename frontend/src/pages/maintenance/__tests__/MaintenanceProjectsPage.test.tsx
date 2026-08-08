import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const { listMaintenanceProjectOperations } = vi.hoisted(() => ({
  listMaintenanceProjectOperations: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return { ...actual, listMaintenanceProjectOperations };
});

import MaintenanceProjectsPage from "../MaintenanceProjectsPage";

const summary = {
  project_id: "project-1",
  project_code: "XM-001",
  display_name: "移动维保项目",
  project_manager_id: "manager-1",
  lifecycle_status: "ongoing",
  is_active: true,
  version: 3,
  contracts: [
    {
      project_contract_id: "pc-1",
      contract_id: "c-1",
      contract_no: "XSDD-001",
      contract_amount: 600,
      contract_status: "已生效",
      status_mapping_state: "mapped",
      included_in_total: true,
      is_effective: true,
      amount_status: "available",
      received_amount: 300,
    },
    {
      project_contract_id: "pc-2",
      contract_id: "c-2",
      contract_no: "XSDD-002",
      contract_amount: null,
      contract_status: null,
      status_mapping_state: "unmapped",
      included_in_total: false,
      is_effective: true,
      amount_status: "missing",
      received_amount: null,
    },
  ],
  metrics: {
    total_contract_amount: 1000,
    known_contract_amount: 1000,
    contract_amount_complete: false,
    received_amount: 300,
    site_requisition_known_cost: 450,
    approved_expense: 50,
    actual_project_cost_known: 500,
    cost_complete: false,
    missing_cost_lines: 2,
  },
  reminder_count: 4,
  as_of: "2026-08-08",
};

beforeEach(() => {
  vi.clearAllMocks();
  listMaintenanceProjectOperations.mockResolvedValue({
    data: {
      rows: [summary],
      total: 1,
      page: 1,
      page_size: 24,
      as_of: "2026-08-08",
      data_version: "v1",
    },
  });
});
afterEach(cleanup);

describe("MaintenanceProjectsPage", () => {
  it("一次批量加载方块卡片，并逐份展示合同与缺成本下限", async () => {
    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);

    const card = await screen.findByTestId("maintenance-project-card-project-1");
    expect(listMaintenanceProjectOperations).toHaveBeenCalledOnce();
    expect(within(card).getByText("XM-001")).toBeInTheDocument();
    expect(within(card).getByText("XSDD-001")).toBeInTheDocument();
    expect(within(card).getByText("XSDD-002")).toBeInTheDocument();
    expect(within(card).getByText("金额缺失")).toBeInTheDocument();
    expect(within(card).getByText("状态未映射")).toBeInTheDocument();
    expect(within(card).getByText(/缺 2 行成本/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "查看项目" })).toHaveAttribute(
      "href",
      "/maintenance/projects/project-1",
    );
    expect(screen.getByTestId("maintenance-project-grid")).toHaveClass(
      "maintenance-project-grid",
    );
  });

  it("搜索一次只产生一个新的批量摘要请求", async () => {
    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);
    await screen.findByTestId("maintenance-project-card-project-1");

    const search = screen.getByRole("searchbox", { name: "搜索维保项目" });
    fireEvent.change(search, { target: { value: "联通" } });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(listMaintenanceProjectOperations).toHaveBeenCalledTimes(2));
    expect(listMaintenanceProjectOperations).toHaveBeenLastCalledWith(expect.objectContaining({
      q: "联通",
      page: 1,
    }));
  });
});
