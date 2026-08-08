import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

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

function LocationProbe() {
  const location = useLocation();
  return <div>{`${location.pathname}${location.search}${location.hash}`}</div>;
}

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
      contract_amount_basis: "inc_tax",
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
      contract_amount_basis: "inc_tax",
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
    contract_amount_basis: "inc_tax",
    contract_amount_complete: false,
    received_amount: 300,
    site_requisition_known_cost: 450,
    site_requisition_known_cost_ex_tax: 398.23,
    site_requisition_known_cost_inc_tax: 450,
    approved_expense: 50,
    approved_expense_ex_tax: 44.25,
    approved_expense_inc_tax: 50,
    actual_project_cost_known: 500,
    actual_project_cost_known_ex_tax: 442.48,
    actual_project_cost_known_inc_tax: 500,
    cost_progress_basis: "inc_tax",
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
    expect(within(card).getByText("原始状态：已生效")).toBeInTheDocument();
    expect(within(card).getByText("原始状态：未提供")).toBeInTheDocument();
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

  it("成本汇总被脱敏时卡片不误标为成本待补", async () => {
    listMaintenanceProjectOperations.mockResolvedValueOnce({
      data: {
        rows: [{
          ...summary,
          metrics: {
            ...summary.metrics,
            site_requisition_known_cost: null,
            site_requisition_known_cost_ex_tax: null,
            site_requisition_known_cost_inc_tax: null,
            approved_expense: null,
            approved_expense_ex_tax: null,
            approved_expense_inc_tax: null,
            actual_project_cost_known: null,
            actual_project_cost_known_ex_tax: null,
            actual_project_cost_known_inc_tax: null,
            cost_complete: null,
            missing_cost_lines: null,
          },
        }],
        total: 1,
        page: 1,
        page_size: 24,
        as_of: "2026-08-08",
        data_version: "v1",
      },
    });

    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);

    const card = await screen.findByTestId("maintenance-project-card-project-1");
    expect(within(card).getByText("成本不可见/无权限")).toBeInTheDocument();
    expect(within(card).queryByText("成本待补")).toBeNull();
    expect(card).not.toHaveTextContent("缺 null 行成本");
  });

  it("带 project_id 的旧提醒深链直接进入稳定项目详情", async () => {
    render(
      <MemoryRouter initialEntries={[
        "/maintenance/projects?project_id=project%2F1&reminder=all#urgent",
      ]}>
        <Routes>
          <Route path="/maintenance/projects" element={<MaintenanceProjectsPage />} />
          <Route path="/maintenance/projects/:projectId" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText(
      "/maintenance/projects/project%2F1?reminder=all#urgent",
    )).toBeInTheDocument();
    expect(listMaintenanceProjectOperations).not.toHaveBeenCalled();
  });
});
