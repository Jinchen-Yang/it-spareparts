import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const {
  getMaintenanceProjectWorkspace,
  getMaintenanceAcceptance,
  searchMaintenanceBadReturns,
  searchMaintenanceReturnObligations,
  searchSiteIssueCandidates,
  searchSiteIssues,
  taxBasisState,
} = vi.hoisted(() => ({
  getMaintenanceProjectWorkspace: vi.fn(),
  getMaintenanceAcceptance: vi.fn(),
  searchMaintenanceBadReturns: vi.fn(),
  searchMaintenanceReturnObligations: vi.fn(),
  searchSiteIssueCandidates: vi.fn(),
  searchSiteIssues: vi.fn(),
  taxBasisState: { value: "both" as "inc" | "ex" | "both" },
}));

vi.mock("../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceOperations")>(
    "../../../api/maintenanceOperations",
  );
  return {
    ...actual,
    getMaintenanceProjectWorkspace,
    getMaintenanceAcceptance,
    searchMaintenanceBadReturns,
    searchMaintenanceReturnObligations,
    searchSiteIssueCandidates,
    searchSiteIssues,
  };
});

vi.mock("../../../context/TaxBasis", async () => {
  const actual = await vi.importActual<typeof import("../../../context/TaxBasis")>(
    "../../../context/TaxBasis",
  );
  return { ...actual, useTaxBasis: () => taxBasisState.value };
});

import MaintenanceProjectWorkspacePage from "../MaintenanceProjectWorkspacePage";

const workspaceReturnRate = {
  project_id: "project-1",
  status: "available" as const,
  official_basis: "warehouse_confirmed_v1" as const,
  official_rate_pct: "20.00",
  registered_rate_pct: "40.00",
  warehouse_confirmed_rate_pct: "20.00",
  required_quantity: "5.000",
  registered_quantity: "2.000",
  warehouse_confirmed_quantity: "1.000",
  outstanding_quantity: "4.000",
  exempt_quantity: "1.000",
  pending_quantity: "0.000",
  required_count: 1,
  exempt_count: 1,
  pending_count: 0,
  business_assumption: "官方返还率以仓库确认数量为准",
};

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
      contract_amount_basis: "inc_tax",
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
      contract_amount_basis: "inc_tax",
      contract_amount_complete: true,
      received_amount: 600,
      site_requisition_known_cost: 450,
      site_requisition_known_cost_ex_tax: 398.23,
      site_requisition_known_cost_inc_tax: 450,
      approved_expense: 350,
      approved_expense_ex_tax: 309.73,
      approved_expense_inc_tax: 350,
      actual_project_cost_known: 800,
      actual_project_cost_known_ex_tax: 707.96,
      actual_project_cost_known_inc_tax: 800,
      cost_progress_basis: "inc_tax",
      cost_complete: false,
      missing_cost_lines: 1,
    },
    return_rate: workspaceReturnRate,
    reminder_count: 1,
    manager_tracking: {
      service_period: {
        service_start: "2026-01-01",
        service_end: "2026-12-31",
        completeness_state: "complete",
      },
      next_collection_milestone: {
        project_contract_id: "pc-1",
        contract_no: "XSDD-001",
        sequence: 2,
        planned_date: "2026-08-01",
        planned_amount: 200,
        overdue_days: 7,
        is_overdue: true,
      },
      acceptance: {
        deliverable_id: "deliverable-1",
        due_date: "2026-08-31",
        submission_status: "not_submitted",
        approval_status: "not_reviewed",
        configuration_state: "configured",
        rejection_reason: null,
        attachment_count: 0,
        overdue_days: 0,
        is_overdue: false,
        version: 1,
      },
    },
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
      unit_cost_ex_tax: null,
      unit_cost_inc_tax: null,
      cost_amount_ex_tax: null,
      cost_amount_inc_tax: null,
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
      unit_cost_ex_tax: null,
      unit_cost_inc_tax: null,
      cost_amount_ex_tax: null,
      cost_amount_inc_tax: null,
      cost_source: null,
      cost_status: "not_counted",
    }, {
      line_id: "line-3",
      order_no: "WBDD-003",
      order_date: "2026-08-03",
      contract_no: "XSDD-001",
      pn: "PN-COSTED",
      description: "已有双税成本的现场领用件",
      quantity: 2,
      unit_cost: 113,
      cost_amount: 226,
      unit_cost_ex_tax: 100,
      unit_cost_inc_tax: 113,
      cost_amount_ex_tax: 200,
      cost_amount_inc_tax: 226,
      cost_source: "sales_window",
      cost_evidence_kind: "sales_estimate",
      cost_is_estimate: true,
      cost_source_label: "估算（销售前后 7 天数量加权）",
      cost_status: "available",
    }],
    total: 3,
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
      amount_ex_tax: 309.73,
      amount_inc_tax: 350,
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
  return_rate: workspaceReturnRate,
  as_of: "2026-08-08",
  data_version: "workspace-v1",
};

beforeEach(() => {
  vi.clearAllMocks();
  taxBasisState.value = "both";
  localStorage.clear();
  localStorage.setItem("role", "admin");
  getMaintenanceProjectWorkspace.mockResolvedValue({ data: workspace });
  const adapter = {
    key: "synthetic_delivery_v1",
    state: "synthetic_ready",
    production_ready: false,
    detail: "真实发货适配器接入前不得用于生产确认",
  };
  searchSiteIssueCandidates.mockResolvedValue({
    data: { adapter, rows: [], total: 0, page: 1, page_size: 50 },
  });
  searchSiteIssues.mockResolvedValue({
    data: { project_id: "project-1", adapter, rows: [], total: 0, page: 1, page_size: 20 },
  });
  searchMaintenanceReturnObligations.mockResolvedValue({
    data: {
      rows: [],
      total: 0,
      page: 1,
      page_size: 50,
      return_rate: workspaceReturnRate,
    },
  });
  searchMaintenanceBadReturns.mockResolvedValue({
    data: { project_id: "project-1", rows: [], total: 0, page: 1, page_size: 20 },
  });
  getMaintenanceAcceptance.mockResolvedValue({
    data: {
      deliverable_id: "deliverable-1",
      project_id: "project-1",
      deliverable_type: "acceptance_report",
      due_date: "2026-08-31",
      submission_status: "not_submitted",
      submitted_at: null,
      submitted_by: null,
      approval_status: "not_reviewed",
      approved_at: null,
      approved_by: null,
      rejection_reason: null,
      configuration_state: "configured",
      version: 1,
      review_policy: "admin_only_pending_business_role_configuration",
      attachments: [],
    },
  });
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
    expect(screen.getByText("回款 / 全部合同额（含税）")).toBeInTheDocument();
    expect(screen.getByText("项目已计成本（含税） / 全部合同额（含税）"))
      .toBeInTheDocument();
    expect(screen.getAllByText("合同额（含税）").length).toBeGreaterThanOrEqual(1);

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
    for (const header of [
      "单位成本（含税）",
      "单位成本（不含税）",
      "已计成本（含税）",
      "已计成本（不含税）",
    ]) {
      expect(within(requisitions).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(within(requisitions).getByText("PN-COSTED")).toBeInTheDocument();
    expect(within(requisitions).getByRole("columnheader", { name: "取价依据" }))
      .toBeInTheDocument();
    expect(within(requisitions).getByText("估算（销售前后 7 天数量加权）"))
      .toBeInTheDocument();
    expect(within(requisitions).getByText("已计入（估算）")).toBeInTheDocument();

    const expenses = screen.getByTestId("approved-expense-table");
    expect(within(expenses).getByText("审批通过")).toBeInTheDocument();
    expect(within(expenses).getAllByText("报销单号").length).toBeGreaterThanOrEqual(1);
    expect(within(expenses).getByText("BXD-202608-001")).toBeInTheDocument();
    expect(within(expenses).getByText("张三")).toBeInTheDocument();
    expect(within(expenses).getByText("差旅")).toBeInTheDocument();
    expect(within(expenses).getByText("现场支持")).toBeInTheDocument();
    expect(within(expenses).getByRole("columnheader", { name: "金额（含税）" }))
      .toBeInTheDocument();
    expect(within(expenses).getByRole("columnheader", { name: "金额（不含税）" }))
      .toBeInTheDocument();
    expect(screen.getByText("存在待补成本")).toBeInTheDocument();
    const tracking = screen.getByTestId("manager-tracking-card");
    expect(within(tracking).getByText("2026-01-01")).toBeInTheDocument();
    expect(within(tracking).getByText("2026-12-31")).toBeInTheDocument();
    expect(within(tracking).getByText(/XSDD-001 · 第 2 期/)).toBeInTheDocument();
    expect(within(tracking).getByText("已逾期 7 天")).toBeInTheDocument();
    expect(within(tracking).getByText("验收审批业务角色尚未配置")).toBeInTheDocument();
    expect(within(tracking).getByRole("button", { name: /上传验收附件/ }))
      .toBeInTheDocument();

    const preview = screen.getByTestId("workbook-four-sheet-preview");
    for (const sheet of ["01_总览", "02_备件消耗", "03_报销单", "04_项目经理追踪"]) {
      expect(within(preview).getByText(sheet)).toBeInTheDocument();
    }
    expect(within(preview).getByText("仅回款表尾可追加")).toBeInTheDocument();
    expect(within(preview).getAllByText("系统生成")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传月度更新" })).toBeInTheDocument();
    expect(screen.getByTestId("site-issue-workflow")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建领用单" })).toBeInTheDocument();
    expect(screen.getByTestId("bad-return-panel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建坏件返还单" })).toBeInTheDocument();
    expect(screen.getByText("仓库确认返还率")).toBeInTheDocument();
    expect(screen.queryByText(/扇形图|饼图/)).toBeNull();
  });

  it.each([
    ["inc", "含税", "不含税"],
    ["ex", "不含税", "含税"],
  ] as const)("维保金额口径为 %s 时只展示%s列", async (basis, shown, hidden) => {
    taxBasisState.value = basis;
    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    const requisitions = await screen.findByTestId("site-requisition-table");
    expect(within(requisitions).getByRole("columnheader", { name: `单位成本（${shown}）` }))
      .toBeInTheDocument();
    expect(within(requisitions).getByRole("columnheader", { name: `已计成本（${shown}）` }))
      .toBeInTheDocument();
    expect(within(requisitions).queryByRole("columnheader", { name: `单位成本（${hidden}）` }))
      .toBeNull();
    expect(within(requisitions).getByText(basis === "inc" ? "¥113" : "¥100"))
      .toBeInTheDocument();

    const expenses = screen.getByTestId("approved-expense-table");
    expect(within(expenses).getByRole("columnheader", { name: `金额（${shown}）` }))
      .toBeInTheDocument();
    expect(within(expenses).queryByRole("columnheader", { name: `金额（${hidden}）` }))
      .toBeNull();
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
      action_maintenance_site_issue_manage: false,
      action_maintenance_bad_return_manage: false,
    }));

    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "移动维保项目" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "去人工回填成本" })).toBeNull();
    expect(screen.queryByRole("button", { name: "上传月度更新" })).toBeNull();
    expect(screen.queryByTestId("site-issue-workflow")).toBeNull();
    expect(screen.getByTestId("bad-return-panel")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建坏件返还单" })).toBeNull();
    expect(screen.getByRole("button", { name: "下载完整四表" })).toBeInTheDocument();
  });

  it("坏件返还管理权限不依赖成本权限，且不连带开放现场领用管理", async () => {
    localStorage.setItem("role", "maintenance_operator");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: false,
      data_profit: false,
      action_maintenance_project_manage: false,
      action_maintenance_roundtrip_apply: false,
      action_maintenance_site_issue_manage: false,
      action_maintenance_bad_return_manage: true,
    }));

    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "移动维保项目" })).toBeInTheDocument();
    expect(screen.getByTestId("bad-return-panel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建坏件返还单" })).toBeInTheDocument();
    expect(screen.queryByTestId("site-issue-workflow")).toBeNull();
    expect(screen.queryByRole("link", { name: "去人工回填成本" })).toBeNull();
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
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: false,
      data_profit: false,
    }));
    getMaintenanceProjectWorkspace.mockResolvedValueOnce({
      data: {
        ...workspace,
        project: {
          ...workspace.project,
          metrics: {
            ...workspace.project.metrics,
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

  it("仅成本权限在费用完整度未知时仍展示现场领用成本、估算和取价证据", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: false,
    }));
    getMaintenanceProjectWorkspace.mockResolvedValueOnce({
      data: {
        ...workspace,
        project: {
          ...workspace.project,
          contracts: workspace.project.contracts.map((contract) => ({
            ...contract,
            contract_amount: null,
            received_amount: null,
            amount_status: "restricted",
          })),
          metrics: {
            ...workspace.project.metrics,
            total_contract_amount: null,
            known_contract_amount: null,
            contract_amount_complete: null,
            received_amount: null,
            collection_progress_pct: null,
            site_requisition_known_cost: 226,
            site_requisition_known_cost_ex_tax: 200,
            site_requisition_known_cost_inc_tax: 226,
            site_requisition_priced_cost_ex_tax: 200,
            site_requisition_priced_cost_inc_tax: 226,
            sales_estimate_cost_ex_tax: 200,
            sales_estimate_cost_inc_tax: 226,
            sales_estimate_lines: 1,
            cost_progress_includes_sales_estimate: true,
            cost_progress_label: "priced_cost_including_sales_estimate",
            approved_expense: null,
            approved_expense_ex_tax: null,
            approved_expense_inc_tax: null,
            actual_project_cost_known: null,
            actual_project_cost_known_ex_tax: null,
            actual_project_cost_known_inc_tax: null,
            cost_rate_lower_bound_pct: null,
            cost_status: null,
            cost_complete: null,
            missing_cost_lines: 0,
          },
        },
        collection_snapshots: {
          ...workspace.collection_snapshots,
          rows: [],
          total: 0,
        },
        requisitions: {
          ...workspace.requisitions,
          rows: [workspace.requisitions.rows[2]],
          total: 1,
        },
        approved_expenses: {
          ...workspace.approved_expenses,
          rows: [],
          total: 0,
        },
      },
    });

    render(
      <MemoryRouter>
        <MaintenanceProjectWorkspacePage projectId="project-1" />
      </MemoryRouter>,
    );

    const requisitions = await screen.findByTestId("site-requisition-table");
    expect(within(requisitions).getByText("PN-COSTED")).toBeInTheDocument();
    expect(within(requisitions).getByText("估算（销售前后 7 天数量加权）"))
      .toBeInTheDocument();
    expect(within(requisitions).getByText("已计入（估算）")).toBeInTheDocument();
    expect(screen.getByText(/销售回退估算（含税）.*¥226/)).toBeInTheDocument();
    expect(screen.getByText("现场领用成本可见；报销费用不可见")).toBeInTheDocument();
    expect(screen.queryByText("成本不可见/无权限")).toBeNull();
    expect(screen.queryByText("成本明细不可见")).toBeNull();
  });
});
