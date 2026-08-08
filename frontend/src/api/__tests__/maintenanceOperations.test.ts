import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();

vi.mock("../../api", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    patch: (...args: unknown[]) => patch(...args),
  },
}));

import {
  applyMaintenanceProjectWorkbook,
  downloadMaintenanceProjectWorkbook,
  downloadMaintenanceWorkbookValidationErrors,
  getMaintenanceProjectWorkspace,
  listMaintenanceCostGaps,
  listMaintenanceProjectOperations,
  recomputeMaintenanceCostGaps,
  updateMaintenanceCostGap,
  validateMaintenanceProjectWorkbook,
} from "../maintenanceOperations";

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({ data: {} });
  patch.mockResolvedValue({ data: { references: [] } });
});

describe("maintenance operations API", () => {
  it("项目卡片只请求一次稳定项目批量摘要，不逐项目加载", () => {
    listMaintenanceProjectOperations({ page: 2, page_size: 24, q: "移动" });

    expect(get).toHaveBeenCalledOnce();
    expect(get).toHaveBeenCalledWith("/maintenance/projects/stable/operations", {
      params: { page: 2, page_size: 24, q: "移动", include_inactive: false },
    });
  });

  it("按稳定项目 ID 加载一份工作台快照", () => {
    getMaintenanceProjectWorkspace("project/危险");

    expect(get).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project%2F%E5%8D%B1%E9%99%A9/workspace",
      { params: {} },
    );
  });

  it("工作台请求透传三类独立服务端分页", () => {
    getMaintenanceProjectWorkspace("project-1", {
      collection_page: 2,
      collection_page_size: 10,
      requisition_page: 3,
      requisition_page_size: 50,
      expense_page: 4,
      expense_page_size: 100,
    });

    expect(get).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project-1/workspace",
      { params: {
        collection_page: 2,
        collection_page_size: 10,
        requisition_page: 3,
        requisition_page_size: 50,
        expense_page: 4,
        expense_page_size: 100,
      } },
    );
  });

  it("导出、校验并应用同一项目的全量四表工作簿", () => {
    const file = new File(["workbook"], "维保.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });

    downloadMaintenanceProjectWorkbook("project/1");
    validateMaintenanceProjectWorkbook("project/1", file);
    applyMaintenanceProjectWorkbook("project/1", {
      validation_token: "validation-1",
      data_version: "version-3",
    });

    expect(get).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project%2F1/workbook",
      { responseType: "blob" },
    );
    expect(post).toHaveBeenNthCalledWith(
      1,
      "/maintenance/projects/stable/project%2F1/workbook/validate",
      expect.any(FormData),
      { timeout: 120000 },
    );
    const form = post.mock.calls[0][1] as FormData;
    expect(form.get("file")).toBe(file);
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/projects/stable/project%2F1/workbook/apply",
      { validation_token: "validation-1", data_version: "version-3" },
    );
  });

  it("按校验令牌下载工作簿错误明细", () => {
    downloadMaintenanceWorkbookValidationErrors("token/危险");

    expect(get).toHaveBeenCalledWith(
      "/maintenance/workbook-validations/token%2F%E5%8D%B1%E9%99%A9/errors.xlsx",
      { responseType: "blob" },
    );
  });

  it("读取并按版本回填一个项目的缺价领用行", () => {
    listMaintenanceCostGaps("project/1", { page: 2, page_size: 20 });
    updateMaintenanceCostGap("project/1", {
      line_id: "line-9",
      version: 7,
      unit_cost_ex_tax: 12.5,
      evidence: "采购单 PO-1",
      reason: "核对采购发票",
    });

    expect(get).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project%2F1/cost-gaps",
      { params: { page: 2, page_size: 20 } },
    );
    expect(patch).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project%2F1/cost-gaps",
      {
        line_id: "line-9",
        version: 7,
        unit_cost_ex_tax: 12.5,
        evidence: "采购单 PO-1",
        reason: "核对采购发票",
      },
    );
  });

  it("按稳定项目重新匹配后到的系统价格证据", () => {
    recomputeMaintenanceCostGaps("project/1", {
      reason: "重新匹配后到采购或销售价格证据",
    });

    expect(post).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project%2F1/cost-gaps/recompute",
      { reason: "重新匹配后到采购或销售价格证据" },
    );
  });

  it("在 API 边界把 Decimal 字符串转成有限 number，非法值降级为 null", async () => {
    get.mockResolvedValueOnce({
      data: {
        rows: [{
          project_id: "project-1",
          project_code: "XM-1",
          display_name: "项目一",
          project_manager_id: null,
          lifecycle_status: "ongoing",
          is_active: true,
          version: 1,
          contracts: [{
            project_contract_id: "pc-1",
            contract_id: "c-1",
            contract_no: "HT-1",
            contract_amount: "1000.50",
            contract_amount_basis: "inc_tax",
            contract_status: "已生效",
            status_mapping_state: "mapped",
            included_in_total: true,
            is_effective: true,
            amount_status: "available",
            received_amount: "600.25",
          }],
          metrics: {
            total_contract_amount: "1000.50",
            known_contract_amount: "   ",
            contract_amount_basis: "inc_tax",
            contract_amount_complete: true,
            received_amount: "9007199254740991.1",
            site_requisition_known_cost: "300.10",
            site_requisition_known_cost_ex_tax: "265.58",
            site_requisition_known_cost_inc_tax: "300.10",
            approved_expense: "99.90",
            approved_expense_ex_tax: "88.41",
            approved_expense_inc_tax: "99.90",
            actual_project_cost_known: "400.00",
            actual_project_cost_known_ex_tax: "353.99",
            actual_project_cost_known_inc_tax: "400.00",
            cost_progress_basis: "inc_tax",
            cost_complete: true,
            missing_cost_lines: 0,
          },
          reminder_count: 0,
          as_of: "2026-08-08",
        }],
        total: 1,
        page: 1,
        page_size: 24,
        as_of: "2026-08-08",
        data_version: "v1",
      },
    });

    const { data } = await listMaintenanceProjectOperations();

    expect(data.rows[0].contracts[0].contract_amount).toBe(1000.5);
    expect(data.rows[0].contracts[0].contract_amount_basis).toBe("inc_tax");
    expect(data.rows[0].contracts[0].received_amount).toBe(600.25);
    expect(data.rows[0].metrics.site_requisition_known_cost).toBe(300.1);
    expect(data.rows[0].metrics.site_requisition_known_cost_ex_tax).toBe(265.58);
    expect(data.rows[0].metrics.site_requisition_known_cost_inc_tax).toBe(300.1);
    expect(data.rows[0].metrics.approved_expense_ex_tax).toBe(88.41);
    expect(data.rows[0].metrics.approved_expense_inc_tax).toBe(99.9);
    expect(data.rows[0].metrics.actual_project_cost_known_ex_tax).toBe(353.99);
    expect(data.rows[0].metrics.actual_project_cost_known_inc_tax).toBe(400);
    expect(data.rows[0].metrics.cost_progress_basis).toBe("inc_tax");
    expect(data.rows[0].metrics.known_contract_amount).toBeNull();
    expect(data.rows[0].metrics.received_amount).toBeNull();
  });

  it("工作台边界同时归一化领用与审批报销的含税、未税双值", async () => {
    get.mockResolvedValueOnce({
      data: {
        project: {
          project_id: "project-1",
          project_code: "XM-1",
          display_name: "项目一",
          project_manager_id: null,
          lifecycle_status: "ongoing",
          is_active: true,
          version: 1,
          contracts: [],
          metrics: {
            total_contract_amount: null,
            known_contract_amount: null,
            contract_amount_basis: "inc_tax",
            contract_amount_complete: true,
            received_amount: null,
            site_requisition_known_cost: "33.30",
            site_requisition_known_cost_ex_tax: "29.47",
            site_requisition_known_cost_inc_tax: "33.30",
            approved_expense: "8.80",
            approved_expense_ex_tax: "7.79",
            approved_expense_inc_tax: "8.80",
            actual_project_cost_known: "42.10",
            actual_project_cost_known_ex_tax: "37.26",
            actual_project_cost_known_inc_tax: "42.10",
            cost_progress_basis: "inc_tax",
            cost_complete: true,
            missing_cost_lines: 0,
          },
          reminder_count: 0,
          as_of: "2026-08-08",
        },
        collection_snapshots: {
          rows: [{
            collection_id: "collection-1",
            project_contract_id: "pc-1",
            contract_no: "HT-1",
            report_month: "2026-08-01",
            cumulative_amount: "18.80",
            receipt_reference: "RECEIPT-1",
            status: "confirmed",
            remark: null,
            version: 1,
          }],
          total: 1,
          page: 1,
          page_size: 20,
        },
        requisitions: {
          rows: [{
            line_id: "line-1",
            order_no: "WBDD-1",
            order_date: "2026-08-01",
            contract_no: null,
            pn: "PN-1",
            description: null,
            quantity: "3.5",
            unit_cost: "9.50",
            cost_amount: "33.25",
            unit_cost_ex_tax: "8.41",
            unit_cost_inc_tax: "9.50",
            cost_amount_ex_tax: "29.42",
            cost_amount_inc_tax: "33.25",
            cost_source: "manual",
            cost_status: "available",
          }, {
            line_id: "line-restricted",
            order_no: "WBDD-RESTRICTED",
            order_date: "2026-08-01",
            contract_no: null,
            pn: "PN-RESTRICTED",
            description: null,
            quantity: "1",
            unit_cost: "999",
            cost_amount: "999",
            unit_cost_ex_tax: "999",
            unit_cost_inc_tax: "999",
            cost_amount_ex_tax: "999",
            cost_amount_inc_tax: "999",
            cost_source: null,
            cost_status: "restricted",
          }],
          total: 2,
          page: 1,
          page_size: 20,
        },
        approved_expenses: {
          rows: [{
            expense_id: "expense-1",
            expense_date: "2026-08-02",
            contract_no: null,
            category: "差旅",
            reason: null,
            amount: "8.80",
            amount_ex_tax: "7.79",
            amount_inc_tax: "8.80",
            approval_status: "approved",
          }],
          total: 1,
          page: 1,
          page_size: 20,
        },
        reminders: [],
        workbook_preview: {
          protocol_version: "2.0",
          sheets: [],
          latest_tracking_month: null,
          last_exported_at: null,
          data_version: "v1",
        },
        as_of: "2026-08-08",
        data_version: "v1",
      },
    });

    const { data } = await getMaintenanceProjectWorkspace("project-1");

    expect(data.requisitions.rows[0]).toEqual(expect.objectContaining({
      quantity: 3.5,
      unit_cost: 9.5,
      cost_amount: 33.25,
      unit_cost_ex_tax: 8.41,
      unit_cost_inc_tax: 9.5,
      cost_amount_ex_tax: 29.42,
      cost_amount_inc_tax: 33.25,
    }));
    expect(data.requisitions.rows[1]).toEqual(expect.objectContaining({
      unit_cost: null,
      cost_amount: null,
      unit_cost_ex_tax: null,
      unit_cost_inc_tax: null,
      cost_amount_ex_tax: null,
      cost_amount_inc_tax: null,
    }));
    expect(data.approved_expenses.rows[0].amount).toBe(8.8);
    expect(data.approved_expenses.rows[0].amount_ex_tax).toBe(7.79);
    expect(data.approved_expenses.rows[0].amount_inc_tax).toBe(8.8);
    expect(data.project.metrics.actual_project_cost_known_ex_tax).toBe(37.26);
    expect(data.project.metrics.actual_project_cost_known_inc_tax).toBe(42.1);
    expect(data.collection_snapshots.rows[0].cumulative_amount).toBe(18.8);
  });

  it("未知或缺失的合同额和成本进度税口径在 API 边界降级为 null", async () => {
    get.mockResolvedValueOnce({
      data: {
        rows: [{
          project_id: "project-guard",
          project_code: "XM-GUARD",
          display_name: "税口径守卫",
          project_manager_id: null,
          lifecycle_status: "ongoing",
          is_active: true,
          version: 1,
          contracts: [{
            project_contract_id: "pc-guard",
            contract_id: "c-guard",
            contract_no: "HT-GUARD",
            contract_amount: "1000",
            contract_amount_basis: "ex_tax",
            contract_status: "已生效",
            status_mapping_state: "mapped",
            included_in_total: true,
            is_effective: true,
            amount_status: "available",
            received_amount: "0",
          }],
          metrics: {
            total_contract_amount: "1000",
            known_contract_amount: "1000",
            contract_amount_basis: "unknown",
            contract_amount_complete: true,
            received_amount: "0",
            site_requisition_known_cost: "0",
            site_requisition_known_cost_ex_tax: "0",
            site_requisition_known_cost_inc_tax: "0",
            approved_expense: "0",
            approved_expense_ex_tax: "0",
            approved_expense_inc_tax: "0",
            actual_project_cost_known: "0",
            actual_project_cost_known_ex_tax: "0",
            actual_project_cost_known_inc_tax: "0",
            cost_progress_basis: "ex_tax",
            cost_complete: true,
            missing_cost_lines: 0,
          },
          reminder_count: 0,
          as_of: "2026-08-09",
        }],
        total: 1,
        page: 1,
        page_size: 24,
        as_of: "2026-08-09",
        data_version: "guard-v1",
      },
    });

    const { data } = await listMaintenanceProjectOperations();

    expect(data.rows[0].contracts[0].contract_amount_basis).toBeNull();
    expect(data.rows[0].metrics.contract_amount_basis).toBeNull();
    expect(data.rows[0].metrics.cost_progress_basis).toBeNull();
  });

  it("成本回填成功响应在 API 边界保留并归一化单位成本和金额双值", async () => {
    patch.mockResolvedValueOnce({
      data: {
        issue_line_id: "line-9",
        version: 8,
        unit_cost: "113.00",
        cost_amount: "226.00",
        unit_cost_ex_tax: "100.00",
        unit_cost_inc_tax: "113.00",
        cost_amount_ex_tax: "200.00",
        cost_amount_inc_tax: "226.00",
        cost_source: "manual",
        manual_applied: true,
        resolution: "manual",
      },
    });

    const { data } = await updateMaintenanceCostGap("project-1", {
      line_id: "line-9",
      version: 7,
      unit_cost_ex_tax: 100,
      evidence: "采购发票",
      reason: "人工核对",
    });

    expect(data).toEqual(expect.objectContaining({
      unit_cost: 113,
      cost_amount: 226,
      unit_cost_ex_tax: 100,
      unit_cost_inc_tax: 113,
      cost_amount_ex_tax: 200,
      cost_amount_inc_tax: 226,
    }));
  });
});
