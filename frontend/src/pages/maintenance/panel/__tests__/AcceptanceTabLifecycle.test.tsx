import { Modal } from "antd";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getMaintenanceAcceptanceChecklist: vi.fn(),
  downloadAcceptanceChecklistTemplate: vi.fn(),
  previewMaintenanceAcceptanceChecklist: vi.fn(),
  applyMaintenanceAcceptanceChecklist: vi.fn(),
  saveBlob: vi.fn(),
}));

vi.mock("../../../../api/maintenanceOperations", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceOperations")
  >("../../../../api/maintenanceOperations");
  return {
    ...actual,
    getMaintenanceAcceptanceChecklist:
      mocks.getMaintenanceAcceptanceChecklist,
    downloadAcceptanceChecklistTemplate:
      mocks.downloadAcceptanceChecklistTemplate,
    previewMaintenanceAcceptanceChecklist:
      mocks.previewMaintenanceAcceptanceChecklist,
    applyMaintenanceAcceptanceChecklist:
      mocks.applyMaintenanceAcceptanceChecklist,
  };
});

vi.mock("../../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceWorkbooks")
  >("../../../../api/maintenanceWorkbooks");
  return { ...actual, saveBlob: mocks.saveBlob };
});

vi.mock("../../../../components/maintenance/MaintenanceAcceptancePanel", () => ({
  default: () => <div data-testid="acceptance-deliverable-panel" />,
}));

import AcceptanceTab from "../AcceptanceTab";

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(Storage.prototype, "getItem").mockImplementation((key: string) => {
    if (key === "role") return "admin";
    if (key === "permissions") return "{}";
    return null;
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const loaded = {
  current: {
    batch_id: "b-1",
    filename: "清单.xlsx",
    uploaded_by: "tester",
    applied_by: "tester",
    applied_at: "2026-08-21T10:00:00",
    item_rows: 2,
    done_rows: 1,
    todo_rows: 1,
    items: [
      { item_id: "i-1", row_no: 2, requirement: "设备巡检报告归档", done: true },
      { item_id: "i-2", row_no: 3, requirement: "备件损耗清单签字", done: false },
    ],
  },
  history: [
    { batch_id: "b-1", filename: "清单.xlsx", applied_by: "tester",
      applied_at: "2026-08-21T10:00:00", item_rows: 2 },
  ],
};


function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const preview = (id: string) => ({ data: {
  batch_id: id, item_rows: 7, done_rows: 0, todo_rows: 7,
  issue_rows: 0, will_replace_rows: 0, issues: [],
} });
function upload(container: HTMLElement, name = "清单.xlsx") {
  fireEvent.change(container.querySelector('input[type="file"]')!, {
    target: { files: [new File(["xlsx"], name)] },
  });
}

describe("验收上传的项目与操作所有权", () => {
  beforeEach(() => {
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({ data: { current: null, history: [] } });
  });

  it.each(["switch", "unmount"])("预检迟到在%s后不能打开确认", async (change) => {
    const pending = deferred<ReturnType<typeof preview>>();
    mocks.previewMaintenanceAcceptanceChecklist.mockReturnValue(pending.promise);
    const confirm = vi.spyOn(Modal, "confirm");
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    upload(view.container);
    await waitFor(() => expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(1));
    if (change === "switch") view.rerender(<AcceptanceTab projectId="B" canImport />);
    else view.unmount();
    await act(async () => { pending.resolve(preview("batch-A")); });
    expect(confirm).not.toHaveBeenCalled();
    expect(mocks.applyMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
  });

  it("项目切换销毁已有确认，旧onOk也不得写入", async () => {
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue(preview("batch-A"));
    const destroy = vi.fn();
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy, update: vi.fn() });
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    const oldOk = confirm.mock.calls[0][0].onOk!;
    view.rerender(<AcceptanceTab projectId="B" canImport />);
    expect(destroy).toHaveBeenCalledOnce();
    await act(async () => { await oldOk(); });
    expect(mocks.applyMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
  });

  it.each(["switch", "unmount"])("apply已发送在%s后完成不触发旧项目读回", async (change) => {
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue(preview("batch-A"));
    const pending = deferred<object>();
    mocks.applyMaintenanceAcceptanceChecklist.mockReturnValue(pending.promise);
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    const refreshA = vi.fn().mockResolvedValue(true);
    const refreshB = vi.fn().mockResolvedValue(true);
    const view = render(<AcceptanceTab projectId="A" canImport onChanged={refreshA} />);
    await screen.findByText(/尚未导入验收清单/);
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    let applied: Promise<unknown>;
    act(() => { applied = confirm.mock.calls[0][0].onOk!() as Promise<unknown>; });
    expect(mocks.applyMaintenanceAcceptanceChecklist).toHaveBeenCalledWith("batch-A");
    if (change === "switch") view.rerender(<AcceptanceTab projectId="B" canImport onChanged={refreshB} />);
    else view.unmount();
    const reads = mocks.getMaintenanceAcceptanceChecklist.mock.calls.length;
    await act(async () => { pending.resolve({}); await applied; });
    expect(mocks.getMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(reads);
    expect(refreshA).not.toHaveBeenCalled();
    expect(refreshB).not.toHaveBeenCalled();
    expect(screen.queryByText(/验收清单已生效并刷新/)).not.toBeInTheDocument();
  });

  it("同一项目较早预检不能覆盖新预检", async () => {
    const first = deferred<ReturnType<typeof preview>>();
    const second = deferred<ReturnType<typeof preview>>();
    mocks.previewMaintenanceAcceptanceChecklist.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    // 同一原生change携带多文件时rc-upload会并发调用beforeUpload；maxCount不是并发锁。
    fireEvent.change(view.container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["1"], "first.xlsx"), new File(["2"], "second.xlsx")] },
    });
    await waitFor(() => expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    await act(async () => { second.resolve(preview("batch-second")); });
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    await act(async () => { first.resolve(preview("batch-first")); });
    expect(confirm).toHaveBeenCalledOnce();
    await act(async () => { await confirm.mock.calls[0][0].onOk!(); });
    expect(mocks.applyMaintenanceAcceptanceChecklist).toHaveBeenCalledOnce();
    expect(mocks.applyMaintenanceAcceptanceChecklist).toHaveBeenCalledWith("batch-second");
  });

  it("成功写入后本地读回期间切项目，不调用父级旧刷新", async () => {
    const readback = deferred<{ data: typeof loaded }>();
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValueOnce({data:{current:null,history:[]}})
      .mockReturnValueOnce(readback.promise).mockResolvedValue({data:{current:null,history:[]}});
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue(preview("batch-A"));
    mocks.applyMaintenanceAcceptanceChecklist.mockResolvedValue({});
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({destroy:vi.fn(),update:vi.fn()});
    const refresh = vi.fn().mockResolvedValue(true);
    const view = render(<AcceptanceTab projectId="A" canImport onChanged={refresh} />);
    await screen.findByText(/尚未导入验收清单/);
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    let applied: Promise<unknown>;
    act(() => { applied = confirm.mock.calls[0][0].onOk!() as Promise<unknown>; });
    await waitFor(() => expect(mocks.getMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    view.rerender(<AcceptanceTab projectId="B" canImport onChanged={refresh} />);
    await act(async () => { readback.resolve({data:loaded}); await applied; });
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.queryByText("设备巡检报告归档")).not.toBeInTheDocument();
  });

  it("同项目新预检取代写后读回时旧清单先失效，旧finally不留下加载态", async () => {
    const readback = deferred<{data: typeof loaded}>();
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValueOnce({data:loaded})
      .mockReturnValueOnce(readback.promise);
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValueOnce(preview("first"))
      .mockResolvedValueOnce({data:{...preview("second").data,issue_rows:1,issues:["新文件问题"]}});
    mocks.applyMaintenanceAcceptanceChecklist.mockResolvedValue({});
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({destroy:vi.fn(),update:vi.fn()});
    const refresh = vi.fn().mockResolvedValue(true);
    const view = render(<AcceptanceTab projectId="A" canImport onChanged={refresh} />);
    await screen.findByText("设备巡检报告归档");
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    let applied: Promise<unknown>;
    act(() => { applied = confirm.mock.calls[0][0].onOk!() as Promise<unknown>; });
    await waitFor(() => expect(mocks.getMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("设备巡检报告归档")).not.toBeInTheDocument();
    // 模拟已经排队的第二次原生文件change，不依赖按钮disabled作为并发安全性。
    upload(view.container, "second.xlsx");
    await waitFor(() => expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    await screen.findByText("新文件问题");
    await act(async () => { readback.resolve({data:loaded}); await applied; });
    expect(screen.queryByText("设备巡检报告归档")).not.toBeInTheDocument();
    expect(screen.queryByText("清单加载中…")).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

});

describe("验收清单的账号与上传权限生命周期", () => {
  let session: string;
  beforeEach(() => {
    session = "session-a";
    vi.mocked(Storage.prototype.getItem).mockImplementation((key: string) => {
      if (key === "token") return session;
      if (key === "role") return "admin";
      if (key === "permissions") return "{}";
      return null;
    });
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({ data: loaded });
  });

  it.each(["session", "permission"])("%s变化后迟到预检不能打开确认，读取仍可用", async (change) => {
    const pending = deferred<ReturnType<typeof preview>>();
    mocks.previewMaintenanceAcceptanceChecklist.mockReturnValue(pending.promise);
    const confirm = vi.spyOn(Modal, "confirm");
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText("设备巡检报告归档");
    upload(view.container);
    await waitFor(() => expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledOnce());
    if (change === "session") session = "session-b";
    view.rerender(<AcceptanceTab projectId="A" canImport={change !== "permission"} />);
    await act(async () => { pending.resolve(preview("old-account")); });
    expect(confirm).not.toHaveBeenCalled();
    expect(mocks.applyMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
    expect(await screen.findByText("设备巡检报告归档")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /下载模板/ })).toBeEnabled();
  });

  it("写后读回期间撤销上传权限，当前只读清单仍会重新加载", async () => {
    const oldRead = deferred<{ data: typeof loaded }>();
    const fresh = { ...loaded, current: { ...loaded.current, filename: "当前只读清单.xlsx" } };
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValueOnce({ data: loaded })
      .mockReturnValueOnce(oldRead.promise).mockResolvedValue({ data: fresh });
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue(preview("committed"));
    mocks.applyMaintenanceAcceptanceChecklist.mockResolvedValue({});
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    const refresh = vi.fn().mockResolvedValue(true);
    const view = render(<AcceptanceTab projectId="A" canImport onChanged={refresh} />);
    await screen.findByText("设备巡检报告归档");
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    let applied: Promise<unknown>;
    act(() => { applied = confirm.mock.calls[0][0].onOk!() as Promise<unknown>; });
    await waitFor(() => expect(mocks.getMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    view.rerender(<AcceptanceTab projectId="A" canImport={false} onChanged={refresh} />);
    await screen.findByText(/当前只读清单.xlsx/);
    await act(async () => { oldRead.resolve({ data: loaded }); await applied; });
    expect(screen.getByText(/当前只读清单.xlsx/)).toBeInTheDocument();
    expect(screen.queryByText(/尚未导入验收清单/)).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    expect(mocks.applyMaintenanceAcceptanceChecklist).toHaveBeenCalledOnce();
  });

  it.each(["session", "permission"])("%s变化再恢复也不能复活旧确认", async (change) => {
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue(preview("old-account"));
    const destroy = vi.fn();
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy, update: vi.fn() });
    const view = render(<AcceptanceTab projectId="A" canImport />);
    await screen.findByText("设备巡检报告归档");
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    const oldOk = confirm.mock.calls[0][0].onOk!;
    if (change === "session") session = "session-b";
    view.rerender(<AcceptanceTab projectId="A" canImport={change !== "permission"} />);
    session = "session-a";
    view.rerender(<AcceptanceTab projectId="A" canImport />);
    await act(async () => { await oldOk(); });
    expect(destroy).toHaveBeenCalledOnce();
    expect(mocks.applyMaintenanceAcceptanceChecklist).not.toHaveBeenCalled();
  });

  it("已发送的写入在账号切换后完成，不触发旧账号读回和父刷新", async () => {
    const pending = deferred<void>();
    mocks.applyMaintenanceAcceptanceChecklist.mockReturnValue(pending.promise);
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue(preview("old-account"));
    const refresh = vi.fn().mockResolvedValue(true);
    const confirm = vi.spyOn(Modal, "confirm").mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    const view = render(<AcceptanceTab projectId="A" canImport onChanged={refresh} />);
    await screen.findByText("设备巡检报告归档");
    upload(view.container);
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    let applying!: Promise<unknown>;
    act(() => { applying = confirm.mock.calls[0][0].onOk!(); });
    await waitFor(() => expect(mocks.applyMaintenanceAcceptanceChecklist).toHaveBeenCalledOnce());
    session = "session-b";
    view.rerender(<AcceptanceTab projectId="A" canImport onChanged={refresh} />);
    await waitFor(() => expect(mocks.getMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2));
    await act(async () => { pending.resolve(); await applying; });
    expect(mocks.getMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(2);
    expect(refresh).not.toHaveBeenCalled();
  });
});
