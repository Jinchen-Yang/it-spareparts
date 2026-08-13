import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

import MaintenanceMigrationPage, { businessDate } from "../MaintenanceMigrationPage";


const summary = {
  run_id: "migration-run-1",
  status: "previewed" as const,
  rule_version: "maintenance-cutover-v1",
  source_snapshot_hash: "c".repeat(64),
  as_of: "2026-08-09",
  blocker_count: 2,
  created_by: "creator-user",
  reconciled_by: null,
  approved_by: null,
  version: 1,
  created_at: "2026-08-09T12:00:00+00:00",
};

const truthComparison = {
  before: {
    parts_cost_ex_tax: "120.00",
    parts_cost_inc_tax: "135.60",
    approved_expense_ex_tax: "10.00",
    approved_expense_inc_tax: "11.30",
    total_ex_tax: "130.00",
    total_inc_tax: "146.90",
  },
  after: {
    parts_cost_ex_tax: "120.00",
    parts_cost_inc_tax: "135.60",
    approved_expense_ex_tax: "10.00",
    approved_expense_inc_tax: "11.30",
    total_ex_tax: "130.00",
    total_inc_tax: "146.90",
  },
  delta: {
    parts_cost_ex_tax: "0.00",
    parts_cost_inc_tax: "0.00",
    approved_expense_ex_tax: "0.00",
    approved_expense_inc_tax: "0.00",
    total_ex_tax: "0.00",
    total_inc_tax: "0.00",
  },
  after_candidate_values_applied: true as const,
  truth_comparison_hash: "9".repeat(64),
};

const detail = {
  run_id: "migration-run-1",
  status: "previewed" as const,
  rule_version: "maintenance-cutover-v1",
  request_fingerprint: "d".repeat(64),
  source_snapshot_hash: "c".repeat(64),
  as_of: "2026-08-09",
  preview: {
    input_fingerprint: "e".repeat(64),
    approval_blocker_count: 2,
    can_approve: false,
    production_activation_included: false as const,
    projects: [{
      project_id: "project-1",
      cutover_date: "2026-08-01",
      as_of: "2026-08-09",
      source_snapshot_hash: "f".repeat(64),
      source_coverage: {
        warehouse_source_ready: true,
        warehouse_ready_through: "2026-08-09",
        warehouse_required_through: "2026-08-09",
        project_version: 1,
      },
      evidence_summary: {
        historical_baseline: 1,
        legacy_cost_lines: 0,
        legacy_expenses: 0,
        truth_quantity_differences: 0,
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
        sales_estimate_cost_ex_tax: "20.00",
        sales_estimate_cost_inc_tax: "22.60",
        sales_estimate_lines: 1,
        cost_progress_includes_sales_estimate: true,
        cost_progress_label: "priced_cost_including_sales_estimate" as const,
        total_ex_tax: "30.00",
        total_inc_tax: "33.90",
      },
      inventory: [{
        balance_key: "project-1:1",
        opening_quantity: "10",
        delivery_quantity: "3",
        available_receipt_quantity: "2",
        closing_quantity: "9",
        ignored_site_issue_quantity: "2",
        ignored_return_registration_quantity: "1",
      }],
      truth_comparison: truthComparison,
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
    as_of: "2026-08-09",
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
      sales_estimate_cost_ex_tax: "20.00",
      sales_estimate_cost_inc_tax: "22.60",
      sales_estimate_lines: 1,
      cost_progress_includes_sales_estimate: true,
      cost_progress_label: "priced_cost_including_sales_estimate" as const,
      total_ex_tax: "30.00",
      total_inc_tax: "33.90",
    },
    truth_comparison: truthComparison,
    historical_baseline: {
      baseline_id: "baseline-1",
      amount_ex_tax: "100.00",
      amount_inc_tax: "113.00",
      evidence_hash: "a".repeat(64),
      coverage_from: "2026-01-01",
      coverage_through: "2026-07-31",
      scope: "site_issue_parts_only" as const,
      excludes_expenses: true as const,
      source_artifact_locator: "archive://maintenance/baseline-1.xlsx",
      source_row_count: 42,
      aggregation_fingerprint: "8".repeat(64),
      approval_state: "pending" as const,
      approved_by: null,
      approved_at: null,
      approval_reason: null,
      version: 1,
    },
    opening_balances: [{
      opening_balance_id: "opening-1",
      balance_key: "project-1:1",
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
    reason: "生成合成预检",
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
        movement_id: "document-1:line-1",
        document_id: "document-1",
        line_id: "line-1",
        document_no: "FH-001",
        document_date: "2026-08-03",
        movement_type: "delivery",
        balance_key: "project-1:1",
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
  it("默认日期使用上海业务日而不是 UTC 日期", () => {
    expect(businessDate(new Date("2026-08-09T16:30:00Z"))).toBe("2026-08-10");
  });

  it("明确显示生产关闭并以方块和表格展示可复算详情", async () => {
    render(<MaintenanceMigrationPage />);

    expect(screen.getByText("生产切换保持关闭")).toBeInTheDocument();
    expect(await screen.findByText("creator-user / — / —")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看" }));

    expect(await screen.findByText("MIG-001 · 合成迁移项目")).toBeInTheDocument();
    expect(screen.getByText("切换后现场领用（未税）")).toBeInTheDocument();
    expect(screen.getByText("销售回退估算（未税）")).toBeInTheDocument();
    expect(screen.getByText("项目已计成本包含销售回退估算")).toBeInTheDocument();
    expect(screen.getByText("旧口径（before）")).toBeInTheDocument();
    expect(screen.getByText("新口径（after，候选应用后）")).toBeInTheDocument();
    expect(screen.getByText("差额（after - before）")).toBeInTheDocument();
    expect(screen.getByText(/覆盖 2026-01-01 至 2026-07-31/)).toBeInTheDocument();
    expect(screen.getByText(/已排除报销费用/)).toBeInTheDocument();
    expect(screen.getByText(/archive:\/\/maintenance\/baseline-1.xlsx/)).toBeInTheDocument();
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
      expected_truth_comparison_hash: "9".repeat(64),
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
            movement_id: `document-${params.page}:line-${params.page}`,
            document_id: `document-${params.page}`,
            line_id: `line-${params.page}`,
            document_no: params.page === 1 ? "FH-001" : "FH-021",
            document_date: "2026-08-03",
            movement_type: "delivery",
            balance_key: "project-1:1",
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

  it("旧口径成本和报销证据使用可读列名并按分区读取", async () => {
    mocks.getMaintenanceMigrationRun.mockResolvedValue({
      data: {
        ...detail,
        preview: {
          ...detail.preview,
          projects: detail.preview.projects.map((project) => ({
            ...project,
            evidence_summary: {
              ...project.evidence_summary,
              legacy_cost_lines: 1,
              legacy_expenses: 1,
              truth_quantity_differences: 0,
            },
          })),
        },
      },
    });
    mocks.getMaintenanceMigrationEvidence.mockImplementation(
      (_runId: string, _projectId: string, params: { section: string }) => Promise.resolve({
        data: {
          run_id: "migration-run-1",
          project_id: "project-1",
          section: params.section,
          source_snapshot_hash: "f".repeat(64),
          items: params.section === "legacy_cost_lines" ? [{
            source_order_id: "legacy-order-1",
            source_line_id: "legacy-line-1",
            order_no: "WBDD-001",
            order_date: "2026-07-01",
            pn: "PN-LEGACY",
            sn: "SN-LEGACY",
            demand_quantity: "5",
            return_quantity: "1",
            effective_quantity: "4",
            unit_cost_ex_tax: "10.00",
            unit_cost_inc_tax: "11.30",
            cost_tax_basis: "ex",
            cost_amount_ex_tax: "40.00",
            cost_amount_inc_tax: "45.20",
          }] : [{
            expense_id: "legacy-expense-1",
            expense_ref: "BX-001",
            expense_date: "2026-07-15",
            normalized_status: "approved",
            raw_status: "已结束",
            contract_no: "XSDD-001",
            project_contract_id: "project-contract-1",
            contract_id: "contract-1",
            contract_relation_version: 3,
            contract_effective_from: "2026-01-01",
            contract_effective_to: null,
            tax_basis: "default_ex",
            amount_ex_tax: "30.00",
            amount_inc_tax: "33.90",
            import_batch_id: 12,
          }],
          total: 1,
          page: 1,
          page_size: 20,
        },
      }),
    );

    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "查看" }));

    expect(await screen.findByText("WBDD-001")).toBeInTheDocument();
    expect(screen.getAllByText("旧口径有效数量").length).toBeGreaterThan(0);
    expect(mocks.getMaintenanceMigrationEvidence).toHaveBeenLastCalledWith(
      "migration-run-1",
      "project-1",
      { section: "legacy_cost_lines", page: 1, page_size: 20 },
    );

    fireEvent.mouseDown(screen.getByRole("combobox", {
      name: "MIG-001 · 合成迁移项目 证据分区",
    }));
    await screen.findByRole("option", { name: "旧口径已审批报销（1）" });
    const expenseOption = screen.getAllByText("旧口径已审批报销（1）")
      .find((node) => node.closest(".ant-select-item-option"));
    fireEvent.click(expenseOption!.closest(".ant-select-item-option")!);

    expect(await screen.findByText("BX-001")).toBeInTheDocument();
    expect(screen.getAllByText("报销日期").length).toBeGreaterThan(0);
    expect(screen.getAllByText("税价基准").length).toBeGreaterThan(0);
    expect(screen.getAllByText("项目合同关系 ID").length).toBeGreaterThan(0);
    expect(screen.getByText("project-contract-1")).toBeInTheDocument();
    expect(screen.getByText("default_ex")).toBeInTheDocument();
    expect(screen.getByText("2026-07-15")).toBeInTheDocument();
    expect(mocks.getMaintenanceMigrationEvidence).toHaveBeenLastCalledWith(
      "migration-run-1",
      "project-1",
      { section: "legacy_expenses", page: 1, page_size: 20 },
    );
  });

  it("新旧数量差异展示 before、after、delta 与稳定来源键", async () => {
    mocks.getMaintenanceMigrationRun.mockResolvedValue({
      data: {
        ...detail,
        preview: {
          ...detail.preview,
          projects: detail.preview.projects.map((project) => ({
            ...project,
            evidence_summary: {
              ...project.evidence_summary,
              truth_quantity_differences: 1,
            },
          })),
        },
      },
    });
    mocks.getMaintenanceMigrationEvidence.mockResolvedValue({
      data: {
        run_id: "migration-run-1",
        project_id: "project-1",
        section: "truth_quantity_differences",
        source_snapshot_hash: "f".repeat(64),
        items: [{
          comparison_key: "legacy:order-1:line-1",
          source_order_id: "order-1",
          source_line_id: "line-1",
          document_no: "WBDD-TRUTH-1",
          pn: "PN-TRUTH",
          sn: "SN-TRUTH",
          before_quantity: "5",
          after_quantity: "3",
          delta_quantity: "-2",
        }],
        total: 1,
        page: 1,
        page_size: 20,
      },
    });

    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "查看" }));

    expect(await screen.findByText("WBDD-TRUTH-1")).toBeInTheDocument();
    expect(screen.getAllByText("旧口径数量").length).toBeGreaterThan(0);
    expect(screen.getAllByText("现场领用数量").length).toBeGreaterThan(0);
    expect(screen.getAllByText("数量差额").length).toBeGreaterThan(0);
    expect(screen.getByText("legacy:order-1:line-1")).toBeInTheDocument();
    expect(mocks.getMaintenanceMigrationEvidence).toHaveBeenLastCalledWith(
      "migration-run-1",
      "project-1",
      { section: "truth_quantity_differences", page: 1, page_size: 20 },
    );
  });

  it("快速切换详情时忽略较早请求的迟到响应", async () => {
    let resolveFirst: ((value: { data: typeof detail }) => void) | undefined;
    let resolveSecond: ((value: { data: typeof detail }) => void) | undefined;
    mocks.searchMaintenanceMigrationRuns.mockResolvedValue({
      data: {
        items: [
          summary,
          { ...summary, run_id: "migration-run-2", created_by: "second-list-owner" },
        ],
        total: 2,
        page: 1,
        page_size: 20,
      },
    });
    mocks.getMaintenanceMigrationRun
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));

    render(<MaintenanceMigrationPage />);
    expect(await screen.findByText("second-list-owner / — / —")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "查看" });
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);

    await act(async () => {
      resolveSecond?.({
        data: {
          ...detail,
          run_id: "migration-run-2",
          created_by: "second-detail-owner",
        },
      });
    });
    expect(await screen.findByText("second-detail-owner")).toBeInTheDocument();

    await act(async () => {
      resolveFirst?.({ data: { ...detail, created_by: "stale-first-owner" } });
    });
    expect(screen.queryByText("stale-first-owner")).not.toBeInTheDocument();
    expect(screen.getByText("second-detail-owner")).toBeInTheDocument();
  });

  it("新建预检 前先阻止缺理由和缺项目的黑盒提交", async () => {
    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "新建预检" }));
    fireEvent.click(screen.getByRole("button", { name: "生成核对清单" }));

    expect(await screen.findByText("请填写生成本次预检的业务理由。")).toBeInTheDocument();
    expect(mocks.previewMaintenanceMigration).not.toHaveBeenCalled();
  });

  it("历史基线必须绑定覆盖边界、来源工件、行数和双 SHA 后才可提交", async () => {
    mocks.previewMaintenanceMigration.mockResolvedValue({ data: detail });
    render(<MaintenanceMigrationPage />);
    await screen.findByText("creator-user / — / —");
    fireEvent.click(screen.getByRole("button", { name: "新建预检" }));

    const projectSelect = screen.getByRole("combobox", { name: "项目 1 稳定项目" });
    fireEvent.mouseDown(projectSelect);
    await screen.findByRole("option", { name: "MIG-001 · 合成迁移项目" });
    const projectOption = screen.getAllByText("MIG-001 · 合成迁移项目")
      .find((node) => node.closest(".ant-select-item-option"));
    fireEvent.click(projectOption!.closest(".ant-select-item-option")!);
    fireEvent.change(screen.getByLabelText("项目 1 切换日期"), {
      target: { value: "2026-08-01" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 历史基线未税金额" }), {
      target: { value: "100.00" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 历史基线含税金额" }), {
      target: { value: "113.00" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 基线证据哈希" }), {
      target: { value: "A".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("项目 1 基线覆盖起点"), {
      target: { value: "2026-01-01" },
    });
    fireEvent.change(screen.getByLabelText("项目 1 基线覆盖截止日"), {
      target: { value: "2026-08-01" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 来源工件定位" }), {
      target: { value: "archive://maintenance/baseline.xlsx" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 来源明细行数" }), {
      target: { value: "42" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 聚合指纹" }), {
      target: { value: "bad" },
    });
    fireEvent.change(screen.getByPlaceholderText("说明本次核对的范围、证据日期和责任人"), {
      target: { value: "核对历史基线机器契约" },
    });

    fireEvent.click(screen.getByRole("button", { name: "生成核对清单" }));
    expect(await screen.findByText("历史基线覆盖截止日必须精确为切换日前一日。")).toBeInTheDocument();
    expect(mocks.previewMaintenanceMigration).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("项目 1 基线覆盖截止日"), {
      target: { value: "2026-07-31" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成核对清单" }));
    expect(await screen.findByText("历史基线聚合指纹必须填写 64 位 SHA-256。")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "项目 1 聚合指纹" }), {
      target: { value: "B".repeat(64) },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成核对清单" }));
    await waitFor(() => expect(mocks.previewMaintenanceMigration).toHaveBeenCalledTimes(1));
    expect(mocks.previewMaintenanceMigration.mock.calls[0][0].projects[0].historical_baseline).toEqual({
      amount_ex_tax: "100.00",
      amount_inc_tax: "113.00",
      evidence_hash: "a".repeat(64),
      coverage_from: "2026-01-01",
      coverage_through: "2026-07-31",
      scope: "site_issue_parts_only",
      excludes_expenses: true,
      source_artifact_locator: "archive://maintenance/baseline.xlsx",
      source_row_count: 42,
      aggregation_fingerprint: "b".repeat(64),
    });
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
