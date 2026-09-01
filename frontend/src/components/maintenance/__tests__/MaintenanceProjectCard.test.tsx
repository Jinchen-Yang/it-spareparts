import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import MaintenanceProjectCard from "../MaintenanceProjectCard";
import type {
  MaintenanceProjectOperationsSummary,
  MaintenanceProjectTask,
} from "../../../api/maintenanceOperations";
import type { ProjectFinancialVisibility } from "../ProjectFinancialProgress";

afterEach(cleanup);

const FULL_VISIBILITY: ProjectFinancialVisibility = {
  canViewCost: true,
  canViewContract: true,
  canViewExpense: true,
  canViewFinancial: true,
};

function makeTask(overrides: Partial<MaintenanceProjectTask> = {}): MaintenanceProjectTask {
  return {
    task_id: "task-1",
    project_id: "p1",
    rule_key: "acceptance:report_due",
    severity: "warning",
    title: "提交验收报告",
    detail: "提交必须包含至少一个有效附件",
    entity_id: null,
    task_type: "验收报告",
    due_date: null,
    due_state: "none",
    is_overdue: false,
    status: "pending",
    owner: null,
    generated_by: "system",
    close_basis: "验收报告已实名提交",
    ...overrides,
  };
}

function makeProject(
  overrides: Partial<MaintenanceProjectOperationsSummary> = {},
): MaintenanceProjectOperationsSummary {
  return {
    project_id: "p1",
    project_code: "PM-SYNTH-1",
    display_name: "合成运维项目",
    project_manager_id: "来源负责人原文",
    lifecycle_status: "ongoing",
    is_active: true,
    version: 1,
    manual_source_order_count: 0,
    contracts: [],
    metrics: {
      total_contract_amount: 100000,
      known_contract_amount: 100000,
      contract_amount_basis: "inc_tax",
      contract_amount_complete: true,
      received_amount: 0,
      collection_progress_pct: 0,
      site_requisition_known_cost: null,
      site_requisition_known_cost_ex_tax: null,
      site_requisition_known_cost_inc_tax: null,
      approved_expense: null,
      approved_expense_ex_tax: null,
      approved_expense_inc_tax: null,
      actual_project_cost_known: null,
      actual_project_cost_known_ex_tax: null,
      actual_project_cost_known_inc_tax: null,
      cost_progress_basis: "inc_tax",
      cost_rate_lower_bound_pct: null,
      cost_status: "normal",
      cost_complete: null,
      missing_cost_lines: null,
    },
    reminder_count: 1,
    manager_assignment: null,
    task_summary: { primary: null, open_count: 1, overdue_count: 0, rows: [] },
    missing_data_labels: ["验收附件待上传"],
    attachment_status: "missing",
    manager_tracking: {
      service_period: {
        service_start: "2026-01-01",
        service_end: "2026-12-31",
        completeness_state: "complete",
      },
      next_collection_milestone: null,
      acceptance: {
        deliverable_id: "d1",
        due_date: null,
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
    as_of: "2026-08-25",
    ...overrides,
  };
}

function renderCard(project: MaintenanceProjectOperationsSummary) {
  return render(
    <MemoryRouter>
      <MaintenanceProjectCard project={project} visibility={FULL_VISIBILITY} />
    </MemoryRouter>,
  );
}

describe("运维项目卡（2026-08-25 验收/月度口径清尾）", () => {
  it("「上传月度全量表」链接退役：即使旧数据仍带 pending 月度任务也不再渲染", () => {
    // 回归钉（同 b12c5fb 思想）：目标路由已重定向回维保主页，死入口不可再现；
    // 后端任务退役后 hasPendingMonthlyUpload 恒假，此处连旧载荷一并钉死。
    const legacyMonthly = makeTask({
      rule_key: "manager_update:2026-08",
      task_type: "项目经理月度更新",
      title: "待上传2026年08月月度全量工作簿",
      status: "pending",
    });
    renderCard(makeProject({
      task_summary: { primary: legacyMonthly, open_count: 1, overdue_count: 0, rows: [legacyMonthly] },
    }));
    expect(screen.queryByRole("link", { name: /上传月度全量表/ })).toBeNull();
    expect(screen.getByRole("link", { name: /进入项目/ }))
      .toHaveAttribute("href", "/maintenance/projects/p1");
  });

  it("项目详情入口使用正式路由并安全编码 project_id", () => {
    renderCard(makeProject({ project_id: "project/含空格" }));
    expect(screen.getByRole("link", { name: /进入项目/ }))
      .toHaveAttribute("href", "/maintenance/projects/project%2F%E5%90%AB%E7%A9%BA%E6%A0%BC");
  });

  it("验收标签挂提交状态：无截止日概念，不再渲染「截止日待补」/逾期", () => {
    const { container } = renderCard(makeProject());
    const text = container.textContent ?? "";
    expect(text).toContain("验收：待提交");
    expect(text).not.toContain("截止日待补");
    expect(text).not.toMatch(/验收：[^，]*，逾期/);
  });

  it("已提交项目验收标签显示「已提交」", () => {
    const project = makeProject();
    const tracking = project.manager_tracking!;
    tracking.acceptance = {
      ...tracking.acceptance,
      submission_status: "submitted",
      approval_status: "approved",
      attachment_count: 1,
    };
    const { container } = renderCard(project);
    expect(container.textContent).toContain("验收：已提交");
  });
});
