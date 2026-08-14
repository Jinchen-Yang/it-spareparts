import { beforeEach, describe, expect, it, vi } from "vitest";

const { post, get } = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));

vi.mock("../../api", () => ({ api: { post, get } }));

import {
  applyCollectionPlan,
  downloadCollectionPlanSourceFile,
  followUpCollectionMilestone,
  formatDecimalAmount,
  getCollectionMilestones,
  previewCollectionPlan,
  searchCollectionPlanBindingOptions,
  searchCollectionReminders,
} from "../maintenanceCollectionReminders";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";

beforeEach(() => vi.clearAllMocks());

describe("collection reminders API contract", () => {
  it("searches the reminder directory with POST body defaults and never /api/api paths", () => {
    searchCollectionReminders({});
    expect(post).toHaveBeenCalledWith(
      "/maintenance/collection-reminders/search",
      { q: "", owner_scope: "me", reminder_state: null, page: 1, page_size: 24 },
    );
    expect(post.mock.calls[0][0]).not.toMatch(/^\/api/);
  });

  it("sends trimmed search terms, scope, state filter and pagination", () => {
    searchCollectionReminders(
      {
        q: "  XM-001  ",
        owner_scope: "all",
        reminder_state: "overdue",
        page: 2,
        page_size: 50,
      },
      { signal: new AbortController().signal },
    );
    expect(post).toHaveBeenCalledWith(
      "/maintenance/collection-reminders/search",
      { q: "XM-001", owner_scope: "all", reminder_state: "overdue", page: 2, page_size: 50 },
      { signal: expect.any(AbortSignal) },
    );
  });

  it("loads one project detail and forwards the abort signal", () => {
    getCollectionMilestones("project-1", { signal: new AbortController().signal });
    expect(get).toHaveBeenCalledWith(
      "/maintenance/projects/stable/project-1/collection-milestones",
      { signal: expect.any(AbortSignal) },
    );
  });

  it("keeps planned_amount as a decimal string (or null) without number conversion", async () => {
    get.mockResolvedValueOnce({
      data: {
        project: {
          project_id: "project-1",
          project_code: "XM-001",
          display_name: "一号项目",
          lifecycle_status: "ongoing",
          version: 3,
          manager_assignment: { username: "m1", display_name: "负责人甲" },
          service_period: {
            service_start: "2026-01-01",
            service_end: "2026-12-31",
            completeness_state: "complete",
          },
          contracts: [],
        },
        summary: {
          total: 1, needs_review: 0, handled: 0, incomplete: 0,
          overdue: 0, due_this_month: 1, upcoming: 0,
        },
        rows: [{
          milestone_id: "milestone-1",
          project_contract_id: "pc-1",
          contract_no: "XSDD-001",
          sequence: 1,
          planned_date: "2026-08-01",
          date_precision: "month",
          planned_month: "2026-08",
          planned_amount: "123.45",
          completeness_state: "complete",
          follow_up_status: "pending",
          reminder_state: "due_this_month",
          follow_up_review_required: false,
          followed_up_by: null,
          followed_up_at: null,
          follow_up_note: null,
          last_operation: null,
          version: 1,
        }],
        as_of: "2026-08-14",
        data_version: "v1",
        amount_visibility: "visible",
      },
    });
    const { data } = await getCollectionMilestones("project-1");
    expect(data.rows[0].planned_amount).toBe("123.45");
    expect(data.amount_visibility).toBe("visible");
  });

  it("keeps a restricted amount as null", async () => {
    get.mockResolvedValueOnce({
      data: {
        project: {
          project_id: "project-1",
          project_code: "XM-001",
          display_name: "一号项目",
          lifecycle_status: "ongoing",
          version: 3,
          manager_assignment: { username: "m1", display_name: "负责人甲" },
          service_period: {
            service_start: "2026-01-01",
            service_end: "2026-12-31",
            completeness_state: "complete",
          },
          contracts: [],
        },
        summary: {
          total: 1, needs_review: 0, handled: 0, incomplete: 0,
          overdue: 0, due_this_month: 1, upcoming: 0,
        },
        rows: [{
          milestone_id: "milestone-1",
          project_contract_id: "pc-1",
          contract_no: "XSDD-001",
          sequence: 1,
          planned_date: "2026-08-01",
          date_precision: "month",
          planned_month: "2026-08",
          planned_amount: null,
          completeness_state: "complete",
          follow_up_status: "pending",
          reminder_state: "due_this_month",
          follow_up_review_required: false,
          followed_up_by: null,
          followed_up_at: null,
          follow_up_note: null,
          last_operation: null,
          version: 1,
        }],
        as_of: "2026-08-14",
        data_version: "v1",
        amount_visibility: "restricted",
      },
    });
    const { data } = await getCollectionMilestones("project-1");
    expect(data.rows[0].planned_amount).toBeNull();
    expect(data.amount_visibility).toBe("restricted");
  });

  it("sends only the note for a handle follow-up", () => {
    followUpCollectionMilestone("milestone-1", {
      expected_version: 2,
      idempotency_key: "handle-key-0001",
      action: "handle",
      note: "已电话跟进",
    });
    expect(post).toHaveBeenCalledWith(
      "/maintenance/collection-milestones/milestone-1/follow-ups",
      {
        expected_version: 2,
        idempotency_key: "handle-key-0001",
        action: "handle",
        note: "已电话跟进",
      },
    );
  });

  it("sends planned_month and reason (no note) for a reschedule", () => {
    followUpCollectionMilestone("milestone-1", {
      expected_version: 3,
      idempotency_key: "reschedule-key-01",
      action: "reschedule",
      planned_month: "2026-10",
      reason: "客户变更验收时间",
    });
    expect(post).toHaveBeenCalledWith(
      "/maintenance/collection-milestones/milestone-1/follow-ups",
      {
        expected_version: 3,
        idempotency_key: "reschedule-key-01",
        action: "reschedule",
        planned_month: "2026-10",
        reason: "客户变更验收时间",
      },
    );
  });

  it("sends only the reason for a reopen", () => {
    followUpCollectionMilestone("milestone-1", {
      expected_version: 4,
      idempotency_key: "reopen-key-0001",
      action: "reopen",
      reason: "误处理，重新进入提醒队列",
    });
    expect(post).toHaveBeenCalledWith(
      "/maintenance/collection-milestones/milestone-1/follow-ups",
      {
        expected_version: 4,
        idempotency_key: "reopen-key-0001",
        action: "reopen",
        reason: "误处理，重新进入提醒队列",
      },
    );
  });

  it("propagates 409 version conflicts without swallowing", async () => {
    const conflict = Object.assign(new Error("conflict"), {
      response: {
        status: 409,
        data: {
          detail: {
            code: "version_conflict",
            message: "数据已变化，请刷新后重试",
            current_version: 2,
            current_data_version: null,
            issues: [],
          },
        },
      },
    });
    post.mockRejectedValueOnce(conflict);
    await expect(followUpCollectionMilestone("milestone-1", {
      expected_version: 1,
      idempotency_key: "stale-key-0001",
      action: "handle",
    })).rejects.toMatchObject({ response: { status: 409 } });
  });

  it("uploads a .xls file for preview with the Idempotency-Key header", () => {
    const file = new File(["xls-bytes"], "回款计划.xls", {
      type: "application/vnd.ms-excel",
    });
    previewCollectionPlan(file, "preview-key-0001");
    const [path, form, config] = post.mock.calls[0] as unknown as [
      string, FormData, { headers: Record<string, string> },
    ];
    expect(path).toBe("/maintenance/collection-plan-imports/preview");
    expect(form).toBeInstanceOf(FormData);
    expect(form.get("file")).toBe(file);
    expect(file.name).toMatch(/\.xls$/);
    expect(config.headers["Idempotency-Key"]).toBe("preview-key-0001");
  });

  it("searches binding options with query params and an abort signal", () => {
    searchCollectionPlanBindingOptions(
      "batch-1",
      { q: "XM", page: 1, page_size: 20 },
      { signal: new AbortController().signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/maintenance/collection-plan-imports/batch-1/binding-options",
      { params: { q: "XM", page: 1, page_size: 20 }, signal: expect.any(AbortSignal) },
    );
  });

  it("applies with versions and the reviewed bindings discriminator", () => {
    applyCollectionPlan("batch-1", {
      expected_batch_version: 2,
      expected_data_version: "dv-2",
      bindings: [
        {
          row_key: "row-1",
          external_order_no: "ORDER-001",
          project_id: "project-1",
          project_version: 3,
          project_contract_id: "pc-1",
          project_contract_version: 1,
          existing_binding_version: null,
          reason: null,
        },
        {
          row_key: "row-2",
          external_order_no: "ORDER-002",
          project_id: "project-2",
          project_version: 2,
          project_contract_id: "pc-2",
          project_contract_version: 1,
          existing_binding_version: 4,
          reason: "合同改派，原合同已终止",
        },
      ],
    });
    expect(post).toHaveBeenCalledWith(
      "/maintenance/collection-plan-imports/batch-1/apply",
      {
        expected_batch_version: 2,
        expected_data_version: "dv-2",
        bindings: [
          {
            row_key: "row-1",
            external_order_no: "ORDER-001",
            project_id: "project-1",
            project_version: 3,
            project_contract_id: "pc-1",
            project_contract_version: 1,
            existing_binding_version: null,
            reason: null,
          },
          {
            row_key: "row-2",
            external_order_no: "ORDER-002",
            project_id: "project-2",
            project_version: 2,
            project_contract_id: "pc-2",
            project_contract_version: 1,
            existing_binding_version: 4,
            reason: "合同改派，原合同已终止",
          },
        ],
      },
    );
  });

  it("downloads the source file as an attachment blob", () => {
    downloadCollectionPlanSourceFile("batch-1");
    expect(get).toHaveBeenCalledWith(
      "/maintenance/collection-plan-imports/batch-1/source-file",
      { responseType: "blob" },
    );
  });
});

describe("formatDecimalAmount", () => {
  it("formats decimal strings for display without float math", () => {
    expect(formatDecimalAmount("0")).toBe("0");
    expect(formatDecimalAmount("1234.5")).toBe("1,234.5");
    expect(formatDecimalAmount("1234567.89")).toBe("1,234,567.89");
    expect(formatDecimalAmount("9007199254740993.12")).toBe("9,007,199,254,740,993.12");
    expect(formatDecimalAmount(" 12.30 ")).toBe("12.30");
  });

  it("returns null for missing, empty or invalid amounts", () => {
    expect(formatDecimalAmount(null)).toBeNull();
    expect(formatDecimalAmount("")).toBeNull();
    expect(formatDecimalAmount("abc")).toBeNull();
    expect(formatDecimalAmount("1.2.3")).toBeNull();
    expect(formatDecimalAmount("12a")).toBeNull();
  });
});

describe("collection reminder capabilities", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("page entry needs the maintenance Beta combination", () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
    }));
    expect(readMaintenanceCapabilities().canViewCollectionReminders).toBe(true);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: false,
      page_maintenance_beta: true,
    }));
    expect(readMaintenanceCapabilities().canViewCollectionReminders).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: false,
    }));
    expect(readMaintenanceCapabilities().canViewCollectionReminders).toBe(false);
  });

  it("follow-up requires the explicit action with no admin short-circuit", () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
      action_maintenance_collection_follow_up: false,
    }));
    expect(readMaintenanceCapabilities().canFollowUpCollection).toBe(false);

    // Admin without the explicit action still cannot follow up.
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
    }));
    expect(readMaintenanceCapabilities().canFollowUpCollection).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
      action_maintenance_collection_follow_up: true,
    }));
    expect(readMaintenanceCapabilities().canFollowUpCollection).toBe(true);
  });

  it("import requires Beta, realm role=admin, explicit action and contract visibility", () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({}));
    expect(readMaintenanceCapabilities().canImportCollectionPlan).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      action_maintenance_collection_plan_import: true,
    }));
    expect(readMaintenanceCapabilities().canImportCollectionPlan).toBe(true);

    // Non-admin with the explicit action but no data_profit cannot import.
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
      action_maintenance_collection_plan_import: true,
    }));
    expect(readMaintenanceCapabilities().canImportCollectionPlan).toBe(false);

    // Full contract visibility is still not enough without the realm admin role.
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_collection_plan_import: true,
    }));
    expect(readMaintenanceCapabilities().canImportCollectionPlan).toBe(false);

    // Admin with data_profit and the explicit action can import.
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_collection_plan_import: true,
    }));
    expect(readMaintenanceCapabilities().canImportCollectionPlan).toBe(true);
  });
});
