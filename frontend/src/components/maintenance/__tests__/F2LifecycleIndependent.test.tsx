import { StrictMode, Suspense, startTransition, useState } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { Modal } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { WorkbookApplyResult, WorkbookValidateResult } from "../../../api/maintenanceWorkbooks";

const api = vi.hoisted(() => ({
  saveBlob: vi.fn(), project: vi.fn(), board: vi.fn(), workspace: vi.fn(),
  download: vi.fn(), validate: vi.fn(), apply: vi.fn(),
}));
vi.mock("../../../api/maintenanceWorkbooks", async () => ({
  ...await vi.importActual("../../../api/maintenanceWorkbooks"),
  saveBlob: api.saveBlob, downloadProjectMaster: api.download,
  validateProjectMaster: api.validate, applyProjectMaster: api.apply,
}));
vi.mock("../../../api/maintenanceProjects", async () => ({
  ...await vi.importActual("../../../api/maintenanceProjects"), getMaintenanceProject: api.project,
}));
vi.mock("../../../api/maintenanceBossBoard", async () => ({
  ...await vi.importActual("../../../api/maintenanceBossBoard"), getBoardProject: api.board,
}));
vi.mock("../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual("../../../api/maintenanceOperations"), getMaintenanceProjectWorkspace: api.workspace,
}));
// The real panel, loaders, router, workbook, Modal and async owner remain mounted.
// Inactive tabs are unrelated to the qualification race under test.
vi.mock("../../../pages/maintenance/panel/OverviewTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/PartsOrdersTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/ExpenseTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/CollectionTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/SiteReturnTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/AcceptanceTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/MaintenanceProjectAnalyticsContext", () => ({ default: () => null }));

import WorkbookRoundTrip from "../WorkbookRoundTrip";
import { readMaintenanceWorkbookContext } from "../maintenancePermissions";
import MaintenanceProjectPanelPage from "../../../pages/maintenance/MaintenanceProjectPanelPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const preview = (label = "current") => ({ will_void_rows: [{ reason: label }] });
const result = (): WorkbookApplyResult => ({
  cost_refills: 0, site_return_flags: 0, collection_creates: 0, collection_voids: 0, expense_updates: 1,
});
const conflict = () => ({ response: { status: 409, data: { detail: { conflicts: [
  { sheet: "04", row: "BXD", field: "remark", base: "base", old: "someone", new: "mine" },
] } } } });
const file = () => new File(["independent synthetic workbook"], "independent.xlsx", { type: "application/vnd.ms-excel" });
function upload(container: HTMLElement) {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input).not.toBeNull();
  fireEvent.change(input!, { target: { files: [file()] } });
}
function permissions(scope = "all") {
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true, data_profit: true, data_purchase_cost: true,
    action_maintenance_expense_collection_upload: true, own_customers_only: scope !== "all",
  }));
}
const props = () => ({
  title: "独立核验", filename: "independent.xlsx", canDownload: true, canUpload: true,
  onDownload: vi.fn().mockResolvedValue(new Blob(["fresh"])),
  onApply: vi.fn().mockResolvedValue(result()),
});
async function confirm() {
  await waitFor(() => expect(screen.getByRole("button", { name: "确认回传" })).toBeVisible());
  fireEvent.click(screen.getByRole("button", { name: "确认回传" }));
}
function panel() {
  return <MemoryRouter initialEntries={["/maintenance/projects/A"]}>
    <Link to="/maintenance/projects/A">switch A</Link><Link to="/maintenance/projects/B">switch B</Link>
    <Routes><Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} /></Routes>
  </MemoryRouter>;
}
const detail = (id: string, edit: boolean) => ({ data: { project: {
  project_id: id, project_code: `XSDD-${id}`, display_name: `独立项目${id}`,
  is_active: true, lifecycle_status: "ongoing", version: 1, can_edit_master_workbook: edit,
} } });
const board = (id: string, edit: boolean) => ({ data: {
  project_id: id, project_code: `XSDD-${id}`, display_name: `独立项目${id}`,
  contract_nos: [`XSDD-${id}`], lifecycle: "ongoing", is_archived: false,
  can_edit_master_workbook: edit,
  contract_amount_inc_tax: { state: "ready", value: "100.00" },
  cost_ratio_pct: { state: "ready", value: "10.00" },
  known_apply_cost_inc_tax: { state: "ready", value: { known_amount: "10.00", quality: "actual_only", coverage_pct: 100 } },
} });

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "account-A");
  localStorage.setItem("role", "staff");
  permissions();
  api.project.mockImplementation(async (id: string) => detail(id, true));
  api.board.mockImplementation(async (id: string) => board(id, true));
  api.workspace.mockImplementation(async (id: string) => ({ data: {
    project: { project_id: id, metrics: {} }, collection_snapshots: { rows: [], total: 0 },
  } }));
  api.download.mockResolvedValue(new Blob(["panel"]));
  api.validate.mockResolvedValue(preview());
  api.apply.mockResolvedValue(result());
});
afterEach(async () => {
  vi.restoreAllMocks();
  await cleanupAntd();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

describe("F2 independent operation ownership", () => {
  it("a revoked download rejection cannot clear the loading state of a restored fresh request", async () => {
    const old = deferred<Blob>();
    const fresh = deferred<Blob>();
    const p = props();
    p.onDownload.mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise);
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} canDownload={false} />);
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    await act(async () => old.reject({ response: { data: { detail: "stale-secret" } } }));
    expect(screen.getByRole("button", { name: /下载/ })).toHaveClass("ant-btn-loading");
    expect(screen.queryByText("stale-secret")).toBeNull();
    const blob = new Blob(["fresh-only"]);
    await act(async () => fresh.resolve(blob));
    expect(api.saveBlob).toHaveBeenCalledExactlyOnceWith(blob, "independent.xlsx");
  });

  it("an old preflight rejection does not release the active upload or create a stale conflict modal", async () => {
    const old = deferred<WorkbookValidateResult>();
    const current = deferred<WorkbookValidateResult>();
    const validate = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
    const p = { ...props(), onValidate: validate };
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    await waitFor(() => expect(validate).toHaveBeenCalledTimes(1));
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} canUpload={false} />);
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    await waitFor(() => expect(validate).toHaveBeenCalledTimes(2));
    await act(async () => old.reject(conflict()));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: /上传覆盖/ })).toHaveClass("ant-btn-loading");
    await act(async () => current.resolve(preview("fresh preview")));
    await confirm();
    await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(1));
  });

  it("a late force-takeover rejection and its finally cannot destroy the next generation's confirmation", async () => {
    const takeover = deferred<WorkbookApplyResult>();
    const p = { ...props(), onValidate: vi.fn().mockRejectedValueOnce(conflict()).mockResolvedValue(preview("new generation")) };
    p.onApply.mockReturnValueOnce(takeover.promise).mockResolvedValue(result());
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    fireEvent.click(await screen.findByRole("button", { name: "强制接管并上传" }));
    await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(1));
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} canDownload={false} />);
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    await screen.findByRole("dialog", { name: "确认回传独立核验？" });
    await act(async () => takeover.reject({ response: { data: { detail: "old takeover failed" } } }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "确认回传独立核验？" })).toBeVisible());
    expect(screen.getByRole("button", { name: /上传覆盖/ })).toHaveClass("ant-btn-loading");
    expect(screen.queryByText("old takeover failed")).toBeNull();
    await confirm();
    await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(2));
    expect(p.onApply.mock.calls[1][1]).toBeUndefined();
  });

  it("silent scope narrowing invalidates preflight and download even while both capability booleans stay true", async () => {
    const read = deferred<Blob>();
    const validate = deferred<WorkbookValidateResult>();
    const p = { ...props(), onValidate: vi.fn().mockReturnValueOnce(validate.promise).mockResolvedValue(preview()) };
    p.onDownload.mockReturnValueOnce(read.promise).mockResolvedValue(new Blob(["fresh"]));
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    upload(view.container);
    await waitFor(() => expect(p.onValidate).toHaveBeenCalledTimes(1));
    permissions("own");
    await act(async () => { validate.resolve(preview("scope-all secret")); read.resolve(new Blob(["scope-all data"])); });
    expect(api.saveBlob).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("button", { name: /下载|上传覆盖/ })).toBeNull();
    // Only a parent-approved new owner can use the new scope.
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} key="new-permission-owner" {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    await waitFor(() => expect(api.saveBlob).toHaveBeenCalledTimes(1));
  });

  it("a silent token change blocks visible old buttons before dispatching any request", async () => {
    const p = props();
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    localStorage.setItem("token", "account-B");
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    upload(view.container);
    await act(async () => {});
    expect(p.onDownload).not.toHaveBeenCalled();
    expect(p.onApply).not.toHaveBeenCalled();
  });

  it("committed token A-B-A retires an old confirm even when it is invoked during the new confirm", async () => {
    const modal = vi.spyOn(Modal, "confirm");
    const p = { ...props(), onValidate: vi.fn().mockResolvedValue(preview()) };
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    await screen.findByRole("dialog");
    const oldOk = modal.mock.calls[0][0].onOk!;
    localStorage.setItem("token", "account-B"); view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    localStorage.setItem("token", "account-A"); view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    await waitFor(() => expect(modal).toHaveBeenCalledTimes(2));
    await act(async () => { await oldOk(); });
    expect(p.onApply).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /上传覆盖/ })).toHaveClass("ant-btn-loading");
    await confirm();
    await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(1));
  });

  it("a stale refresh rejection cannot publish warnings or clear a newer upload after revocation", async () => {
    const refresh = deferred<boolean>();
    const current = deferred<WorkbookValidateResult>();
    const p = { ...props(), onAfterApply: vi.fn().mockReturnValueOnce(refresh.promise).mockResolvedValue(true),
      onValidate: vi.fn().mockResolvedValueOnce(preview()).mockReturnValueOnce(current.promise) };
    const view = render(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container); await confirm();
    await waitFor(() => expect(p.onAfterApply).toHaveBeenCalledTimes(1));
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} canUpload={false} />);
    view.rerender(<WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} />);
    upload(view.container);
    await waitFor(() => expect(p.onValidate).toHaveBeenCalledTimes(2));
    await act(async () => refresh.reject(new Error("old refresh failed")));
    expect(screen.queryByText(/数据已写入，但页面刷新失败/)).toBeNull();
    expect(screen.getByRole("button", { name: /上传覆盖/ })).toHaveClass("ant-btn-loading");
    await act(async () => current.resolve(preview())); await confirm();
    await waitFor(() => expect(p.onAfterApply).toHaveBeenCalledTimes(2));
  });

  it("StrictMode unmount and remount isolate a pending conflict takeover from the fresh owner's modal", async () => {
    const old = deferred<WorkbookApplyResult>();
    const p = { ...props(), onValidate: vi.fn().mockRejectedValueOnce(conflict()).mockResolvedValue(preview()) };
    p.onApply.mockReturnValueOnce(old.promise).mockResolvedValue(result());
    const first = render(<StrictMode><WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} /></StrictMode>);
    upload(first.container);
    fireEvent.click(await screen.findByRole("button", { name: "强制接管并上传" }));
    await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(1));
    first.unmount();
    const next = render(<StrictMode><WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} /></StrictMode>);
    upload(next.container);
    await screen.findByRole("dialog", { name: "确认回传独立核验？" });
    await act(async () => old.reject(conflict()));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /上传覆盖/ })).toHaveClass("ant-btn-loading");
    await confirm();
    await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(2));
  });

  it("an uncommitted suspended revocation neither closes the committed dialog nor blocks current download", async () => {
    const never = new Promise<void>(() => {});
    const attempted = vi.fn();
    const p = { ...props(), onValidate: vi.fn().mockResolvedValue(preview()) };
    function Gate({ suspend }: { suspend: boolean }) {
      if (suspend) { attempted(); throw never; }
      return null;
    }
    function Harness() {
      const [candidate, setCandidate] = useState(false);
      return <>
        <button onClick={() => startTransition(() => setCandidate(true))}>try revocation</button>
        <button onClick={() => setCandidate(false)}>discard render</button>
        <Suspense fallback={<div>pending candidate</div>}>
          <WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} canDownload={!candidate} /><Gate suspend={candidate} />
        </Suspense>
      </>;
    }
    const view = render(<StrictMode><Harness /></StrictMode>);
    upload(view.container);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
    await act(async () => fireEvent.click(screen.getByText("try revocation")));
    expect(attempted).toHaveBeenCalled();
    expect(screen.queryByText("pending candidate")).toBeNull();
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    await waitFor(() => expect(api.saveBlob).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("discard render"));
    await confirm(); await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(1));
  });

  it("discarding a suspended account-B owner leaves committed account-A work valid after restoring its token", async () => {
    const never = new Promise<void>(() => {});
    const attempted = vi.fn();
    const pendingA = deferred<Blob>();
    const p = { ...props(), onValidate: vi.fn().mockResolvedValue(preview()) };
    p.onDownload.mockReturnValueOnce(pendingA.promise);
    function Gate({ candidate }: { candidate: boolean }) {
      if (candidate) { attempted(); throw never; }
      return null;
    }
    function Harness() {
      const [candidate, setCandidate] = useState(false);
      return <>
        <button onClick={() => {
          localStorage.setItem("token", "account-B");
          startTransition(() => setCandidate(true));
        }}>try account B</button>
        <button onClick={() => {
          localStorage.setItem("token", "account-A"); setCandidate(false);
        }}>keep committed A</button>
        <Suspense fallback={<div>pending B owner</div>}>
          <WorkbookRoundTrip permissionContext={readMaintenanceWorkbookContext()} {...p} /><Gate candidate={candidate} />
        </Suspense>
      </>;
    }
    const view = render(<StrictMode><Harness /></StrictMode>);
    fireEvent.click(screen.getByRole("button", { name: /下载/ }));
    upload(view.container);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
    await act(async () => fireEvent.click(screen.getByText("try account B")));
    expect(attempted).toHaveBeenCalled();
    expect(screen.queryByText("pending B owner")).toBeNull();
    fireEvent.click(screen.getByText("keep committed A"));
    const blobA = new Blob(["still-owned-by-A"]);
    await act(async () => pendingA.resolve(blobA));
    expect(api.saveBlob).toHaveBeenCalledExactlyOnceWith(blobA, "independent.xlsx");
    await confirm(); await waitFor(() => expect(p.onApply).toHaveBeenCalledTimes(1));
  });
});

describe("F2 independent real panel qualification races", () => {
  it.each([
    ["board", "token"], ["workspace", "token"],
    ["board", "permissions"], ["workspace", "permissions"],
  ] as const)("a late %s result cannot lend old server qualification to a silently changed %s owner", async (loader, identity) => {
    const oldBoard = deferred<ReturnType<typeof board>>();
    const oldWorkspaceValue = { data: {
      project: { project_id: "A", metrics: {} }, collection_snapshots: { rows: [], total: 0 },
    } };
    const oldWorkspace = deferred<typeof oldWorkspaceValue>();
    if (loader === "board") api.board.mockReturnValueOnce(oldBoard.promise);
    else api.workspace.mockReturnValueOnce(oldWorkspace.promise);
    const view = render(panel());
    await screen.findByRole("button", { name: /上传覆盖/ });
    expect(api.project).toHaveBeenCalledTimes(1);
    if (identity === "token") localStorage.setItem("token", "account-B");
    else permissions("own");
    await act(async () => {
      if (loader === "board") oldBoard.resolve(board("A", true));
      else oldWorkspace.resolve(oldWorkspaceValue);
    });
    // Only the old Content's internal loader updated state. The Page did not fetch B.
    expect(api.project).toHaveBeenCalledTimes(1);
    const staleDownload = screen.queryByRole("button", { name: /下载本项目总表/ });
    if (staleDownload) fireEvent.click(staleDownload);
    const staleInput = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    if (staleInput) fireEvent.change(staleInput, { target: { files: [file()] } });
    await act(async () => {});
    expect(api.download).not.toHaveBeenCalled();
    expect(api.validate).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /下载本项目总表|上传覆盖/ })).toBeNull();
  });

  it("an old preflight finally cannot silently adopt a new account's token with the old manager qualification", async () => {
    const old = deferred<WorkbookValidateResult>();
    api.validate.mockReturnValueOnce(old.promise).mockResolvedValue(preview());
    // Both accounts share the same local permissions; manager qualification is server-specific.
    const view = render(panel());
    await screen.findByRole("button", { name: /上传覆盖/ });
    upload(view.container);
    await waitFor(() => expect(api.validate).toHaveBeenCalledTimes(1));
    localStorage.setItem("token", "account-B");
    await act(async () => old.resolve(preview("account-A-only")));
    expect(screen.queryByRole("dialog")).toBeNull();
    // No parent render and no B detail read occurred: A's qualification cannot be B's.
    expect(api.project).toHaveBeenCalledTimes(1);
    const staleInput = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    if (staleInput) fireEvent.change(staleInput, { target: { files: [file()] } });
    await act(async () => {});
    expect(api.validate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /下载本项目总表/ })).toBeNull();
  });

  it("same-project scope change reloads qualification and discards a late old positive board flag", async () => {
    const oldBoard = deferred<ReturnType<typeof board>>();
    api.board.mockReturnValueOnce(oldBoard.promise).mockResolvedValue(board("A", false));
    api.project.mockResolvedValueOnce(detail("A", true)).mockResolvedValue(detail("A", false));
    const view = render(panel());
    await screen.findByRole("button", { name: /上传覆盖/ });
    permissions("own"); view.rerender(panel());
    await waitFor(() => expect(api.project).toHaveBeenCalledTimes(2));
    await screen.findByRole("button", { name: /下载本项目总表/ });
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
    await act(async () => oldBoard.resolve(board("A", true)));
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /下载本项目总表/ }));
    await waitFor(() => expect(api.saveBlob).toHaveBeenCalledTimes(1));
  });

  it("A-B-A navigation ignores the first A's delayed denial while preserving the new A upload", async () => {
    const oldA = deferred<ReturnType<typeof detail>>();
    api.project.mockReturnValueOnce(oldA.promise).mockImplementation(async (id: string) => detail(id, true));
    const view = render(panel());
    await waitFor(() => expect(api.project).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("link", { name: "switch B" }));
    await screen.findByRole("heading", { name: "独立项目B" });
    fireEvent.click(screen.getByRole("link", { name: "switch A" }));
    await screen.findByRole("heading", { name: "独立项目A" });
    await act(async () => oldA.resolve(detail("A", false)));
    upload(view.container);
    const modal = await screen.findByRole("dialog", { name: "确认回传本项目总表？" });
    fireEvent.click(within(modal).getByRole("button", { name: "确认回传" }));
    await waitFor(() => expect(api.apply).toHaveBeenCalledTimes(1));
    expect(api.apply.mock.calls[0][0]).toBe("A");
  });

  it("token A-B-A keeps current denial despite a late old A detail with manager qualification", async () => {
    const oldA = deferred<ReturnType<typeof detail>>();
    api.project.mockReturnValueOnce(oldA.promise).mockResolvedValue(detail("A", false));
    api.board.mockResolvedValue(board("A", false));
    const view = render(panel());
    await waitFor(() => expect(api.project).toHaveBeenCalledTimes(1));
    localStorage.setItem("token", "account-B"); view.rerender(panel());
    await waitFor(() => expect(api.project).toHaveBeenCalledTimes(2));
    localStorage.setItem("token", "account-A"); view.rerender(panel());
    await waitFor(() => expect(api.project).toHaveBeenCalledTimes(3));
    await screen.findByRole("button", { name: /下载本项目总表/ });
    await act(async () => oldA.resolve(detail("A", true)));
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
    expect(api.board).toHaveBeenCalledTimes(2);
  });

  it("late current board denial closes detail-authorized preflight and invalidates a saved confirm callback", async () => {
    const boardResponse = deferred<ReturnType<typeof board>>();
    api.board.mockReturnValueOnce(boardResponse.promise);
    const modal = vi.spyOn(Modal, "confirm");
    const view = render(panel());
    await screen.findByRole("button", { name: /上传覆盖/ });
    upload(view.container);
    await screen.findByRole("dialog", { name: "确认回传本项目总表？" });
    const oldConfirm = modal.mock.calls[0][0].onOk!;
    await act(async () => boardResponse.resolve(board("A", false)));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("button", { name: /下载本项目总表/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /上传覆盖/ })).toBeNull();
    await act(async () => { await oldConfirm(); });
    expect(api.apply).not.toHaveBeenCalled();
  });
});
