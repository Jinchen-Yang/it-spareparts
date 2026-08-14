import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const {
  applyCollectionPlan,
  followUpCollectionMilestone,
  getCollectionMilestones,
  previewCollectionPlan,
  searchCollectionPlanBindingOptions,
  searchCollectionReminders,
} = vi.hoisted(() => ({
  applyCollectionPlan: vi.fn(),
  followUpCollectionMilestone: vi.fn(),
  getCollectionMilestones: vi.fn(),
  previewCollectionPlan: vi.fn(),
  searchCollectionPlanBindingOptions: vi.fn(),
  searchCollectionReminders: vi.fn(),
}));

vi.mock("../../../api/maintenanceCollectionReminders", async () => {
  const actual = await vi.importActual<
    typeof import("../../../api/maintenanceCollectionReminders")
  >("../../../api/maintenanceCollectionReminders");
  return {
    ...actual,
    applyCollectionPlan,
    followUpCollectionMilestone,
    getCollectionMilestones,
    previewCollectionPlan,
    searchCollectionPlanBindingOptions,
    searchCollectionReminders,
  };
});

import MaintenanceCollectionRemindersPage from "../MaintenanceCollectionRemindersPage";
import {
  COLLECTION_FOLLOW_UP,
  COLLECTION_PAGE,
} from "../../../components/maintenance/maintenanceLanguage";
import type {
  CollectionDirectoryRow,
  CollectionMilestoneRow,
  CollectionOwnerScope,
  CollectionProjectDetailResponse,
  CollectionProjectRef,
  CollectionReminderDirectoryResponse,
} from "../../../api/maintenanceCollectionReminders";

const makeProject = (id: string, code: string, name: string): CollectionProjectRef => ({
  project_id: id,
  project_code: code,
  display_name: name,
  lifecycle_status: "ongoing",
  version: 1,
  manager_assignment: { username: `m-${id}`, display_name: "负责人甲" },
  service_period: {
    service_start: "2026-01-01",
    service_end: "2026-12-31",
    completeness_state: "complete",
  },
  contracts: [
    {
      project_contract_id: `pc-${id}`,
      contract_no: `HT-${code}`,
      relation_status: "current",
      lifecycle_status: "effective",
      version: 1,
    },
  ],
});

const makeDirectoryRow = (
  id: string,
  code: string,
  name: string,
  countsOverrides: Partial<CollectionDirectoryRow["reminder_counts"]> = {},
): CollectionDirectoryRow => ({
  project: makeProject(id, code, name),
  reminder_counts: {
    total: 1,
    needs_review: 0,
    handled: 0,
    incomplete: 0,
    overdue: 0,
    due_this_month: 1,
    upcoming: 0,
    ...countsOverrides,
  },
  next_actionable_milestone: {
    milestone_id: `m-${id}`,
    project_contract_id: `pc-${id}`,
    contract_no: `HT-${code}`,
    sequence: 1,
    planned_month: "2026-08",
    planned_amount: "1234.50",
    reminder_state: "due_this_month",
    version: 1,
  },
});

const makeDirectory = (
  rows: CollectionDirectoryRow[],
  overrides: Partial<CollectionReminderDirectoryResponse> = {},
): CollectionReminderDirectoryResponse => ({
  rows,
  total: rows.length,
  page: 1,
  page_size: 24,
  owner_scope: "me",
  allowed_owner_scopes: ["me"],
  as_of: "2026-08-14",
  data_version: "v1",
  amount_visibility: "visible",
  ...overrides,
});

const makeMilestone = (
  id: string,
  overrides: Partial<CollectionMilestoneRow> = {},
): CollectionMilestoneRow => ({
  milestone_id: id,
  project_contract_id: "pc-project-a",
  contract_no: "HT-XM-001",
  sequence: 1,
  planned_date: "2026-08-01",
  date_precision: "month",
  planned_month: "2026-08",
  planned_amount: "1234.50",
  completeness_state: "complete",
  follow_up_status: "pending",
  reminder_state: "due_this_month",
  follow_up_review_required: false,
  followed_up_by: null,
  followed_up_at: null,
  follow_up_note: null,
  last_operation: null,
  version: 1,
  ...overrides,
});

const makeDetail = (
  rows: CollectionMilestoneRow[],
  overrides: Partial<CollectionProjectDetailResponse> = {},
): CollectionProjectDetailResponse => ({
  project: makeProject("project-a", "XM-001", "一号项目"),
  summary: {
    total: rows.length,
    needs_review: rows.filter((r) => r.reminder_state === "needs_review").length,
    handled: rows.filter((r) => r.reminder_state === "handled").length,
    incomplete: rows.filter((r) => r.reminder_state === "incomplete").length,
    overdue: rows.filter((r) => r.reminder_state === "overdue").length,
    due_this_month: rows.filter((r) => r.reminder_state === "due_this_month").length,
    upcoming: rows.filter((r) => r.reminder_state === "upcoming").length,
  },
  rows,
  as_of: "2026-08-14",
  data_version: "v1",
  amount_visibility: "visible",
  ...overrides,
});

const rowA = makeDirectoryRow("project-a", "XM-001", "一号项目");
const rowB = makeDirectoryRow("project-b", "XM-002", "二号项目");
const rowC = makeDirectoryRow("project-c", "XM-003", "三号项目");
const rowD = makeDirectoryRow("project-d", "XM-004", "四号项目");

function stubViewport(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  const media = vi.fn((query: string) => ({
    matches: query.includes("min-width: 1200px")
      ? width >= 1200
      : query.includes("min-width: 768px")
        ? width >= 768
        : false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: () => false,
  }));
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: media });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <MaintenanceCollectionRemindersPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true,
    page_maintenance_beta: true,
    action_maintenance_collection_follow_up: true,
  }));
  stubViewport(1440);
  searchCollectionReminders.mockResolvedValue({ data: makeDirectory([rowA, rowB]) });
  getCollectionMilestones.mockResolvedValue({
    data: makeDetail([makeMilestone("m-1")]),
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceCollectionRemindersPage", () => {
  it("首次加载自动选中第一页第一项并加载其详情", async () => {
    renderPage();
    await waitFor(() => expect(searchCollectionReminders).toHaveBeenCalledWith(
      { q: "", owner_scope: "me", reminder_state: null, page: 1, page_size: 24 },
      { signal: expect.any(AbortSignal) },
    ));
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-a",
      { signal: expect.any(AbortSignal) },
    ));
    expect(screen.getByTestId("mcr-row-project-a")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("mcr-row-project-b")).not.toHaveAttribute("aria-current");
    expect(
      within(screen.getByTestId("mcr-detail-pane")).getByText("一号项目"),
    ).toBeInTheDocument();
  });

  it("空列表清空右侧详情且不请求详情", async () => {
    searchCollectionReminders.mockResolvedValue({ data: makeDirectory([]) });
    renderPage();
    expect(
      await screen.findByText(COLLECTION_PAGE.emptyDirectory),
    ).toBeInTheDocument();
    await waitFor(() => expect(getCollectionMilestones).not.toHaveBeenCalled());
    expect(
      within(screen.getByTestId("mcr-detail-pane")).getByText(COLLECTION_PAGE.emptyDetail),
    ).toBeInTheDocument();
  });

  it("搜索使当前项目离开结果集时选择新首项，旧列表响应不覆盖新结果", async () => {
    let resolveFirst!: (value: { data: CollectionReminderDirectoryResponse }) => void;
    searchCollectionReminders
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ data: makeDirectory([rowB]) });
    renderPage();
    await waitFor(() => expect(searchCollectionReminders).toHaveBeenCalledTimes(1));

    const input = screen.getByLabelText(COLLECTION_PAGE.searchLabel);
    fireEvent.change(input, { target: { value: "XM-002" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(searchCollectionReminders).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-b",
      { signal: expect.any(AbortSignal) },
    ));
    expect(screen.getByTestId("mcr-row-project-b")).toHaveAttribute("aria-current", "true");

    // 旧列表慢响应到达：必须被 generation/abort 拦截
    resolveFirst({ data: makeDirectory([rowA]) });
    await waitFor(() => {
      expect(screen.getByTestId("mcr-row-project-b")).toHaveAttribute("aria-current", "true");
    });
    expect(screen.queryByTestId("mcr-row-project-a")).toBeNull();
  });

  it("状态筛选后选择新首项", async () => {
    searchCollectionReminders
      .mockResolvedValueOnce({ data: makeDirectory([rowA, rowB]) })
      .mockResolvedValueOnce({ data: makeDirectory([rowB]) });
    renderPage();
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-a",
      { signal: expect.any(AbortSignal) },
    ));
    fireEvent.mouseDown(screen.getByLabelText(COLLECTION_PAGE.stateFilterLabel));
    const option = await screen.findByRole("option", { name: "已逾期" });
    fireEvent.click(option);
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-b",
      { signal: expect.any(AbortSignal) },
    ));
    expect(searchCollectionReminders).toHaveBeenLastCalledWith(
      { q: "", owner_scope: "me", reminder_state: "overdue", page: 1, page_size: 24 },
      { signal: expect.any(AbortSignal) },
    );
    expect(screen.getByTestId("mcr-row-project-b")).toHaveAttribute("aria-current", "true");
  });

  it("翻页后当前项离开结果集时选择新页首项", async () => {
    searchCollectionReminders.mockImplementation((body: { page?: number }) => Promise.resolve({
      data: makeDirectory(
        body.page === 1 ? [rowA, rowB] : [rowC, rowD],
        { total: 48, page: body.page ?? 1 },
      ),
    }));
    renderPage();
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-a",
      { signal: expect.any(AbortSignal) },
    ));
    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-c",
      { signal: expect.any(AbortSignal) },
    ));
    expect(screen.getByTestId("mcr-row-project-c")).toHaveAttribute("aria-current", "true");
    expect(screen.queryByTestId("mcr-row-project-a")).toBeNull();
  });

  it("切换项目时旧详情慢响应不能覆盖当前选择", async () => {
    let resolveA!: (value: { data: CollectionProjectDetailResponse }) => void;
    getCollectionMilestones.mockImplementation((projectId: string) => {
      if (projectId === "project-a") {
        return new Promise((resolve) => { resolveA = resolve; });
      }
      return Promise.resolve({
        data: makeDetail([], { project: makeProject("project-b", "XM-002", "二号项目") }),
      });
    });
    renderPage();
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-a",
      { signal: expect.any(AbortSignal) },
    ));
    fireEvent.click(screen.getByTestId("mcr-row-project-b"));
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-b",
      { signal: expect.any(AbortSignal) },
    ));
    expect(
      within(screen.getByTestId("mcr-detail-pane")).getByText("二号项目"),
    ).toBeInTheDocument();

    resolveA({ data: makeDetail([makeMilestone("m-1")], { project: makeProject("project-a", "XM-001", "一号项目") }) });
    await waitFor(() => {
      expect(
        within(screen.getByTestId("mcr-detail-pane")).queryByText("一号项目"),
      ).toBeNull();
    });
    expect(
      within(screen.getByTestId("mcr-detail-pane")).getByText("二号项目"),
    ).toBeInTheDocument();
  });

  it("全部项目控件由 allowed_owner_scopes 决定，不按角色推断", async () => {
    localStorage.setItem("role", "admin");
    searchCollectionReminders.mockResolvedValue({
      data: makeDirectory([rowA], { allowed_owner_scopes: ["me"] }),
    });
    renderPage();
    await screen.findByText("一号项目");
    expect(screen.queryByText(COLLECTION_PAGE.ownerScopeAll)).toBeNull();

    cleanup();
    searchCollectionReminders.mockResolvedValue({
      data: makeDirectory([rowA], { allowed_owner_scopes: ["me", "all"] }),
    });
    renderPage();
    await screen.findByText("一号项目");
    fireEvent.mouseDown(screen.getByLabelText(COLLECTION_PAGE.ownerScopeLabel));
    const option = await screen.findByRole("option", { name: COLLECTION_PAGE.ownerScopeAll });
    fireEvent.click(option);
    await waitFor(() => expect(searchCollectionReminders).toHaveBeenLastCalledWith(
      { q: "", owner_scope: "all", reminder_state: null, page: 1, page_size: 24 },
      { signal: expect.any(AbortSignal) },
    ));
  });

  it("受限金额显示无权限查看而不是 0", async () => {
    getCollectionMilestones.mockResolvedValue({
      data: makeDetail(
        [makeMilestone("m-1", { planned_amount: null })],
        { amount_visibility: "restricted" },
      ),
    });
    renderPage();
    const detailPane = screen.getByTestId("mcr-detail-pane");
    await waitFor(() => expect(
      within(detailPane).getAllByText(COLLECTION_PAGE.amountRestricted).length,
    ).toBeGreaterThan(0));
    expect(within(detailPane).queryByText("¥0")).toBeNull();
  });

  it("incomplete 无操作；needs_review 只可重新打开；待处理完整行有标记/改期；handled 可重新打开", async () => {
    getCollectionMilestones.mockResolvedValue({
      data: makeDetail([
        makeMilestone("m-overdue", {
          contract_no: "HT-A-1", sequence: 1, planned_month: "2026-06", reminder_state: "overdue",
        }),
        makeMilestone("m-review", {
          contract_no: "HT-A-2", sequence: 2, planned_month: "2026-07",
          reminder_state: "needs_review", follow_up_status: "handled", follow_up_review_required: true,
        }),
        makeMilestone("m-handled", {
          contract_no: "HT-A-3", sequence: 3, planned_month: "2026-08",
          reminder_state: "handled", follow_up_status: "handled",
        }),
        makeMilestone("m-incomplete", {
          contract_no: "HT-A-4", sequence: 4, planned_month: null, planned_amount: null,
          reminder_state: "incomplete", completeness_state: "incomplete",
        }),
      ]),
    });
    renderPage();
    const detailPane = screen.getByTestId("mcr-detail-pane");
    await waitFor(() => expect(
      within(detailPane).getByText("2026-06"),
    ).toBeInTheDocument());

    const overdueRow = within(detailPane).getByText("2026-06").closest("tr") as HTMLElement;
    expect(within(overdueRow).getByRole("button", { name: COLLECTION_PAGE.actionHandle })).toBeInTheDocument();
    expect(within(overdueRow).getByRole("button", { name: COLLECTION_PAGE.actionReschedule })).toBeInTheDocument();
    expect(within(overdueRow).queryByRole("button", { name: COLLECTION_PAGE.actionReopen })).toBeNull();

    const reviewRow = within(detailPane).getByText("2026-07").closest("tr") as HTMLElement;
    expect(within(reviewRow).getByRole("button", { name: COLLECTION_PAGE.actionReopen })).toBeInTheDocument();
    expect(within(reviewRow).queryByRole("button", { name: COLLECTION_PAGE.actionHandle })).toBeNull();
    expect(within(reviewRow).queryByRole("button", { name: COLLECTION_PAGE.actionReschedule })).toBeNull();
    expect(within(reviewRow).getByText(COLLECTION_PAGE.needsReviewHint)).toBeInTheDocument();

    const handledRow = within(detailPane).getByText("2026-08").closest("tr") as HTMLElement;
    expect(within(handledRow).getByRole("button", { name: COLLECTION_PAGE.actionReopen })).toBeInTheDocument();
    expect(within(handledRow).queryByRole("button", { name: COLLECTION_PAGE.actionHandle })).toBeNull();

    const incompleteRow = within(detailPane).getByText("HT-A-4").closest("tr") as HTMLElement;
    expect(within(incompleteRow).queryByRole("button", { name: COLLECTION_PAGE.actionHandle })).toBeNull();
    expect(within(incompleteRow).queryByRole("button", { name: COLLECTION_PAGE.actionReschedule })).toBeNull();
    expect(within(incompleteRow).queryByRole("button", { name: COLLECTION_PAGE.actionReopen })).toBeNull();
    expect(within(incompleteRow).getByText(COLLECTION_PAGE.incompleteHint)).toBeInTheDocument();
  });

  it("无写权限时不渲染行操作按钮", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
      action_maintenance_collection_follow_up: false,
    }));
    renderPage();
    const detailPane = screen.getByTestId("mcr-detail-pane");
    await waitFor(() => expect(
      within(detailPane).getByText("2026-08"),
    ).toBeInTheDocument());
    expect(within(detailPane).queryByRole("button", { name: COLLECTION_PAGE.actionHandle })).toBeNull();
    expect(within(detailPane).queryByRole("button", { name: COLLECTION_PAGE.actionReschedule })).toBeNull();
    expect(within(detailPane).queryByRole("button", { name: COLLECTION_PAGE.actionReopen })).toBeNull();
  });

  it("写成功后重新请求列表和详情，不做乐观计数猜测", async () => {
    let detailCalls = 0;
    getCollectionMilestones.mockImplementation(() => {
      detailCalls += 1;
      return Promise.resolve({
        data: detailCalls === 1
          ? makeDetail([makeMilestone("m-1", { contract_no: "HT-XM-001", planned_month: "2026-08" })])
          : makeDetail([makeMilestone("m-1", {
            contract_no: "HT-XM-001",
            planned_month: "2026-08",
            reminder_state: "handled",
            follow_up_status: "handled",
            followed_up_by: "负责人甲",
            followed_up_at: "2026-08-14T10:00:00Z",
            follow_up_note: "已电话跟进",
          })]),
      });
    });
    followUpCollectionMilestone.mockResolvedValue({
      data: {
        row: makeMilestone("m-1", { reminder_state: "handled", follow_up_status: "handled" }),
        data_version: "v2",
        idempotent_replay: false,
      },
    });
    renderPage();
    const detailPane = screen.getByTestId("mcr-detail-pane");
    const handleButton = await within(detailPane).findByRole("button", {
      name: COLLECTION_PAGE.actionHandle,
    });
    fireEvent.click(handleButton);
    await screen.findByText(COLLECTION_FOLLOW_UP.handleTitle);
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_FOLLOW_UP.submit }));

    await waitFor(() => expect(followUpCollectionMilestone).toHaveBeenCalledTimes(1));
    const body = followUpCollectionMilestone.mock.calls[0][1];
    expect(body).toMatchObject({ expected_version: 1, action: "handle" });
    expect(body).not.toHaveProperty("planned_month");
    expect(body).not.toHaveProperty("reason");
    await waitFor(() => expect(searchCollectionReminders).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledTimes(2));
    expect(await within(detailPane).findByRole("button", {
      name: COLLECTION_PAGE.actionReopen,
    })).toBeInTheDocument();
  });

  it("403 显示权限提示且保留筛选上下文", async () => {
    const forbidden = Object.assign(new Error("forbidden"), {
      response: { status: 403, data: { detail: { code: "permission_denied", message: "无权执行此操作" } } },
    });
    searchCollectionReminders.mockResolvedValueOnce({ data: makeDirectory([rowA]) })
      .mockRejectedValueOnce(forbidden);
    renderPage();
    await screen.findByText("一号项目");
    const input = screen.getByLabelText(COLLECTION_PAGE.searchLabel);
    fireEvent.change(input, { target: { value: "XM-001" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByText(COLLECTION_PAGE.permissionDenied)).toBeInTheDocument();
    expect(input).toHaveValue("XM-001");
  });

  it("409 提示刷新且保留筛选上下文", async () => {
    const conflict = Object.assign(new Error("conflict"), {
      response: { status: 409, data: { detail: { code: "version_conflict", message: "数据已变化，请刷新后重试" } } },
    });
    searchCollectionReminders.mockResolvedValueOnce({ data: makeDirectory([rowA]) })
      .mockRejectedValueOnce(conflict);
    renderPage();
    await screen.findByText("一号项目");
    const input = screen.getByLabelText(COLLECTION_PAGE.searchLabel);
    fireEvent.change(input, { target: { value: "XM-001" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByText(COLLECTION_PAGE.versionConflict)).toBeInTheDocument();
    expect(input).toHaveValue("XM-001");
    expect(screen.getByRole("button", { name: COLLECTION_PAGE.retry })).toBeInTheDocument();
  });

  it("500 显示加载失败并可重试", async () => {
    const failure = Object.assign(new Error("boom"), { response: { status: 500 } });
    searchCollectionReminders.mockResolvedValueOnce({ data: makeDirectory([rowA]) })
      .mockRejectedValueOnce(failure);
    renderPage();
    await screen.findByText("一号项目");
    const input = screen.getByLabelText(COLLECTION_PAGE.searchLabel);
    fireEvent.change(input, { target: { value: "XM-001" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByText(COLLECTION_PAGE.loadFailed)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: COLLECTION_PAGE.retry }));
    await waitFor(() => expect(searchCollectionReminders).toHaveBeenCalledTimes(3));
  });

  it("详情加载失败时左侧列表仍可用", async () => {
    const failure = Object.assign(new Error("boom"), { response: { status: 500 } });
    getCollectionMilestones.mockRejectedValueOnce(failure);
    renderPage();
    expect(await screen.findByText(COLLECTION_PAGE.detailLoadFailed)).toBeInTheDocument();
    expect(screen.getByTestId("mcr-row-project-a")).toBeInTheDocument();
    expect(screen.getByTestId("mcr-row-project-b")).toBeInTheDocument();
  });

  it("小于 768px 使用全高 MobileDetailDrawer，桌面显示双栏", async () => {
    stubViewport(390);
    renderPage();
    await waitFor(() => expect(getCollectionMilestones).toHaveBeenCalledWith(
      "project-a",
      { signal: expect.any(AbortSignal) },
    ));
    expect(screen.queryByTestId("mcr-master-detail")).toBeNull();
    fireEvent.click(screen.getByTestId("mcr-row-project-a"));
    const drawer = await screen.findByRole("dialog");
    expect(drawer).toBeInTheDocument();
    expect(
      document.querySelector(".ant-drawer-content-wrapper"),
    ).toHaveStyle({ height: "100%" });
    expect(within(drawer).getByText("一号项目")).toBeInTheDocument();
    expect(within(drawer).getByText("2026-08")).toBeInTheDocument();
  });

  it("768 与 1024 使用 42/58 分栏，1440 使用 38/62", async () => {
    for (const width of [768, 1024]) {
      stubViewport(width);
      renderPage();
      const grid = await screen.findByTestId("mcr-master-detail");
      expect(grid).toHaveStyle({
        gridTemplateColumns: "minmax(0, 42fr) minmax(0, 58fr)",
      });
      cleanup();
    }
    stubViewport(1440);
    renderPage();
    const grid = await screen.findByTestId("mcr-master-detail");
    expect(grid).toHaveStyle({
      gridTemplateColumns: "minmax(0, 38fr) minmax(0, 62fr)",
    });
  });

  it("390/768/1024/1440 宽度下页面无页面级横向滚动", async () => {
    for (const width of [390, 768, 1024, 1440]) {
      stubViewport(width);
      renderPage();
      const root = await screen.findByTestId("collection-reminders-page");
      expect(root).toHaveStyle({ maxWidth: "100%", overflowX: "hidden" });
      cleanup();
    }
  });

  it("固定免责声明存在，页面无其他禁用到账文案", async () => {
    renderPage();
    await screen.findByText("一号项目");
    const disclaimer = screen.getByText(COLLECTION_PAGE.disclaimer);
    expect(disclaimer).toBeInTheDocument();
    const forbidden = ["已到账", "实收", "待收", "回款率", "到账率", "凭证", "核销"];
    const offenders: string[] = [];
    for (const el of Array.from(document.body.querySelectorAll("*"))) {
      const directText = Array.from(el.childNodes)
        .filter((node) => node.nodeType === Node.TEXT_NODE)
        .map((node) => node.textContent ?? "")
        .join("");
      if (!directText.trim()) continue;
      if (directText.trim() === COLLECTION_PAGE.disclaimer) continue;
      if (forbidden.some((term) => directText.includes(term))) {
        offenders.push(directText.trim());
      }
    }
    expect(offenders).toEqual([]);
  });
});
