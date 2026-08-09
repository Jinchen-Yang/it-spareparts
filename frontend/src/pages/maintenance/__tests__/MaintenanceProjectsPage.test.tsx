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
  manager_assignment: {
    assignment_id: "assignment-1",
    project_id: "project-1",
    responsibility_type: "primary_manager",
    user_id: 9,
    username: "manager_account",
    display_name: "合成项目经理",
    account_status: "active",
    source_manager_text: "manager-1",
    version: 1,
    assigned_at: "2026-08-01T00:00:00+00:00",
    archived_at: null,
  },
  task_summary: {
    primary: {
      task_id: "task-1",
      project_id: "project-1",
      rule_key: "manager_update:2026-08",
      severity: "warning",
      title: "待上传2026年08月月度全量工作簿",
      detail: "项目经理本人范围的月度全量上传通道待接入",
      entity_id: null,
      task_type: "项目经理月度更新",
      due_date: "2026-08-31",
      due_state: "upcoming",
      is_overdue: false,
      status: "pending",
      owner: "manager_account",
      generated_by: "system",
      close_basis: "本人范围的月度全量上传批次成功应用后自动关闭（通道待接入）",
    },
    open_count: 4,
    overdue_count: 0,
    rows: [],
  },
  missing_data_labels: ["合同额待补", "成本待补", "附件状态待接入"],
  attachment_status: "not_integrated",
  as_of: "2026-08-08",
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
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
afterEach(() => {
  cleanup();
  localStorage.clear();
});

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
    expect(within(card).getByText(/合成项目经理 · manager_account/)).toBeInTheDocument();
    expect(within(card).getByText("待上传2026年08月月度全量工作簿")).toBeInTheDocument();
    expect(within(card).getByText(/完成依据：本人范围的月度全量上传批次/)).toBeInTheDocument();
    expect(within(card).getByText("月度全量上传待接入")).toBeInTheDocument();
    expect(within(card).queryByRole("link", { name: "月度全量上传待接入" })).toBeNull();
    expect(within(card).getByText("附件状态待接入")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "管理负责人" })).toBeInTheDocument();
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
    expect(listMaintenanceProjectOperations).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "联通", page: 1 }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("成本汇总被脱敏时卡片不误标为成本待补", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: false,
      data_profit: false,
    }));
    listMaintenanceProjectOperations.mockResolvedValueOnce({
      data: {
        rows: [{
          ...summary,
          missing_data_labels: [],
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

  it("仅成本权限的卡片保留现场领用成本和估算，不把费用完整度 null 标成成本不可见", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: false,
    }));
    listMaintenanceProjectOperations.mockResolvedValueOnce({
      data: {
        rows: [{
          ...summary,
          missing_data_labels: [],
          contracts: summary.contracts.map((contract) => ({
            ...contract,
            contract_amount: null,
            received_amount: null,
            amount_status: "restricted",
          })),
          metrics: {
            ...summary.metrics,
            total_contract_amount: null,
            known_contract_amount: null,
            contract_amount_complete: null,
            received_amount: null,
            collection_progress_pct: null,
            site_requisition_priced_cost_ex_tax: 398.23,
            site_requisition_priced_cost_inc_tax: 450,
            sales_estimate_cost_ex_tax: 106.19,
            sales_estimate_cost_inc_tax: 120,
            sales_estimate_lines: 2,
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
        }],
        total: 1,
        page: 1,
        page_size: 24,
        as_of: "2026-08-08",
        data_version: "cost-only-v1",
      },
    });

    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);

    const card = await screen.findByTestId("maintenance-project-card-project-1");
    expect(within(card).getByText(/现场领用已计成本（含税） ¥450/)).toBeInTheDocument();
    expect(within(card).getByText(/销售回退估算（含税） ¥120（2 行）/)).toBeInTheDocument();
    expect(within(card).getByText("现场领用成本可见；报销费用不可见"))
      .toBeInTheDocument();
    expect(within(card).getByText("项目总成本状态不可判定")).toBeInTheDocument();
    expect(within(card).queryByText("成本不可见/无权限")).toBeNull();
    expect(within(card).queryByText("成本不可见")).toBeNull();
  });

  it.each([
    ["无财务权限拒绝全量提醒", "all", false, false, false],
    ["无财务权限拒绝成本提醒", "cost:missing_price", false, false, false],
    ["仅成本权限允许成本提醒", "cost:missing_price", true, false, true],
    ["仅成本权限允许销售估算提醒", "cost:sales_fallback_estimate", true, false, true],
    ["仅成本权限允许现场领用缺价提醒", "completeness:missing_consumption_cost", true, false, true],
    ["仅成本权限拒绝费用完整度提醒", "completeness:expense_data_not_ready", true, false, false],
    ["仅成本权限拒绝回款提醒", "collection:incomplete", true, false, false],
    ["完整财务权限允许 severity", "warning", true, true, true],
    ["无财务权限允许月度更新", "manager_update:2026-08", false, false, true],
    ["无财务权限拒绝成本率提醒", "cost_ratio:red", false, false, false],
  ])("%s", async (_label, reminder, dataPurchaseCost, dataProfit, allowed) => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: dataPurchaseCost,
      data_profit: dataProfit,
    }));

    render(
      <MemoryRouter initialEntries={[`/maintenance/projects?reminder=${reminder}`]}>
        <MaintenanceProjectsPage />
      </MemoryRouter>,
    );

    if (allowed) {
      await waitFor(() => expect(listMaintenanceProjectOperations).toHaveBeenCalledWith(
        expect.objectContaining({ reminder }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ));
      expect(screen.queryByText("当前账号无权使用该提醒筛选")).toBeNull();
    } else {
      expect(await screen.findByText("当前账号无权使用该提醒筛选")).toBeInTheDocument();
      expect(listMaintenanceProjectOperations).not.toHaveBeenCalled();
      expect(screen.queryByText(/红色|80%|100%/)).toBeNull();
    }
  });

  it("后端拒绝过期权限快照时展示通用提示且不透传敏感详情", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
    }));
    listMaintenanceProjectOperations.mockRejectedValueOnce({
      response: {
        status: 403,
        data: { detail: "项目成本已达到 123.45%，真实状态为红色" },
      },
    });

    render(
      <MemoryRouter initialEntries={["/maintenance/projects?reminder=warning"]}>
        <MaintenanceProjectsPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("当前账号无权使用该提醒筛选")).toBeInTheDocument();
    expect(listMaintenanceProjectOperations).toHaveBeenCalledOnce();
    expect(screen.queryByText(/123\.45|真实状态|红色/)).toBeNull();
    expect(screen.queryByText("项目面板加载失败")).toBeNull();
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

  it("新筛选请求发出前取消旧请求，并坚持使用 POST body 参数", async () => {
    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);
    await screen.findByTestId("maintenance-project-card-project-1");
    const firstSignal = listMaintenanceProjectOperations.mock.calls[0][1].signal as AbortSignal;

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索维保项目" }), {
      target: { value: "二期" },
    });
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "搜索维保项目" }), {
      key: "Enter",
      code: "Enter",
    });

    await waitFor(() => expect(listMaintenanceProjectOperations).toHaveBeenCalledTimes(2));
    expect(firstSignal.aborted).toBe(true);
    expect(listMaintenanceProjectOperations.mock.calls[1][0]).toEqual(expect.objectContaining({
      q: "二期",
      owner_scope: "all",
    }));
    expect(listMaintenanceProjectOperations.mock.calls[1][1].signal).toBeInstanceOf(AbortSignal);
  });

  it("没有项目管理权限时不渲染负责人管理入口", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_project_manage: false,
    }));

    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);

    const card = await screen.findByTestId("maintenance-project-card-project-1");
    expect(within(card).queryByRole("button", { name: "管理负责人" })).toBeNull();
  });

  it("老板可看全量项目，但不显示仅管理员可用的负责人映射入口", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_project_manage: true,
    }));

    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);

    const card = await screen.findByTestId("maintenance-project-card-project-1");
    expect(within(card).queryByRole("button", { name: "管理负责人" })).toBeNull();
    expect(listMaintenanceProjectOperations.mock.calls[0][0]).toEqual(
      expect.objectContaining({ owner_scope: "all" }),
    );
  });

  it("已映射账号停用时明确标注负责人账号失效", async () => {
    listMaintenanceProjectOperations.mockResolvedValueOnce({
      data: {
        rows: [{
          ...summary,
          manager_assignment: {
            ...summary.manager_assignment,
            account_status: "inactive",
          },
        }],
        total: 1,
        page: 1,
        page_size: 24,
        as_of: "2026-08-08",
        data_version: "inactive-manager-v1",
      },
    });

    render(<MemoryRouter><MaintenanceProjectsPage /></MemoryRouter>);

    const card = await screen.findByTestId("maintenance-project-card-project-1");
    expect(within(card).getByText("负责人账号失效")).toBeInTheDocument();
  });
});
