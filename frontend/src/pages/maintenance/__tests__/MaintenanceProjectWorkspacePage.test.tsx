import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  collection_snapshots: {
    rows: [{
      collection_id: "collection-1",
      project_contract_id: "pc-1",
      contract_no: "XSDD-001",
      report_month: "2026-06-01",
      cumulative_amount: 500,
      receipt_reference: "RECEIPT-202606",
      status: "confirmed",
      remark: "六月已确认",
      version: 2,
    }, {
      collection_id: "collection-2",
      project_contract_id: "pc-1",
      contract_no: "XSDD-001",
      report_month: "2026-07-01",
      cumulative_amount: 600,
      receipt_reference: null,
      status: "unconfirmed",
      remark: "七月待财务确认",
      version: 1,
    }, {
      collection_id: "collection-3",
      project_contract_id: "pc-1",
      contract_no: "XSDD-001",
      report_month: "2026-08-01",
      cumulative_amount: 650,
      receipt_reference: "RECEIPT-VOID",
      status: "void",
      remark: "凭证重复，已作废",
      version: 3,
    }],
    total: 3,
    page: 1,
    page_size: 20,
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
    }, {
      line_id: "line-2",
      order_no: "WBDD-002",
      order_date: "2026-08-02",
      contract_no: "XSDD-001",
      pn: "PN-NOT-COUNTED",
      description: "未确认现场领用",
      quantity: 1,
      unit_cost: null,
      cost_amount: null,
      cost_source: null,
      cost_status: "not_counted",
    }],
    total: 2,
    page: 1,
    page_size: 20,
  },
  approved_expenses: {
    rows: [{
      expense_id: "expense-1",
      expense_ref: "BXD-202608-001",
      expense_date: "2026-08-02",
      contract_no: "XSDD-001",
      applicant: "张三",
      category: "差旅",
      expense_reason: "现场支持",
      amount: 350,
      approval_status: "approved",
    }],
    total: 1,
    page: 1,
    page_size: 20,
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

    const collections = screen.getByTestId("collection-snapshot-table");
    expect(within(collections).getByText("2026-06")).toBeInTheDocument();
    expect(within(collections).getByText("2026-07")).toBeInTheDocument();
    expect(within(collections).getByText("2026-08")).toBeInTheDocument();
    expect(within(collections).getByText("已确认")).toBeInTheDocument();
    expect(within(collections).getByText("待确认")).toBeInTheDocument();
    expect(within(collections).getByText("已作废")).toBeInTheDocument();
    expect(within(collections).getByText("RECEIPT-202606")).toBeInTheDocument();
    expect(within(collections).getByText("凭证重复，已作废")).toBeInTheDocument();

    const requisitions = screen.getByTestId("site-requisition-table");
    expect(within(requisitions).getByText("PN-MISSING")).toBeInTheDocument();
    expect(within(requisitions).getByText("待回填成本")).toBeInTheDocument();
    expect(within(requisitions).getByText("未计入成本")).toBeInTheDocument();
    expect(within(requisitions).getByText("待补价格的现场领用件")).toBeInTheDocument();

    const expenses = screen.getByTestId("approved-expense-table");
    expect(within(expenses).getByText("审批通过")).toBeInTheDocument();
    expect(within(expenses).getAllByText("报销单号").length).toBeGreaterThanOrEqual(1);
    expect(within(expenses).getByText("BXD-202608-001")).toBeInTheDocument();
    expect(within(expenses).getByText("张三")).toBeInTheDocument();
    expect(within(expenses).getByText("差旅")).toBeInTheDocument();
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

  it("切换回款页时带齐三类独立页码并采用服务端返回的第二页", async () => {
    const firstPage = {
      ...workspace,
      collection_snapshots: {
        ...workspace.collection_snapshots,
        total: 21,
      },
    };
    const secondPage = {
      ...firstPage,
      collection_snapshots: {
        ...firstPage.collection_snapshots,
        page: 2,
        rows: [{
          ...workspace.collection_snapshots.rows[0],
          collection_id: "collection-21",
          report_month: "2026-09-01",
          receipt_reference: "RECEIPT-PAGE-2",
        }],
      },
    };
    getMaintenanceProjectWorkspace
      .mockResolvedValueOnce({ data: firstPage })
      .mockResolvedValueOnce({ data: secondPage });

    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    const collections = await screen.findByTestId("collection-snapshot-table");
    fireEvent.click(within(collections).getByTitle("2"));

    await waitFor(() => expect(getMaintenanceProjectWorkspace).toHaveBeenLastCalledWith(
      "project-1",
      {
        collection_page: 2,
        collection_page_size: 20,
        requisition_page: 1,
        requisition_page_size: 20,
        expense_page: 1,
        expense_page_size: 20,
      },
    ));
    expect(await within(collections).findByText("RECEIPT-PAGE-2")).toBeInTheDocument();
  });

  it("成本汇总被脱敏时不把未知状态误标为缺价", async () => {
    getMaintenanceProjectWorkspace.mockResolvedValueOnce({
      data: {
        ...workspace,
        project: {
          ...workspace.project,
          metrics: {
            ...workspace.project.metrics,
            site_requisition_known_cost: null,
            approved_expense: null,
            actual_project_cost_known: null,
            cost_complete: null,
            missing_cost_lines: null,
          },
        },
      },
    });

    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByText("成本不可见/无权限")).toBeInTheDocument();
    expect(screen.queryByText(/缺 null 行成本/)).toBeNull();
    expect(screen.getByText("成本明细不可见")).toBeInTheDocument();
  });
});
