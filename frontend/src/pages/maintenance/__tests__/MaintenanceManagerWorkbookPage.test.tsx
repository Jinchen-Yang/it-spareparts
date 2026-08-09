import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getStatus = vi.fn();
const downloadWorkbook = vi.fn();
const validateWorkbook = vi.fn();
const applyWorkbook = vi.fn();

vi.mock("../../../api/maintenanceOperations", () => ({
  getMaintenanceManagerWorkbookStatus: (...args: unknown[]) => getStatus(...args),
  downloadMaintenanceManagerWorkbook: (...args: unknown[]) => downloadWorkbook(...args),
  validateMaintenanceManagerWorkbook: (...args: unknown[]) => validateWorkbook(...args),
  applyMaintenanceManagerWorkbook: (...args: unknown[]) => applyWorkbook(...args),
}));

import MaintenanceManagerWorkbookPage from "../MaintenanceManagerWorkbookPage";


const pendingStatus = {
  report_month: "2026-08-01",
  project_count: 3,
  scope_version: "scope-v1",
  data_version: "data-v1",
  latest_batch: null,
  acceptance_configuration: "pending_business_configuration",
  attachment_carrier: "controlled_business_file",
  approval_role: "admin_only_pending_business_configuration",
};


beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  getStatus.mockResolvedValue({ data: pendingStatus });
  downloadWorkbook.mockResolvedValue({ data: new Blob(["xlsx"]) });
  validateWorkbook.mockResolvedValue({
    data: {
      validation_token: "validation-v3",
      batch_id: "validation-v3",
      status: "valid",
      report_month: "2026-08-01",
      data_version: "data-v1",
      file_sha256: "a".repeat(64),
      changes: {
        service_periods: 1,
        planned_collection_milestones: 2,
        acceptance_due_dates: 1,
        total: 4,
      },
      items: [{
        kind: "planned_collection_milestone",
        project_id: "project-1",
        project_code: "XM-001",
        project_name: "合成项目",
        project_contract_id: "pc-1",
        contract_no: "XSDD-001",
        sequence: 3,
        before: { planned_date: null, planned_amount: null },
        after: { planned_date: "2026-09-20", planned_amount: "18000" },
      }],
      warnings: [{
        code: "partial_plan_node",
        message: "第 3 期只有计划日期，已保留并标注不完整",
        sheet: "02_计划回款节点",
        row: 8,
        column: "D",
      }],
      errors: [],
      unchanged: false,
      can_apply: true,
      already_applied: false,
      expires_at: "2026-08-10T12:00:00+00:00",
    },
  });
  applyWorkbook.mockResolvedValue({
    data: {
      applied: true,
      replayed: false,
      batch_id: "validation-v3",
      changed_rows: 3,
      project_count: 3,
      warnings: 1,
      report_month: "2026-08-01",
    },
  });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:manager-workbook"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
});

afterEach(() => cleanup());


describe("MaintenanceManagerWorkbookPage", () => {
  it("把全量下载、校验预览和原子应用放在一个可理解的流程里", async () => {
    render(
      <MemoryRouter>
        <MaintenanceManagerWorkbookPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("本人负责项目 3 个")).toBeInTheDocument();
    expect(screen.getByText("计划回款不等于财务确认实收")).toBeInTheDocument();
    expect(screen.getByText("管理员临时审批（业务角色待定）")).toBeInTheDocument();
    expect(screen.getByText("受控附件存储与下载审计")).toBeInTheDocument();
    expect(screen.getAllByText(/下载全量表/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/追加或更新/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/校验预览/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/确认应用/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "下载本月全量表" }));
    await waitFor(() => expect(downloadWorkbook).toHaveBeenCalledWith(expect.any(String)));

    const input = screen.getByLabelText("选择项目经理月度工作簿");
    const file = new File(["synthetic"], "manager-v3.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    fireEvent.change(input, { target: { files: [file] } });

    const preview = await screen.findByTestId("manager-workbook-validation-preview");
    expect(within(preview).getByText("维保期限 1 项")).toBeInTheDocument();
    expect(within(preview).getByText("计划回款节点 2 项")).toBeInTheDocument();
    expect(within(preview).getByText("验收截止日 1 项")).toBeInTheDocument();
    expect(within(preview).getByText(/只有计划日期/)).toBeInTheDocument();
    expect(within(preview).getByText("02_计划回款节点 · 第 8 行 · 第 D 列"))
      .toBeInTheDocument();
    expect(within(preview).getByText(/XSDD-001 · 第 3 期/)).toBeInTheDocument();
    expect(within(preview).getByText(/计划日期：空/)).toBeInTheDocument();
    expect(within(preview).getByText(/计划日期：2026-09-20/)).toBeInTheDocument();

    fireEvent.click(within(preview).getByRole("button", { name: "确认原子应用" }));
    await waitFor(() => {
      expect(applyWorkbook).toHaveBeenCalledWith({
        validation_token: "validation-v3",
        data_version: "data-v1",
      });
    });
    expect(await screen.findByText("已应用 3 项更新，本月任务已关闭")).toBeInTheDocument();
    expect(getStatus).toHaveBeenCalledTimes(2);
  });

  it("把上传失败的工作表行列定位直接展示给项目经理", async () => {
    validateWorkbook.mockRejectedValueOnce({
      response: {
        data: {
          detail: {
            message: "模板版本不匹配",
            issues: [{
              code: "template_version_mismatch",
              message: "只接受当前系统生成的模板",
              sheet: "00_系统元数据",
              row: 2,
              column: "B",
            }],
          },
        },
      },
    });
    render(
      <MemoryRouter>
        <MaintenanceManagerWorkbookPage />
      </MemoryRouter>,
    );
    await screen.findByText("本人负责项目 3 个");
    fireEvent.change(screen.getByLabelText("选择项目经理月度工作簿"), {
      target: {
        files: [new File(["bad"], "bad.xlsx", {
          type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })],
      },
    });
    expect(await screen.findByText("模板版本不匹配")).toBeInTheDocument();
    expect(screen.getByText("只接受当前系统生成的模板")).toBeInTheDocument();
    expect(screen.getByText("00_系统元数据 · 第 2 行 · 第 B 列")).toBeInTheDocument();
  });
});
