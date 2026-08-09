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
  archiveMaintenanceProjectManager,
  assignMaintenanceProjectManager,
  confirmSiteIssue,
  createSiteIssueDraft,
  downloadMaintenanceProjectWorkbook,
  downloadMaintenanceWorkbookValidationErrors,
  getMaintenanceProjectWorkspace,
  listMaintenanceCostGaps,
  listMaintenanceProjectOperations,
  patchSiteIssue,
  previewSiteIssue,
  recomputeMaintenanceCostGaps,
  searchMaintenanceManagerAccounts,
  searchSiteIssueCandidates,
  searchSiteIssues,
  updateMaintenanceCostGap,
  validateMaintenanceProjectWorkbook,
  voidSiteIssue,
} from "../maintenanceOperations";

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  get.mockResolvedValue({ data: {} });
  post.mockResolvedValue({ data: {} });
  patch.mockResolvedValue({ data: { references: [] } });
});

describe("maintenance operations API", () => {
  it("搜索词仅通过 POST body 发送，不进入请求 URL", () => {
    listMaintenanceProjectOperations({ page: 2, page_size: 24, q: "移动" });

    expect(post).toHaveBeenCalledOnce();
    expect(post).toHaveBeenCalledWith("/maintenance/projects/stable/operations/search", {
      page: 2,
      page_size: 24,
      q: "移动",
      include_inactive: false,
    });
    expect(get).not.toHaveBeenCalled();
  });

  it("无搜索词也把全部筛选放在 POST body，不使用查询串", () => {
    listMaintenanceProjectOperations({
      page: 2,
      page_size: 24,
      owner_scope: "me",
      task_type: "项目经理月度更新",
      task_status: "pending",
      due_from: "2026-08-01",
      due_to: "2026-08-31",
    });

    expect(post).toHaveBeenCalledOnce();
    expect(post).toHaveBeenCalledWith("/maintenance/projects/stable/operations/search", {
      page: 2,
      page_size: 24,
      q: "",
      include_inactive: false,
      owner_scope: "me",
      task_type: "项目经理月度更新",
      task_status: "pending",
      due_from: "2026-08-01",
      due_to: "2026-08-31",
    });
    expect(get).not.toHaveBeenCalled();
  });

  it("现场领用候选与单据搜索只通过 POST body，稳定 ID 做 URL 编码", () => {
    searchSiteIssueCandidates("project/危险", { q: "SN 敏感词", page: 2 });
    searchSiteIssues({
      project_id: "project/危险",
      q: "领用单 敏感词",
      workflow_statuses: ["draft", "confirmed"],
    });

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/maintenance/projects/stable/project%2F%E5%8D%B1%E9%99%A9/issue-candidates/search",
      { q: "SN 敏感词", page: 2, page_size: 50 },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/site-issues/search",
      {
        project_id: "project/危险",
        q: "领用单 敏感词",
        workflow_statuses: ["draft", "confirmed"],
        page: 1,
        page_size: 20,
      },
    );
    expect(get).not.toHaveBeenCalled();
  });

  it("现场领用完整动作使用系统单据 ID，并把版本和幂等键放在 body", () => {
    createSiteIssueDraft("project/1", {
      idempotency_key: "draft-command-001",
      issue_date: "2026-08-09",
      receiver: "接收人",
      issued_by: "发出人",
      site_location: "现场 A",
      lines: [{ delivery_line_id: "delivery-1", quantity: 2 }],
      reason: "保存草稿",
    });
    patchSiteIssue("issue/1", {
      project_id: "project/1",
      version: 1,
      idempotency_key: "patch-command-001",
      receiver: "新接收人",
      reason: "修改草稿",
    });
    previewSiteIssue("issue/1", { project_id: "project/1", version: 2 });
    confirmSiteIssue("issue/1", {
      project_id: "project/1",
      version: 2,
      idempotency_key: "confirm-command-001",
      reason: "确认领用",
    });
    voidSiteIssue("issue/1", {
      project_id: "project/1",
      version: 3,
      idempotency_key: "void-command-001",
      reason: "作废领用",
    });

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/maintenance/projects/stable/project%2F1/site-issues",
      expect.objectContaining({ idempotency_key: "draft-command-001" }),
    );
    expect(patch).toHaveBeenCalledWith(
      "/maintenance/site-issues/issue%2F1",
      expect.objectContaining({ version: 1, idempotency_key: "patch-command-001" }),
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/site-issues/issue%2F1/preview",
      { project_id: "project/1", version: 2 },
    );
    expect(post).toHaveBeenNthCalledWith(
      3,
      "/maintenance/site-issues/issue%2F1/confirm",
      expect.objectContaining({ version: 2, idempotency_key: "confirm-command-001" }),
    );
    expect(post).toHaveBeenNthCalledWith(
      4,
      "/maintenance/site-issues/issue%2F1/void",
      expect.objectContaining({ version: 3, idempotency_key: "void-command-001" }),
    );
  });

  it("把 AbortSignal 交给 POST 请求，不混入业务 body", () => {
    const controller = new AbortController();

    listMaintenanceProjectOperations(
      { q: "项目" },
      { signal: controller.signal },
    );

    expect(post).toHaveBeenCalledWith(
      "/maintenance/projects/stable/operations/search",
      { q: "项目", include_inactive: false },
      { signal: controller.signal },
    );
  });

  it("负责人候选只用 POST 搜索，映射和归档均带版本与原因", () => {
    searchMaintenanceManagerAccounts({ q: " 王经理 ", page_size: 30 });
    assignMaintenanceProjectManager("project/1", {
      user_id: 9,
      expected_assignment_id: "assignment-1",
      expected_assignment_version: 2,
      reason: "项目交接",
    });
    archiveMaintenanceProjectManager("assignment/1", {
      version: 3,
      reason: "暂停负责关系",
    });

    expect(post).toHaveBeenNthCalledWith(
      1,
      "/maintenance/project-manager-assignments/search",
      { q: "王经理", page: 1, page_size: 30 },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/projects/stable/project%2F1/manager-assignment",
      {
        user_id: 9,
        expected_assignment_id: "assignment-1",
        expected_assignment_version: 2,
        reason: "项目交接",
      },
    );
    expect(post).toHaveBeenNthCalledWith(
      3,
      "/maintenance/project-manager-assignments/assignment%2F1/archive",
      { version: 3, reason: "暂停负责关系" },
    );
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

  it("零元人工回填请求保留成本 0 和证据，不把 0 当成空值", () => {
    updateMaintenanceCostGap("project/1", {
      line_id: "line-free",
      version: 3,
      unit_cost_ex_tax: 0,
      evidence: "厂家免费更换确认单 FREE-001",
      reason: "有证据的免费更换",
    });

    expect(patch).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project%2F1/cost-gaps",
      {
        line_id: "line-free",
        version: 3,
        unit_cost_ex_tax: 0,
        evidence: "厂家免费更换确认单 FREE-001",
        reason: "有证据的免费更换",
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
    post.mockResolvedValueOnce({
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
            site_requisition_priced_cost_ex_tax: "265.58",
            site_requisition_priced_cost_inc_tax: "300.10",
            sales_estimate_cost_ex_tax: "88.50",
            sales_estimate_cost_inc_tax: "100.00",
            sales_estimate_lines: "2",
            cost_progress_includes_sales_estimate: true,
            cost_progress_label: "priced_cost_including_sales_estimate",
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
    expect(data.rows[0].metrics.site_requisition_priced_cost_ex_tax).toBe(265.58);
    expect(data.rows[0].metrics.site_requisition_priced_cost_inc_tax).toBe(300.1);
    expect(data.rows[0].metrics.sales_estimate_cost_ex_tax).toBe(88.5);
    expect(data.rows[0].metrics.sales_estimate_cost_inc_tax).toBe(100);
    expect(data.rows[0].metrics.sales_estimate_lines).toBe(2);
    expect(data.rows[0].metrics.cost_progress_includes_sales_estimate).toBe(true);
    expect(data.rows[0].metrics.cost_progress_label).toBe("priced_cost_including_sales_estimate");
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
            site_requisition_priced_cost_ex_tax: "29.47",
            site_requisition_priced_cost_inc_tax: "33.30",
            sales_estimate_cost_ex_tax: "8.41",
            sales_estimate_cost_inc_tax: "9.50",
            sales_estimate_lines: "1",
            cost_progress_includes_sales_estimate: true,
            cost_progress_label: "priced_cost_including_sales_estimate",
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
            cost_source: "sales_window",
            cost_evidence_kind: "sales_estimate",
            cost_is_estimate: true,
            cost_source_label: "估算（销售前后 7 天数量加权）",
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
            cost_source: "sales_window",
            cost_evidence_kind: "sales_estimate",
            cost_is_estimate: true,
            cost_source_label: "不应泄露的销售价格来源",
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
      cost_source: "sales_window",
      cost_evidence_kind: "sales_estimate",
      cost_is_estimate: true,
      cost_source_label: "估算（销售前后 7 天数量加权）",
    }));
    expect(data.requisitions.rows[1]).toEqual(expect.objectContaining({
      unit_cost: null,
      cost_amount: null,
      unit_cost_ex_tax: null,
      unit_cost_inc_tax: null,
      cost_amount_ex_tax: null,
      cost_amount_inc_tax: null,
      cost_source: null,
      cost_evidence_kind: null,
      cost_is_estimate: null,
      cost_source_label: null,
    }));
    expect(data.approved_expenses.rows[0].amount).toBe(8.8);
    expect(data.approved_expenses.rows[0].amount_ex_tax).toBe(7.79);
    expect(data.approved_expenses.rows[0].amount_inc_tax).toBe(8.8);
    expect(data.project.metrics.actual_project_cost_known_ex_tax).toBe(37.26);
    expect(data.project.metrics.actual_project_cost_known_inc_tax).toBe(42.1);
    expect(data.project.metrics.site_requisition_priced_cost_ex_tax).toBe(29.47);
    expect(data.project.metrics.site_requisition_priced_cost_inc_tax).toBe(33.3);
    expect(data.project.metrics.sales_estimate_cost_ex_tax).toBe(8.41);
    expect(data.project.metrics.sales_estimate_cost_inc_tax).toBe(9.5);
    expect(data.project.metrics.sales_estimate_lines).toBe(1);
    expect(data.project.metrics.cost_progress_includes_sales_estimate).toBe(true);
    expect(data.project.metrics.cost_progress_label).toBe("priced_cost_including_sales_estimate");
    expect(data.collection_snapshots.rows[0].cumulative_amount).toBe(18.8);
  });

  it("未知或缺失的合同额和成本进度税口径在 API 边界降级为 null", async () => {
    post.mockResolvedValueOnce({
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

  it("成本受限时目录和工作台都不会保留销售估算侧信道", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false,
      data_profit: false,
    }));
    const restrictedMetrics = {
      total_contract_amount: null,
      known_contract_amount: null,
      contract_amount_basis: "inc_tax",
      contract_amount_complete: null,
      received_amount: null,
      collection_progress_pct: null,
      site_requisition_known_cost: "999",
      site_requisition_known_cost_ex_tax: "999",
      site_requisition_known_cost_inc_tax: "999",
      site_requisition_priced_cost_ex_tax: "999",
      site_requisition_priced_cost_inc_tax: "999",
      sales_estimate_cost_ex_tax: "888",
      sales_estimate_cost_inc_tax: "999",
      sales_estimate_lines: "7",
      cost_progress_includes_sales_estimate: true,
      cost_progress_label: "priced_cost_including_sales_estimate",
      approved_expense: null,
      approved_expense_ex_tax: null,
      approved_expense_inc_tax: null,
      actual_project_cost_known: "999",
      actual_project_cost_known_ex_tax: "999",
      actual_project_cost_known_inc_tax: "999",
      cost_progress_basis: "inc_tax",
      cost_rate_lower_bound_pct: "99.9",
      cost_status: "yellow",
      cost_complete: null,
      missing_cost_lines: null,
    };
    const project = {
      project_id: "project-restricted",
      project_code: "XM-RESTRICTED",
      display_name: "成本受限项目",
      project_manager_id: null,
      lifecycle_status: "ongoing",
      is_active: true,
      version: 1,
      contracts: [],
      metrics: restrictedMetrics,
      reminder_count: 0,
      as_of: "2026-08-09",
    };
    post.mockResolvedValueOnce({
      data: {
        rows: [project], total: 1, page: 1, page_size: 24,
        as_of: "2026-08-09", data_version: "restricted-v1",
      },
    });
    get.mockResolvedValueOnce({
      data: {
        project,
        collection_snapshots: { rows: [], total: 0, page: 1, page_size: 20 },
        requisitions: {
          rows: [{
            line_id: "line-raw-cost",
            order_no: "WBDD-RAW-COST",
            order_date: "2026-08-01",
            contract_no: null,
            pn: "PN-RAW-COST",
            description: "服务端异常返回的成本字段",
            quantity: "1",
            unit_cost: "999",
            cost_amount: "999",
            unit_cost_ex_tax: "999",
            unit_cost_inc_tax: "999",
            cost_amount_ex_tax: "999",
            cost_amount_inc_tax: "999",
            cost_source: "sales_window",
            cost_evidence_kind: "sales_estimate",
            cost_is_estimate: true,
            cost_source_label: "不应保留的销售估算来源",
            cost_status: "available",
          }],
          total: 1,
          page: 1,
          page_size: 20,
        },
        approved_expenses: { rows: [], total: 0, page: 1, page_size: 20 },
        reminders: [],
        workbook_preview: {
          protocol_version: "2.0", sheets: [], latest_tracking_month: null,
          last_exported_at: null, data_version: "restricted-v1",
        },
        as_of: "2026-08-09",
        data_version: "restricted-v1",
      },
    });

    const directoryMetrics = (await listMaintenanceProjectOperations()).data.rows[0].metrics;
    const workspaceData = (await getMaintenanceProjectWorkspace("project-restricted")).data;
    const workspaceMetrics = workspaceData.project.metrics;
    for (const metrics of [directoryMetrics, workspaceMetrics]) {
      expect(metrics.site_requisition_priced_cost_ex_tax).toBeNull();
      expect(metrics.site_requisition_priced_cost_inc_tax).toBeNull();
      expect(metrics.sales_estimate_cost_ex_tax).toBeNull();
      expect(metrics.sales_estimate_cost_inc_tax).toBeNull();
      expect(metrics.sales_estimate_lines).toBeNull();
      expect(metrics.cost_progress_includes_sales_estimate).toBeNull();
      expect(metrics.cost_progress_label).toBeNull();
    }
    expect(workspaceData.requisitions.rows[0]).toEqual(expect.objectContaining({
      quantity: 1,
      unit_cost: null,
      cost_amount: null,
      cost_source: null,
      cost_evidence_kind: null,
      cost_is_estimate: null,
      cost_source_label: null,
      cost_status: "restricted",
    }));
  });

  it("仅成本权限不把费用完整度 null 误判为成本不可见", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true,
      data_profit: false,
    }));
    post.mockResolvedValueOnce({
      data: {
        rows: [{
          project_id: "project-cost-only",
          project_code: "XM-COST-ONLY",
          display_name: "仅成本权限项目",
          project_manager_id: null,
          lifecycle_status: "ongoing",
          is_active: true,
          version: 1,
          contracts: [],
          metrics: {
            total_contract_amount: null,
            known_contract_amount: null,
            contract_amount_basis: "inc_tax",
            contract_amount_complete: null,
            received_amount: null,
            collection_progress_pct: null,
            site_requisition_known_cost: "113.00",
            site_requisition_known_cost_ex_tax: "100.00",
            site_requisition_known_cost_inc_tax: "113.00",
            site_requisition_priced_cost_ex_tax: "100.00",
            site_requisition_priced_cost_inc_tax: "113.00",
            sales_estimate_cost_ex_tax: "25.00",
            sales_estimate_cost_inc_tax: "28.25",
            sales_estimate_lines: "2",
            cost_progress_includes_sales_estimate: true,
            cost_progress_label: "priced_cost_including_sales_estimate",
            approved_expense: null,
            approved_expense_ex_tax: null,
            approved_expense_inc_tax: null,
            actual_project_cost_known: null,
            actual_project_cost_known_ex_tax: null,
            actual_project_cost_known_inc_tax: null,
            cost_progress_basis: "inc_tax",
            cost_rate_lower_bound_pct: null,
            cost_status: null,
            cost_complete: null,
            missing_cost_lines: 1,
          },
          reminder_count: 1,
          as_of: "2026-08-09",
        }],
        total: 1,
        page: 1,
        page_size: 24,
        as_of: "2026-08-09",
        data_version: "cost-only-v1",
      },
    });

    const metrics = (await listMaintenanceProjectOperations()).data.rows[0].metrics;

    expect(metrics.site_requisition_known_cost_inc_tax).toBe(113);
    expect(metrics.site_requisition_priced_cost_ex_tax).toBe(100);
    expect(metrics.sales_estimate_cost_inc_tax).toBe(28.25);
    expect(metrics.sales_estimate_lines).toBe(2);
    expect(metrics.cost_progress_includes_sales_estimate).toBe(true);
    expect(metrics.cost_progress_label).toBe("priced_cost_including_sales_estimate");
    expect(metrics.missing_cost_lines).toBe(1);
    expect(metrics.approved_expense_inc_tax).toBeNull();
    expect(metrics.actual_project_cost_known_inc_tax).toBeNull();
  });

  it("仅成本权限在工作台保留现场领用取价证据并继续隐藏费用和回款", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true,
      data_profit: false,
    }));
    get.mockResolvedValueOnce({
      data: {
        project: {
          project_id: "project-cost-only",
          project_code: "XM-COST-ONLY",
          display_name: "仅成本权限项目",
          project_manager_id: null,
          lifecycle_status: "ongoing",
          is_active: true,
          version: 1,
          contracts: [],
          metrics: {
            total_contract_amount: null,
            known_contract_amount: null,
            contract_amount_basis: "inc_tax",
            contract_amount_complete: null,
            received_amount: null,
            collection_progress_pct: null,
            site_requisition_known_cost: "113.00",
            site_requisition_known_cost_ex_tax: "100.00",
            site_requisition_known_cost_inc_tax: "113.00",
            site_requisition_priced_cost_ex_tax: "100.00",
            site_requisition_priced_cost_inc_tax: "113.00",
            sales_estimate_cost_ex_tax: "25.00",
            sales_estimate_cost_inc_tax: "28.25",
            sales_estimate_lines: "2",
            cost_progress_includes_sales_estimate: true,
            cost_progress_label: "priced_cost_including_sales_estimate",
            approved_expense: "999.00",
            approved_expense_ex_tax: "999.00",
            approved_expense_inc_tax: "999.00",
            actual_project_cost_known: "1112.00",
            actual_project_cost_known_ex_tax: "1099.00",
            actual_project_cost_known_inc_tax: "1112.00",
            cost_progress_basis: "inc_tax",
            cost_rate_lower_bound_pct: "99.90",
            cost_status: "yellow",
            cost_complete: null,
            missing_cost_lines: 0,
          },
          reminder_count: 1,
          as_of: "2026-08-09",
        },
        collection_snapshots: {
          rows: [{
            collection_id: "collection-hidden",
            project_contract_id: "pc-hidden",
            contract_no: null,
            report_month: "2026-08-01",
            cumulative_amount: "999.00",
            receipt_reference: "RECEIPT-HIDDEN",
            status: "confirmed",
            remark: "不应保留的回款备注",
            version: 1,
          }],
          total: 1,
          page: 1,
          page_size: 20,
        },
        requisitions: {
          rows: [{
            line_id: "line-sales-estimate",
            order_no: "WBDD-COST-ONLY",
            order_date: "2026-08-01",
            contract_no: null,
            pn: "PN-COST-ONLY",
            description: "成本权限可见",
            quantity: "2",
            unit_cost: "56.50",
            cost_amount: "113.00",
            unit_cost_ex_tax: "50.00",
            unit_cost_inc_tax: "56.50",
            cost_amount_ex_tax: "100.00",
            cost_amount_inc_tax: "113.00",
            cost_source: "sales_window",
            cost_evidence_kind: "sales_estimate",
            cost_is_estimate: true,
            cost_source_label: "估算（销售前后 7 天数量加权）",
            cost_status: "available",
          }],
          total: 1,
          page: 1,
          page_size: 20,
        },
        approved_expenses: {
          rows: [{
            expense_id: "expense-hidden",
            expense_date: "2026-08-02",
            contract_no: null,
            amount: "999.00",
            amount_ex_tax: "999.00",
            amount_inc_tax: "999.00",
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
          data_version: "cost-only-v1",
        },
        as_of: "2026-08-09",
        data_version: "cost-only-v1",
      },
    });

    const { data } = await getMaintenanceProjectWorkspace("project-cost-only");

    expect(data.requisitions.rows[0]).toEqual(expect.objectContaining({
      quantity: 2,
      unit_cost_inc_tax: 56.5,
      cost_amount_inc_tax: 113,
      cost_source: "sales_window",
      cost_evidence_kind: "sales_estimate",
      cost_is_estimate: true,
      cost_source_label: "估算（销售前后 7 天数量加权）",
    }));
    expect(data.project.metrics.site_requisition_known_cost_inc_tax).toBe(113);
    expect(data.project.metrics.approved_expense).toBeNull();
    expect(data.project.metrics.approved_expense_inc_tax).toBeNull();
    expect(data.project.metrics.actual_project_cost_known_inc_tax).toBeNull();
    expect(data.project.metrics.cost_rate_lower_bound_pct).toBeNull();
    expect(data.project.metrics.cost_status).toBeNull();
    expect(data.collection_snapshots.rows[0]).toEqual(expect.objectContaining({
      cumulative_amount: null,
      receipt_reference: null,
      remark: null,
    }));
    expect(data.approved_expenses.rows).toEqual([]);
    expect(data.approved_expenses.total).toBe(0);
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
