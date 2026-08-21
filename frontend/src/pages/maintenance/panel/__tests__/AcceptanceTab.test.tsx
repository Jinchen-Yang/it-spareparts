import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getMaintenanceAcceptanceChecklist: vi.fn(),
  downloadAcceptanceChecklistTemplate: vi.fn(),
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
  };
});

import AcceptanceTab from "../AcceptanceTab";

beforeEach(() => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation((key: string) => {
    if (key === "role") return "admin";
    if (key === "permissions") return "{}";
    return null;
  });
});

afterEach(() => {
  cleanup();
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
});
