import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const { getMaintenanceProjectWorkspace } = vi.hoisted(() => ({
  getMaintenanceProjectWorkspace: vi.fn(),
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return { ...actual, getMaintenanceProjectWorkspace };
});

import MaintenanceProjectWorkspacePage from "../MaintenanceProjectWorkspacePage";

const workspace = {
  project: {
    project_id: "project-1",
    project_code: "XM-001",
    display_name: "移动维保项目",
    project_manager_id: "manager-1",
    lifecycle_status: "ongoing",
    is_active: true,
    version: 3,
    contracts: [{
      project_contract_id: "pc-1",
      contract_id: "c-1",
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
      site_requisition_known_cost: 450,
      approved_expense: 350,
      actual_project_cost_known: 800,
      cost_complete: false,
      missing_cost_lines: 1,
    },
    reminder_count: 1,
    as_of: "2026-08-08",
  },
  requisitions: {
    rows: [{
      line_id: "line-1",
      order_no: "WBDD-001",
      order_date: "2026-08-01",
      contract_no: "XSDD-001",
      pn: "PN-MISSING",
      description: "待补价格的现场领用件",
      quantity: 2,
      unit_cost: null,
      cost_amount: null,
      cost_source: null,
      cost_status: "missing",
    }],
    total: 1,
  },
  approved_expenses: {
    rows: [{
      expense_id: "expense-1",
      expense_date: "2026-08-02",
      contract_no: "XSDD-001",
      category: "差旅",
      reason: "现场支持",
      amount: 350,
      approval_status: "approved",
    }],
    total: 1,
  },
  reminders: [{
    reminder_id: "reminder-1",
    type: "cost_gap",
    severity: "warning",
    title: "存在待补成本",
    detail: "1 行现场领用缺少价格",
    due_date: null,
  }],
  workbook_preview: {
    protocol_version: "2.0",
    sheets: [
      { code: "overview", name: "01_总览", row_count: 1, ownership: "append_only" },
      { code: "site_requisitions", name: "02_备件消耗", row_count: 1, ownership: "system" },
      { code: "approved_expenses", name: "03_报销单", row_count: 1, ownership: "system" },
      { code: "manager_tracking", name: "04_项目经理追踪", row_count: 2, ownership: "system" },
    ],
    latest_tracking_month: "2026-08",
    last_exported_at: null,
    data_version: "workspace-v1",
  },
  as_of: "2026-08-08",
  data_version: "workspace-v1",
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  getMaintenanceProjectWorkspace.mockResolvedValue({ data: workspace });
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceProjectWorkspacePage", () => {
  it("完整展示合同、双进度、缺价领用、审批通过报销、提醒和四表预览", async () => {
    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "移动维保项目" })).toBeInTheDocument();
    expect(screen.getAllByText("XSDD-001").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("回款 / 全部合同额")).toBeInTheDocument();
    expect(screen.getByText("项目实际成本 / 全部合同额")).toBeInTheDocument();

    const requisitions = screen.getByTestId("site-requisition-table");
    expect(within(requisitions).getByText("PN-MISSING")).toBeInTheDocument();
    expect(within(requisitions).getByText("待回填成本")).toBeInTheDocument();
    expect(within(requisitions).getByText("待补价格的现场领用件")).toBeInTheDocument();

    const expenses = screen.getByTestId("approved-expense-table");
    expect(within(expenses).getByText("审批通过")).toBeInTheDocument();
    expect(within(expenses).getByText("现场支持")).toBeInTheDocument();
    expect(screen.getByText("存在待补成本")).toBeInTheDocument();

    const preview = screen.getByTestId("workbook-four-sheet-preview");
    for (const sheet of ["01_总览", "02_备件消耗", "03_报销单", "04_项目经理追踪"]) {
      expect(within(preview).getByText(sheet)).toBeInTheDocument();
    }
    expect(within(preview).getByText("仅回款表尾可追加")).toBeInTheDocument();
    expect(within(preview).getAllByText("系统生成")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传月度更新" })).toBeInTheDocument();
    expect(screen.queryByText(/扇形图|饼图/)).toBeNull();
  });

  it("无项目管理权限时隐藏人工成本回填入口", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
      own_customers_only: false,
      action_maintenance_project_manage: false,
      action_maintenance_roundtrip_apply: false,
    }));

    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "移动维保项目" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "去人工回填成本" })).toBeNull();
    expect(screen.queryByRole("button", { name: "上传月度更新" })).toBeNull();
    expect(screen.getByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
  });
});
