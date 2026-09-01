import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { message } from "antd";

const searchMaintenanceDemands = vi.fn();
const voidFastMaintenanceDemands = vi.fn();
const restoreMaintenanceDemand = vi.fn();
const uploadWbdd = vi.fn();
const getWbddMissing = vi.fn();

vi.mock("../../../api/maintenanceDemands", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceDemands",
  );
  return {
    ...actual,
    searchMaintenanceDemands: (...args: unknown[]) => searchMaintenanceDemands(...args),
    voidFastMaintenanceDemands: (...args: unknown[]) => voidFastMaintenanceDemands(...args),
    restoreMaintenanceDemand: (...args: unknown[]) => restoreMaintenanceDemand(...args),
  };
});

vi.mock("../../../api/maintenanceWbddImport", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceWbddImport",
  );
  return {
    ...actual,
    uploadWbdd: (...args: unknown[]) => uploadWbdd(...args),
    getWbddMissing: (...args: unknown[]) => getWbddMissing(...args),
  };
});

import MaintenanceDemandsPage from "../MaintenanceDemandsPage";

const BOTH_PERMS = {
  action_maintenance_demand_delete: true,
  action_maintenance_wbdd_import: true,
};

const missingPayload = {
  batch_id: 7,
  uploaded_at: "2026-08-19 09:30",
  window: { from: "2026-08-01", to: "2026-08-19" },
  missing_count: 2,
  truncated: false,
  missing_orders: [
    {
      source_order_id: "RAW-1",
      order_no: "XQD-001",
      order_date: "2026-08-10",
      line_count: 3,
      assigned_project_id: "P-100",
    },
    {
      source_order_id: "RAW-2",
      order_no: "XQD-002",
      order_date: "2026-08-11",
      line_count: 5,
      assigned_project_id: null,
    },
  ],
};

const demandRow = (id: string, no: string, extra: Record<string, unknown> = {}) => ({
  source_order_id: id,
  order_no: no,
  order_date: "2026-08-12",
  project: "项目甲",
  project_raw: "项目甲",
  linked_sales_order_no: "XSDD-1",
  line_count: 2,
  downstream_references: [],
  version_digest: "v1",
  ...extra,
});

const demandPage = (items: unknown[]) => ({
  data: { items, total: items.length, page: 1, page_size: 20 },
});

/** 取最后一次调用的首个入参（tsconfig 的 lib 早于 es2022，没有 Array.at）。 */
function lastArg(mock: { mock: { calls: unknown[][] } }) {
  const calls = mock.mock.calls;
  return calls[calls.length - 1]?.[0];
}

/** 在作废原因弹窗里填原因并确认（antd 两字按钮自动插空格，正则要容忍）。 */
async function confirmVoidWithReason(reason: string) {
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(
    within(dialog).getByPlaceholderText(/作废原因/),
    { target: { value: reason } },
  );
  fireEvent.click(within(dialog).getByRole("button", { name: /确认作废/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  getWbddMissing.mockResolvedValue({ data: missingPayload });
  searchMaintenanceDemands.mockResolvedValue(demandPage([demandRow("RAW-9", "XQD-009")]));
  voidFastMaintenanceDemands.mockResolvedValue({
    data: {
      voided: 1,
      results: [{ source_order_id: "RAW-1", order_no: "XQD-001", status: "voided" }],
    },
  });
  restoreMaintenanceDemand.mockResolvedValue({ data: {} });
  uploadWbdd.mockResolvedValue({
    data: {
      batch_id: 8,
      snapshot_diff: { missing_orders: 2, sample_order_nos: [], window: null },
    },
  });
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("需求单与数据同步页", () => {
  it("渲染两个区块，进入页面即拉差异清单与需求单列表", async () => {
    render(<MaintenanceDemandsPage />);

    expect(await screen.findByText("需求单与数据同步")).toBeInTheDocument();
    expect(screen.getByText("氚云快照同步")).toBeInTheDocument();
    expect(screen.getByText("需求单")).toBeInTheDocument();

    await waitFor(() => expect(getWbddMissing).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(1));
    expect(searchMaintenanceDemands.mock.calls[0][0]).toEqual({
      page: 1,
      page_size: 20,
    });

    // 差异清单内容 + 批次信息
    expect(await screen.findByText("XQD-001")).toBeInTheDocument();
    expect(screen.getByText("XQD-002")).toBeInTheDocument();
    expect(screen.getByText(/批次 #7/)).toBeInTheDocument();
    expect(screen.getByText(/上传于 2026-08-19 09:30/)).toBeInTheDocument();
    // 需求单列表
    expect(screen.getByText("XQD-009")).toBeInTheDocument();
  });

  it("差异清单为空时给出说人话的空态", async () => {
    getWbddMissing.mockResolvedValue({
      data: { ...missingPayload, missing_count: 0, missing_orders: [] },
    });
    render(<MaintenanceDemandsPage />);
    expect(await screen.findByText("最近一份快照没有消失的单")).toBeInTheDocument();
  });

  it("勾选差异清单行 → 填原因 → 批量作废 → 刷新清单与列表", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    render(<MaintenanceDemandsPage />);

    await screen.findByText("XQD-001");
    const batchBtn = screen.getByRole("button", { name: /按氚云现状批量作废/ });
    expect(batchBtn).toBeDisabled();

    // 勾选第一行
    const row = screen.getByText("XQD-001").closest("tr")!;
    fireEvent.click(within(row).getByRole("checkbox"));
    await waitFor(() => expect(batchBtn).toBeEnabled());
    fireEvent.click(batchBtn);

    // 原因必填：空原因时确认按钮不可用
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /确认作废/ })).toBeDisabled();

    await confirmVoidWithReason("氚云侧已删除该单");

    await waitFor(() => expect(voidFastMaintenanceDemands).toHaveBeenCalledTimes(1));
    expect(voidFastMaintenanceDemands.mock.calls[0][0]).toMatchObject({
      source_order_ids: ["RAW-1"],
      reason: "氚云侧已删除该单",
    });
    expect(voidFastMaintenanceDemands.mock.calls[0][0].idempotency_key).toBeTruthy();

    // 成功后差异清单与需求单列表都刷新
    await waitFor(() => expect(getWbddMissing).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
  });

  it("差异清单支持全选后批量作废", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    render(<MaintenanceDemandsPage />);

    await screen.findByText("XQD-001");
    // 表头全选框
    const table = screen.getByText("XQD-001").closest("table")!;
    fireEvent.click(within(table).getAllByRole("checkbox")[0]);

    fireEvent.click(screen.getByRole("button", { name: /按氚云现状批量作废/ }));
    await confirmVoidWithReason("整批消失");

    await waitFor(() => expect(voidFastMaintenanceDemands).toHaveBeenCalledTimes(1));
    expect(voidFastMaintenanceDemands.mock.calls[0][0].source_order_ids).toEqual([
      "RAW-1",
      "RAW-2",
    ]);
  });

  it("行内作废：填原因 → void-fast 单张 → 刷新", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    render(<MaintenanceDemandsPage />);

    await screen.findByText("XQD-009");
    fireEvent.click(screen.getByRole("button", { name: /^作\s*废$/ }));
    await confirmVoidWithReason("重复导入");

    await waitFor(() => expect(voidFastMaintenanceDemands).toHaveBeenCalledTimes(1));
    expect(voidFastMaintenanceDemands.mock.calls[0][0]).toMatchObject({
      source_order_ids: ["RAW-9"],
      reason: "重复导入",
    });
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
  });

  it("作废已提交但列表读回失败时清空旧行，只提示写成功但刷新失败", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    searchMaintenanceDemands
      .mockResolvedValueOnce(demandPage([demandRow("RAW-9", "XQD-009")]))
      .mockRejectedValueOnce(new Error("readback failed"));
    render(<MaintenanceDemandsPage />);

    await screen.findByText("XQD-009");
    fireEvent.click(screen.getByRole("button", { name: /^作\s*废$/ }));
    await confirmVoidWithReason("重复导入");

    expect(await screen.findByText(/已作废 1 张需求单，但页面刷新失败/))
      .toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("XQD-009")).toBeNull());
    expect(screen.queryByText(/已作废 1 张需求单，页面已刷新/)).toBeNull();
  });

  it("作废行灰显并给「恢复」入口；恢复需填原因（后端强制 reason 非空 + admin）", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    localStorage.setItem("role", "admin");
    searchMaintenanceDemands.mockResolvedValue(
      demandPage([demandRow("RAW-9", "XQD-009", { is_voided: true })]),
    );
    render(<MaintenanceDemandsPage />);

    expect(await screen.findByText("已作废")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^作\s*废$/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^恢\s*复$/ }));
    // 弹窗填原因前「确认恢复」不可用
    const confirm = await screen.findByRole("button", { name: /确认恢复/ });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText(/恢复原因/), {
      target: { value: "误作废，氚云侧单仍然有效" },
    });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(restoreMaintenanceDemand).toHaveBeenCalledWith(
        "RAW-9",
        "误作废，氚云侧单仍然有效",
      ));
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
  });

  it("恢复已提交但列表读回失败时清空旧作废行并明确提示", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    localStorage.setItem("role", "admin");
    searchMaintenanceDemands
      .mockResolvedValueOnce(
        demandPage([demandRow("RAW-9", "XQD-009", { is_voided: true })]),
      )
      .mockRejectedValueOnce(new Error("readback failed"));
    render(<MaintenanceDemandsPage />);

    await screen.findByText("已作废");
    fireEvent.click(screen.getByRole("button", { name: /^恢\s*复$/ }));
    fireEvent.change(screen.getByPlaceholderText(/恢复原因/), {
      target: { value: "误作废" },
    });
    fireEvent.click(screen.getByRole("button", { name: /确认恢复/ }));

    expect(await screen.findByText(/已恢复需求单 XQD-009，但页面刷新失败/))
      .toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("XQD-009")).toBeNull());
    expect(screen.queryByText(/已恢复需求单 XQD-009，页面已刷新/)).toBeNull();
  });

  it("非 admin 看不到「恢复」入口（后端 require_admin，给了也只会 403）", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    localStorage.setItem("role", "user");
    searchMaintenanceDemands.mockResolvedValue(
      demandPage([demandRow("RAW-9", "XQD-009", { is_voided: true })]),
    );
    render(<MaintenanceDemandsPage />);

    expect(await screen.findByText("已作废")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^恢\s*复$/ })).toBeNull();
  });

  it("「含已作废」开关触发 include_voided=true 的重新查询", async () => {
    render(<MaintenanceDemandsPage />);
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(1));
    expect(searchMaintenanceDemands.mock.calls[0][0]).not.toHaveProperty("include_voided");

    fireEvent.click(screen.getByRole("switch"));
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
    expect(lastArg(searchMaintenanceDemands)).toEqual({
      page: 1,
      page_size: 20,
      include_voided: true,
    });
  });

  it("较早的查询晚到时不能覆盖最新筛选结果", async () => {
    let resolveOld!: (value: unknown) => void;
    searchMaintenanceDemands
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(demandPage([demandRow("RAW-NEW", "XQD-NEW")]));
    render(<MaintenanceDemandsPage />);

    fireEvent.click(screen.getByRole("switch"));
    expect(await screen.findByText("XQD-NEW")).toBeInTheDocument();
    await act(async () => {
      resolveOld(demandPage([demandRow("RAW-OLD", "XQD-OLD")]));
    });
    expect(screen.getByText("XQD-NEW")).toBeInTheDocument();
    expect(screen.queryByText("XQD-OLD")).toBeNull();
  });

  it("上传氚云快照成功后同时刷新差异清单与需求单列表", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    const { container } = render(<MaintenanceDemandsPage />);
    await waitFor(() => expect(getWbddMissing).toHaveBeenCalledTimes(1));

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: {
        files: [new File(["xlsx"], "wbdd.xlsx", { type: "application/vnd.ms-excel" })],
      },
    });

    await waitFor(() => expect(uploadWbdd).toHaveBeenCalledTimes(1));
    expect(uploadWbdd.mock.calls[0][1]).toBeTruthy(); // 幂等键
    await waitFor(() => expect(getWbddMissing).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
  });

  it("快照已同步但需求单读回失败时清空旧列表，不误报上传失败", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    searchMaintenanceDemands
      .mockResolvedValueOnce(demandPage([demandRow("RAW-9", "XQD-009")]))
      .mockRejectedValueOnce(new Error("readback failed"));
    const { container } = render(<MaintenanceDemandsPage />);
    await screen.findByText("XQD-009");
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, {
      target: { files: [new File(["xlsx"], "wbdd.xlsx")] },
    });

    expect(await screen.findByText(/快照已同步（批次 #8）.*但页面刷新失败/))
      .toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("XQD-009")).toBeNull());
    expect(screen.queryByText("快照上传失败")).toBeNull();
  });

  it("上传期间切换筛选，成功后按最新筛选刷新", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    let resolveUpload!: (value: unknown) => void;
    uploadWbdd.mockImplementationOnce(() => new Promise((resolve) => { resolveUpload = resolve; }));
    const { container } = render(<MaintenanceDemandsPage />);
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(1));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(["xlsx"], "wbdd.xlsx")] },
    });
    await waitFor(() => expect(uploadWbdd).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("switch"));
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveUpload({
        data: { batch_id: 8, snapshot_diff: { missing_orders: 0 } },
      });
    });

    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(3));
    expect(lastArg(searchMaintenanceDemands)).toMatchObject({
      page: 1,
      include_voided: true,
    });
  });

  it("重算忙后重传同一文件复用原幂等键，允许后端补跑成本", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    uploadWbdd
      .mockRejectedValueOnce({
        response: { data: { detail: { code: "recompute_busy", message: "成本重算进行中" } } },
      })
      .mockResolvedValueOnce({
        data: { batch_id: 8, replayed: true, snapshot_diff: { missing_orders: 0 } },
      });
    const { container } = render(<MaintenanceDemandsPage />);
    await waitFor(() => expect(getWbddMissing).toHaveBeenCalledTimes(1));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["xlsx"], "wbdd.xlsx", {
      type: "application/vnd.ms-excel",
      lastModified: 1234,
    });

    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(uploadWbdd).toHaveBeenCalledTimes(1));
    const retryInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(retryInput, { target: { files: [file] } });
    await waitFor(() => expect(uploadWbdd).toHaveBeenCalledTimes(2));

    expect(uploadWbdd.mock.calls[1][1]).toBe(uploadWbdd.mock.calls[0][1]);
    await waitFor(() => expect(searchMaintenanceDemands).toHaveBeenCalledTimes(2));
  });

  it("浏览器缺少 crypto.randomUUID 时仍会上传 WBDD 并生成幂等键", async () => {
    localStorage.setItem("permissions", JSON.stringify(BOTH_PERMS));
    vi.stubGlobal("crypto", undefined);
    const { container } = render(<MaintenanceDemandsPage />);
    await waitFor(() => expect(getWbddMissing).toHaveBeenCalledTimes(1));

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: {
        files: [new File(["xlsx"], "wbdd.xlsx", { type: "application/vnd.ms-excel" })],
      },
    });

    await waitFor(() => expect(uploadWbdd).toHaveBeenCalledTimes(1));
    expect(uploadWbdd.mock.calls[0][1]).toMatch(/^wbdd-upload-\d+-[0-9a-f]+$/);
  });

  it("无动作权限时隐藏上传与作废入口", async () => {
    render(<MaintenanceDemandsPage />);
    await screen.findByText("XQD-001");
    await screen.findByText("XQD-009");

    expect(screen.queryByRole("button", { name: /上传氚云需求单快照/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /按氚云现状批量作废/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^作\s*废$/ })).toBeNull();
    // 无作废权限时差异清单也不给勾选框
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });
});
