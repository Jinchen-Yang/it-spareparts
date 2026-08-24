import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getMaintenanceAcceptanceChecklist: vi.fn(),
  downloadAcceptanceChecklistTemplate: vi.fn(),
  previewMaintenanceAcceptanceChecklist: vi.fn(),
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

describe("验收 tab（2026-08-21 客户反馈）", () => {
  it("展示清单条目与完成状态", async () => {
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({
      data: loaded,
    });
    render(<AcceptanceTab projectId="p1" canImport />);
    expect(await screen.findByText("设备巡检报告归档")).toBeInTheDocument();
    expect(screen.getByText("备件损耗清单签字")).toBeInTheDocument();
    expect(screen.getByText("已完成 1")).toBeInTheDocument();
    expect(screen.getByText("待验收 1")).toBeInTheDocument();
  });

  it("未导入时显示引导文案", async () => {
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({
      data: { current: null, history: [] },
    });
    render(<AcceptanceTab projectId="p1" canImport />);
    await waitFor(() =>
      expect(screen.getByText(/尚未导入验收清单/)).toBeInTheDocument());
    expect(screen.getByText("下载模板")).toBeInTheDocument();
    expect(screen.getByText("导入清单")).toBeInTheDocument();
  });

  it("无导入权限时不出现导入按钮", async () => {
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({
      data: { current: null, history: [] },
    });
    render(<AcceptanceTab projectId="p1" canImport={false} />);
    await waitFor(() =>
      expect(screen.getByText(/尚未导入验收清单/)).toBeInTheDocument());
    expect(screen.queryByText("导入清单")).not.toBeInTheDocument();
    expect(screen.getByText(/导入需授权/)).toBeInTheDocument();
  });

  it("下载模板复用延迟释放的统一保存逻辑", async () => {
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({
      data: { current: null, history: [] },
    });
    const blob = new Blob(["xlsx"]);
    mocks.downloadAcceptanceChecklistTemplate.mockResolvedValue({ data: blob });
    render(<AcceptanceTab projectId="p1" canImport />);

    fireEvent.click(await screen.findByText("下载模板"));
    await waitFor(() =>
      expect(mocks.saveBlob).toHaveBeenCalledWith(blob, "验收需求清单模板.xlsx"));
  });

  it("缺少 randomUUID 时仍生成幂等键并把上传请求发到预检接口", async () => {
    vi.stubGlobal("crypto", {});
    mocks.getMaintenanceAcceptanceChecklist.mockResolvedValue({
      data: { current: null, history: [] },
    });
    mocks.previewMaintenanceAcceptanceChecklist.mockResolvedValue({
      data: {
        batch_id: "preview-1",
        item_rows: 0,
        done_rows: 0,
        todo_rows: 0,
        issue_rows: 1,
        will_replace_rows: 0,
        issues: ["测试问题行"],
      },
    });
    const { container } = render(<AcceptanceTab projectId="p1" canImport />);
    await screen.findByText(/尚未导入验收清单/);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["xlsx"], "验收.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(mocks.previewMaintenanceAcceptanceChecklist).toHaveBeenCalledTimes(1));
    const [, payload] = mocks.previewMaintenanceAcceptanceChecklist.mock.calls[0];
    expect(payload.file).toBe(file);
    expect(payload.idempotencyKey).toMatch(/^acceptance-checklist-/);
  });
});
