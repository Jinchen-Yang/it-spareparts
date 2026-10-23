import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, renderWithoutMotion as render } from "../../../test/antdRender";
import { MAINTENANCE_CHANGED_STORAGE_KEY } from "../../../utils/maintenanceRefresh";
import type { WorkbookApplyResult, WorkbookValidateResult } from "../../../api/maintenanceWorkbooks";

const api = vi.hoisted(() => ({
  project: vi.fn(), board: vi.fn(), workspace: vi.fn(), expense: vi.fn(),
  download: vi.fn(), validate: vi.fn(), apply: vi.fn(), saveBlob: vi.fn(),
}));
vi.mock("../../../api/maintenanceProjects", async () => ({
  ...await vi.importActual("../../../api/maintenanceProjects"),
  getMaintenanceProject: api.project,
}));
vi.mock("../../../api/maintenanceBossBoard", async () => ({
  ...await vi.importActual("../../../api/maintenanceBossBoard"),
  getBoardProject: api.board,
}));
vi.mock("../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual("../../../api/maintenanceOperations"),
  getMaintenanceProjectWorkspace: api.workspace,
}));
vi.mock("../../../api/maintenanceWorkbooks", async () => ({
  ...await vi.importActual("../../../api/maintenanceWorkbooks"),
  listProjectExpenseRows: api.expense,
  downloadProjectMaster: api.download,
  validateProjectMaster: api.validate,
  applyProjectMaster: api.apply,
  saveBlob: api.saveBlob,
}));
// Keep the page, router, ExpenseTab, file input, static Modal and async hook real.
// Unrelated inactive tabs do not participate in these workbook scenarios.
vi.mock("../panel/OverviewTab", () => ({ default: () => null }));
vi.mock("../panel/PartsOrdersTab", () => ({ default: () => null }));
vi.mock("../panel/CollectionTab", () => ({ default: () => null }));
vi.mock("../panel/SiteReturnTab", () => ({ default: () => null }));
vi.mock("../panel/MaintenanceProjectAnalyticsContext", () => ({ default: () => null }));
vi.mock("../panel/AcceptanceTab", () => ({ default: () => null }));

import MaintenanceProjectPanelPage from "../MaintenanceProjectPanelPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}
const ready = <T,>(value: T) => ({ state: "ready", value, as_of: null });
const project = (id: string) => ({ data: { project: {
  project_id: id, project_code: `XSDD-${id}`, display_name: `项目${id}`,
  is_active: true, lifecycle_status: "ongoing", version: 1,
} } });
const board = (id: string) => ({ data: {
  project_id: id, project_code: `XSDD-${id}`, display_name: `项目${id}`,
  contract_nos: [`XSDD-${id}`], lifecycle: "ongoing", is_archived: false,
  contract_amount_inc_tax: ready("1000.00"), cost_ratio_pct: ready("10.0"),
  known_apply_cost_inc_tax: ready({ known_amount: "100.00", quality: "actual_only", coverage_pct: 100 }),
} });
const workspace = (id: string, received = 100) => ({ data: {
  project: { project_id: id, metrics: { received_amount: received, collection_progress_pct: 10 } },
  collection_snapshots: { rows: [], total: 0, page: 1, page_size: 100 },
} });
const expenses = (id: string) => ({ rows: [{
  raw_line_id: `expense-${id}`, bxd_no: `BXD-${id}`, remark: `报销快照${id}`,
}], total: 1 });
const preview = (): WorkbookValidateResult => ({ will_void_rows: [], expense_updates: 1 });
const applied = (): WorkbookApplyResult => ({
  expense_updates: 1, cost_refills: 0, site_return_flags: 0, collection_creates: 0, collection_voids: 0,
});
const file = () => new File(["synthetic workbook"], "回填.xlsx", {
  type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
});
function allowUpload(allowed: boolean) {
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true, action_maintenance_expense_collection_upload: allowed,
  }));
}
function panel() {
  return <MemoryRouter initialEntries={["/maintenance/projects/A?tab=expense"]}>
    <Link to="/maintenance/projects/A?tab=expense">转到A</Link>
    <Link to="/maintenance/projects/B?tab=expense">转到B</Link>
    <Routes><Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} /></Routes>
  </MemoryRouter>;
}
async function waitForProject(id: string) {
  await screen.findByRole("heading", { name: `项目${id}` });
  await screen.findByText(`报销快照${id}`);
}
function upload(container: HTMLElement, selected: File, target: "master" | "expense" = "master") {
  const inputs = container.querySelectorAll<HTMLInputElement>('input[type="file"]');
  expect(inputs).toHaveLength(2);
  fireEvent.change(inputs[target === "master" ? 0 : 1], { target: { files: [selected] } });
}
async function confirm(title = "本项目总表") {
  const dialog = await screen.findByRole("dialog", { name: `确认回传${title}？` });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认回传" }));
}
async function switchTo(id: string) {
  fireEvent.click(screen.getByRole("link", { name: `转到${id}` }));
  await waitForProject(id);
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "session-A");
  allowUpload(true);
  api.project.mockImplementation(async (id: string) => project(id));
  api.board.mockImplementation(async (id: string) => board(id));
  api.workspace.mockImplementation(async (id: string) => workspace(id));
  api.expense.mockImplementation(async (id: string) => expenses(id));
  api.validate.mockResolvedValue(preview());
  api.apply.mockResolvedValue({ expense_updates: 1 });
  api.download.mockResolvedValue(new Blob(["current workbook"]));
});
afterEach(cleanupAntd);

describe("工作簿真实项目消费者的上下文交叉核验", () => {
  it("真实路由 A→B→A 重建项目子树：旧下载和预检不能占用新项目的文件或确认", async () => {
    const oldDownload = deferred<Blob>();
    const oldPreview = deferred<WorkbookValidateResult>();
    const currentPreview = deferred<WorkbookValidateResult>();
    api.download.mockReturnValueOnce(oldDownload.promise);
    api.validate.mockReturnValueOnce(oldPreview.promise).mockReturnValueOnce(currentPreview.promise);
    const view = render(panel());
    await waitForProject("A");
    fireEvent.click(screen.getByRole("button", { name: /下载本项目总表/ }));
    upload(view.container, file());
    await waitFor(() => expect(api.validate).toHaveBeenCalledTimes(1));
    await switchTo("B");
    await switchTo("A");
    const currentFile = file();
    upload(view.container, currentFile);
    await waitFor(() => expect(api.validate).toHaveBeenCalledTimes(2));
    await act(async () => {
      oldDownload.resolve(new Blob(["old A workbook"]));
      oldPreview.resolve(preview());
    });
    expect(api.saveBlob).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getAllByRole("button", { name: /上传覆盖/ })[0]).toHaveClass("ant-btn-loading");
    await act(async () => currentPreview.resolve(preview()));
    await confirm();
    await waitFor(() => expect(api.apply).toHaveBeenCalledWith("A", currentFile, undefined));
    fireEvent.click(screen.getByRole("button", { name: /下载本项目总表/ }));
    await waitFor(() => expect(api.saveBlob).toHaveBeenCalledWith(expect.any(Blob), "XSDD-A-项目A-总表.xlsx"));
    expect(api.saveBlob).toHaveBeenCalledTimes(1);
  });

  it("A 的 apply 已发送后转到 B：旧成功不启动父级刷新，B 的真实报销入口仍携带 B 与预检 token", async () => {
    const appliedA = deferred<WorkbookApplyResult>();
    api.apply.mockReturnValueOnce(appliedA.promise);
    const view = render(panel());
    await waitForProject("A");
    const oldFile = file();
    upload(view.container, oldFile);
    await confirm();
    await waitFor(() => expect(api.apply).toHaveBeenCalledWith("A", oldFile, undefined));
    await switchTo("B");
    const readsBefore = [api.project.mock.calls.length, api.workspace.mock.calls.length, api.expense.mock.calls.length];
    await act(async () => appliedA.resolve(applied()));
    expect([api.project.mock.calls.length, api.workspace.mock.calls.length, api.expense.mock.calls.length]).toEqual(readsBefore);
    expect(localStorage.getItem(MAINTENANCE_CHANGED_STORAGE_KEY)).toBeNull();
    expect(screen.queryByText(/已覆盖并刷新/)).toBeNull();
    api.validate.mockResolvedValueOnce({ ...preview(), site_issue_merge_token: "preview-for-B" });
    const currentFile = file();
    upload(view.container, currentFile, "expense");
    await confirm("报销");
    await waitFor(() => expect(api.apply).toHaveBeenLastCalledWith("B", currentFile, { siteIssueMergeToken: "preview-for-B" }));
    await screen.findByText("已覆盖并刷新：报销更新 1 行");
    expect(api.validate).toHaveBeenLastCalledWith("B", currentFile);
    expect(api.project.mock.calls.slice(readsBefore[0])).toEqual([["B"]]);
    expect(api.expense.mock.calls.slice(readsBefore[2])).toEqual([["B"]]);
    fireEvent.click(screen.getByRole("button", { name: /下载报销/ }));
    await waitFor(() => expect(api.download).toHaveBeenCalledWith("B", ["04_报销订单"]));
  });

  it("A 已进入真实父级和 tab 读回后转 B：迟到回款分页不继续，旧报销快照不落入 B", async () => {
    const oldWorkspace = deferred<ReturnType<typeof workspace>>();
    const oldExpenses = deferred<ReturnType<typeof expenses>>();
    const view = render(panel());
    await waitForProject("A");
    api.workspace.mockReturnValueOnce(oldWorkspace.promise);
    api.expense.mockReturnValueOnce(oldExpenses.promise);
    upload(view.container, file(), "expense");
    await confirm("报销");
    await waitFor(() => expect(api.expense).toHaveBeenCalledTimes(2));
    await switchTo("B");
    await act(async () => {
      const stale = workspace("A", 987654);
      stale.data.collection_snapshots.total = 101;
      oldWorkspace.resolve(stale);
      oldExpenses.resolve(expenses("旧A"));
    });
    expect(api.workspace.mock.calls.filter(([id]) => id === "A")).toHaveLength(2);
    expect(screen.queryByText("报销快照旧A")).toBeNull();
    expect(screen.getByText("报销快照B")).toBeVisible();
    expect(screen.getByTestId("panel-health-band").textContent).not.toContain("987,654");
    expect(screen.queryByText(/已覆盖并刷新/)).toBeNull();
  });

  it("token 未触发 React 重绘时点击真实旧确认，仍不得调用项目 apply", async () => {
    const view = render(panel());
    await waitForProject("A");
    upload(view.container, file(), "expense");
    const dialog = await screen.findByRole("dialog", { name: "确认回传报销？" });
    localStorage.setItem("token", "session-B");
    fireEvent.click(within(dialog).getByRole("button", { name: "确认回传" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.apply).not.toHaveBeenCalled();
    expect(localStorage.getItem(MAINTENANCE_CHANGED_STORAGE_KEY)).toBeNull();
  });

  it("撤销并恢复上传资格期间已发送的 apply 不复活父级刷新，新文件可以继续操作", async () => {
    const oldApply = deferred<WorkbookApplyResult>();
    api.apply.mockReturnValueOnce(oldApply.promise);
    const view = render(panel());
    await waitForProject("A");
    upload(view.container, file(), "expense");
    await confirm("报销");
    await waitFor(() => expect(api.apply).toHaveBeenCalledTimes(1));
    allowUpload(false);
    view.rerender(panel());
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
    allowUpload(true);
    view.rerender(panel());
    expect(screen.getAllByRole("button", { name: /上传覆盖/ })).toHaveLength(2);
    await act(async () => oldApply.resolve(applied()));
    expect(api.project).toHaveBeenCalledTimes(1);
    expect(api.expense).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(MAINTENANCE_CHANGED_STORAGE_KEY)).toBeNull();
    const currentFile = file();
    upload(view.container, currentFile, "expense");
    await confirm("报销");
    await screen.findByText("已覆盖并刷新：报销更新 1 行");
    expect(api.apply).toHaveBeenLastCalledWith("A", currentFile, undefined);
    expect(api.expense).toHaveBeenCalledTimes(2);
  });

  it.each(["master", "expense"] as const)("%s 旧页面 token 已变但未重绘时，新下载也不能借新账号发请求；重绘后正常下载", async (target) => {
    const view = render(panel());
    await waitForProject("A");
    const buttonName = target === "master" ? /下载本项目总表/ : /下载报销/;
    localStorage.setItem("token", "session-B");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: buttonName })));
    expect(api.download).not.toHaveBeenCalled();
    expect(api.saveBlob).not.toHaveBeenCalled();
    view.rerender(panel());
    fireEvent.click(screen.getByRole("button", { name: buttonName }));
    await waitFor(() => expect(api.download).toHaveBeenCalledTimes(1));
    if (target === "master") expect(api.download).toHaveBeenCalledWith("A");
    else expect(api.download).toHaveBeenCalledWith("A", ["04_报销订单"]);
    await waitFor(() => expect(api.saveBlob).toHaveBeenCalledTimes(1));
  });

  it.each(["master", "expense"] as const)("%s 旧页面 token 已变但未重绘时，新上传不能借新账号预检；重绘后正常应用", async (target) => {
    const view = render(panel());
    await waitForProject("A");
    localStorage.setItem("token", "session-B");
    await act(async () => upload(view.container, file(), target));
    expect(api.validate).not.toHaveBeenCalled();
    expect(api.apply).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(panel());
    const currentFile = file();
    upload(view.container, currentFile, target);
    await confirm(target === "master" ? "本项目总表" : "报销");
    await waitFor(() => expect(api.apply).toHaveBeenCalledWith("A", currentFile, undefined));
    expect(api.validate).toHaveBeenCalledTimes(1);
    await screen.findByText("已覆盖并刷新：报销更新 1 行");
  });
});
