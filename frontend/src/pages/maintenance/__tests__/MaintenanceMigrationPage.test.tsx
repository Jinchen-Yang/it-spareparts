import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  approveMaintenanceMigrationRun: vi.fn(),
  getMaintenanceMigrationEvidence: vi.fn(),
  getMaintenanceMigrationRun: vi.fn(),
  previewMaintenanceMigration: vi.fn(),
  reconcileMaintenanceMigrationRun: vi.fn(),
  searchMaintenanceMigrationRuns: vi.fn(),
  listMaintenanceProjects: vi.fn(),
}));

vi.mock("../../../api/maintenanceMigration", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceMigration")>(
    "../../../api/maintenanceMigration",
  );
  return { ...actual, ...mocks };
});

vi.mock("../../../api/maintenanceProjects", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceProjects")>(
    "../../../api/maintenanceProjects",
  );
  return { ...actual, listMaintenanceProjects: mocks.listMaintenanceProjects };
});

import MaintenanceMigrationPage from "../MaintenanceMigrationPage";


const summary = {
  run_id: "migration-run-1",
  status: "previewed" as const,
  rule_version: "maintenance-cutover-v1",
  source_snapshot_hash: "c".repeat(64),
  blocker_count: 2,
  created_by: "creator-user",
  reconciled_by: null,
  approved_by: null,
  version: 1,
  created_at: "2026-08-09T12:00:00+00:00",
};

const detail = {
  run_id: "migration-run-1",
  status: "previewed" as const,
  rule_version: "maintenance-cutover-v1",
  request_fingerprint: "d".repeat(64),
  source_snapshot_hash: "c".repeat(64),
  preview: {
    input_fingerprint: "e".repeat(64),
    approval_blocker_count: 2,
    can_approve: false,
    production_activation_included: false as const,
    projects: [{
      project_id: "project-1",
      cutover_date: "2026-08-01",
      source_snapshot_hash: "f".repeat(64),
      source_coverage: {
        warehouse_source_ready: true,
        project_version: 1,
      },
      evidence_summary: {
        historical_baseline: 1,
        historical_site_issues: 0,
        post_cutover_site_issues: 0,
        expenses: 0,
        opening_balances: 1,
        inventory_movements: 1,
      },
      cost: {
        historical_baseline_ex_tax: "0.00",
        historical_baseline_inc_tax: "0.00",
        post_cutover_consumption_ex_tax: "20.00",
        post_cutover_consumption_inc_tax: "22.60",
        approved_expense_ex_tax: "10.00",
        approved_expense_inc_tax: "11.30",
        total_ex_tax: "30.00",
        total_inc_tax: "33.90",
      },
      inventory: [{
        balance_key: "project-1:part-1",
        opening_quantity: "10",
        delivery_quantity: "3",
        available_receipt_quantity: "2",
        closing_quantity: "9",
        ignored_site_issue_quantity: "2",
        ignored_return_registration_quantity: "1",
      }],
      approval_blockers: [
        { code: "historical_baseline_not_approved", detail: "历史成本基线尚未实名审批" },
        { code: "opening_balance_not_approved", detail: "库存期初尚未实名审批" },
      ],
      can_approve: false,
    }],
  },
  manifest: null,
  manifest_hash: null,
  manifest_key_id: null,
  created_by: "creator-user",
  reconciled_by: null,
  approved_by: null,
  version: 1,
  created_at: "2026-08-09T12:00:00+00:00",
  plans: [{
    plan_id: "plan-1",
    project_id: "project-1",
    cutover_date: "2026-08-01",
    historical_mode: "approved_cost_baseline",
    blocker_count: 2,
    status: "previewed" as const,
    reconciled_by: null,
    reconciled_at: null,
    reconciliation_reason: null,
    version: 1,
    cost: {
      historical_ex_tax: "0.00",
      historical_inc_tax: "0.00",
      post_cutover_ex_tax: "20.00",
      post_cutover_inc_tax: "22.60",
      approved_expense_ex_tax: "10.00",
      approved_expense_inc_tax: "11.30",
      total_ex_tax: "30.00",
      total_inc_tax: "33.90",
    },
    historical_baseline: {
      baseline_id: "baseline-1",
      amount_ex_tax: "100.00",
      amount_inc_tax: "113.00",
      evidence_hash: "a".repeat(64),
      approval_state: "pending" as const,
      approved_by: null,
      approved_at: null,
      approval_reason: null,
      version: 1,
    },
    opening_balances: [{
      opening_balance_id: "opening-1",
      balance_key: "project-1:part-1",
      pn: "PN-001",
      quantity: "10",
      evidence_hash: "b".repeat(64),
      approval_state: "pending" as const,
      approved_by: null,
      approved_at: null,
      approval_reason: null,
      version: 1,
    }],
    discrepancies: [{
      discrepancy_id: "discrepancy-1",
      code: "historical_baseline_not_approved",
      entity_id: null,
      severity: "blocker" as const,
      status: "open" as const,
      detail: { detail: "历史成本基线尚未实名审批" },
      resolved_by: null,
      version: 1,
    }],
  }],
  events: [{
    event_id: "event-1",
    action: "preview" as const,
    from_status: null,
    to_status: "previewed" as const,
    reason: "生成合成 dry-run",
    operated_by: "creator-user",
    operated_at: "2026-08-09T12:00:00+00:00",
  }],
  production_activation_included: false as const,
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  mocks.searchMaintenanceMigrationRuns.mockResolvedValue({
    data: { items: [summary], total: 1, page: 1, page_size: 20 },
  });
  mocks.listMaintenanceProjects.mockResolvedValue({
    data: {
      rows: [{
        project_id: "project-1",
        project_code: "MIG-001",
        display_name: "合成迁移项目",
        project_manager_id: "manager-1",
        lifecycle_status: "ongoing",
        is_active: true,
        version: 1,
      }],
      total: 1,
      page: 1,
      page_size: 200,
      as_of: "2026-08-09",
      data_version: "v1",
    },
  });
  mocks.getMaintenanceMigrationRun.mockResolvedValue({ data: detail });
  mocks.getMaintenanceMigrationEvidence.mockResolvedValue({
    data: {
      run_id: "migration-run-1",
      project_id: "project-1",
      section: "inventory_movements",
      source_snapshot_hash: "f".repeat(64),
      items: [{
        movement_id: "movement-1",
        document_id: "document-1",
        document_no: "FH-001",
        document_date: "2026-08-03",
        movement_type: "delivery",
        balance_key: "project-1:part-1",
        pn: "PN-001",
        sn: "SN-001",
        quantity: "3",
      }],
      total: 1,
      page: 1,
      page_size: 20,
    },
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceMigrationPage", () => {
  it("明确显示生产关闭并以方块和表格展示可复算详情", async () => {
    render(<MaintenanceMigrationPage />);

    expect(screen.getByText("生产切换保持关闭")).toBeInTheDocument();
    expect(await screen.findByText("creator-user / — / —")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看" }));

    expect(await screen.findByText("MIG-001 · 合成迁移项目")).toBeInTheDocument();
    expect(screen.getByText("切换后现场领用")).toBeInTheDocument();
    expect(screen.getByText("正式可用入库")).toBeInTheDocument();
    expect(screen.getByText("历史成本基线尚未实名审批")).toBeInTheDocument();
    expect(await screen.findByText("FH-001")).toBeInTheDocument();
    expect(screen.getByText("SN-001")).toBeInTheDocument();
    expect(mocks.getMaintenanceMigrationEvidence).toHaveBeenCalledWith(
      "migration-run-1",
      "project-1",
      { section: "inventory_movements", page: 1, page_size: 20 },
    );
    expect(screen.queryByRole("button", { name: /启用生产/ })).not.toBeInTheDocument();
  });

  it("网络失败重试实名对账时复用同一个幂等键", async () => {
    mocks.reconcileMaintenanceMigrationRun
      .mockRejectedValueOnce({ response: { data: { detail: "模拟响应丢失" } } })
      .mockResolvedValueOnce({
        data: {
          ...detail,
          status: "reconciled",
          version: 2,
          reconciled_by: "reconciler-user",
          preview: { ...detail.preview, approval_blocker_count: 0, can_approve: true },
        },
      });
    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    const reconcile = await screen.findByRole("button", { name: "实名对账" });
    expect(reconcile).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "确认 MIG-001 历史成本基线" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "确认 MIG-001 库存期初 PN-001" }));
    fireEvent.change(screen.getByLabelText("MIG-001 项目对账理由"), {
      target: { value: "逐项核对本项目来源、金额与数量" },
    });
    fireEvent.click(screen.getByRole("checkbox", {
      name: "已查看 MIG-001 全部分页证据并确认候选完整",
    }));
    expect(reconcile).toBeEnabled();
    fireEvent.click(reconcile);
    fireEvent.change(screen.getByPlaceholderText("填写核对依据和结论；必须是可审计的业务理由"), {
      target: { value: "已逐项检查成本基线和库存期初" },
    });
    fireEvent.click(screen.getByRole("button", { name: "记录对账" }));

    expect(await screen.findByText("模拟响应丢失")).toBeInTheDocument();
    const firstBody = mocks.reconcileMaintenanceMigrationRun.mock.calls[0][1];
    fireEvent.click(screen.getByRole("button", { name: "记录对账" }));
    await waitFor(() => expect(mocks.reconcileMaintenanceMigrationRun).toHaveBeenCalledTimes(2));
    const secondBody = mocks.reconcileMaintenanceMigrationRun.mock.calls[1][1];
    expect(secondBody.operation_key).toBe(firstBody.operation_key);
    expect(secondBody.expected_version).toBe(1);
    expect(secondBody.project_signoffs).toEqual([{
      project_id: "project-1",
      expected_plan_version: 1,
      reason: "逐项核对本项目来源、金额与数量",
      historical_baseline: { baseline_id: "baseline-1", expected_version: 1 },
      opening_balances: [{ opening_balance_id: "opening-1", expected_version: 1 }],
    }]);
  });

  it("证据表按服务端分页读取而不是把全部来源塞进详情", async () => {
    mocks.getMaintenanceMigrationEvidence.mockImplementation(
      (_runId: string, _projectId: string, params: { page: number }) => Promise.resolve({
        data: {
          run_id: "migration-run-1",
          project_id: "project-1",
          section: "inventory_movements",
          source_snapshot_hash: "f".repeat(64),
          items: [{
            movement_id: `movement-${params.page}`,
            document_id: `document-${params.page}`,
            document_no: params.page === 1 ? "FH-001" : "FH-021",
            document_date: "2026-08-03",
            movement_type: "delivery",
            balance_key: "project-1:part-1",
            pn: "PN-001",
            sn: null,
            quantity: "3",
          }],
          total: 21,
          page: params.page,
          page_size: 20,
        },
      }),
    );
    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "查看" }));

    expect(await screen.findByText("FH-001")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("2"));
    expect(await screen.findByText("FH-021")).toBeInTheDocument();
    expect(mocks.getMaintenanceMigrationEvidence).toHaveBeenLastCalledWith(
      "migration-run-1",
      "project-1",
      { section: "inventory_movements", page: 2, page_size: 20 },
    );
  });

  it("新建 dry-run 前先阻止缺理由和缺项目的黑盒提交", async () => {
    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "新建 dry-run" }));
    fireEvent.click(screen.getByRole("button", { name: "生成核对清单" }));

    expect(await screen.findByText("请填写生成本次 dry-run 的业务理由。")).toBeInTheDocument();
    expect(mocks.previewMaintenanceMigration).not.toHaveBeenCalled();
  });

  it("有未解决 blocker 时不允许点击独立审批", async () => {
    mocks.getMaintenanceMigrationRun.mockResolvedValue({
      data: { ...detail, status: "reconciled", version: 2 },
    });
    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "查看" }));

    const approve = await screen.findByRole("button", { name: "独立审批" });
    expect(approve).toBeDisabled();
  });
});
