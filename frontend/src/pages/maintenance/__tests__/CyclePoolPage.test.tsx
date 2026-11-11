import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listUnits = vi.fn();
const selfPass = vi.fn();
const sellIntent = vi.fn();
const listWorkItems = vi.fn();

vi.mock("../../../api/cyclePool", () => ({
  listUnits: (...args: unknown[]) => listUnits(...args),
  selfPass: (...args: unknown[]) => selfPass(...args),
  sellIntent: (...args: unknown[]) => sellIntent(...args),
  wipCounts: vi.fn(),
  intakeExternal: vi.fn(),
  handoffFromReceipt: vi.fn(),
  dispatchUnit: vi.fn(),
  completeTest: vi.fn(),
  completeRepair: vi.fn(),
  outsourceReturn: vi.fn(),
  closeUnit: vi.fn(),
  withdrawUnit: vi.fn(),
  claimUnit: vi.fn(),
  markSold: vi.fn(),
}));

vi.mock("../../../api/workItems", () => ({
  listWorkItems: (...args: unknown[]) => listWorkItems(...args),
}));

import CyclePoolPage, { resultChoices } from "../CyclePoolPage";

const unit = {
  id: 7,
  unit_uid: "uid",
  part_id: 3,
  pn: "CYCLE-PN",
  station: "pool",
  outcome: null,
  condition_bucket: null,
  intake_condition: null,
  repair_round: 0,
  assignee_username: null,
  version: 3,
  created_at: "2026-11-10T00:00:00Z",
  updated_at: "2026-11-10T00:00:00Z",
  closed_at: null,
  serial_number: "SN-HIDDEN",
  vendor_name: "VENDOR",
};

describe("维修循环工位页", () => {
  beforeEach(() => {
    listUnits.mockResolvedValue({ items: [unit], total: 1, page: 1, page_size: 20 });
    selfPass.mockResolvedValue(unit);
    sellIntent.mockResolvedValue({ unit_id: 7, status: "blocked", blockCode: "missing_list_price" });
    listWorkItems.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100, can_supervise: false });
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("回收岗可以自测，表格不出现序列号", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_cycle_recycle: true }));
    render(<CyclePoolPage />);
    expect(await screen.findByText("CYCLE-PN")).toBeTruthy();
    expect(screen.queryByText("序列号")).toBeNull();
    expect(screen.queryByText("SN-HIDDEN")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "处理" }));
    fireEvent.click(await screen.findByRole("button", { name: "自测通过" }));
    await waitFor(() => expect(selfPass).toHaveBeenCalledWith(7, 3));
    expect(screen.queryByRole("button", { name: "标记售出" })).toBeNull();
  });

  it("待办超过第一页时仍能领取当前这件", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_cycle_test: true }));
    listUnits.mockResolvedValue({
      items: [{ ...unit, station: "testing" }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    listWorkItems.mockImplementation(async (params: { page?: number }) => {
      if ((params.page ?? 1) === 1) {
        return {
          items: Array.from({ length: 100 }, (_, index) => ({ subject_id: 1000 + index, version: 1 })),
          total: 101,
          page: 1,
          page_size: 100,
          can_supervise: false,
        };
      }
      return {
        items: [{ subject_id: 7, version: 4 }],
        total: 101,
        page: 2,
        page_size: 100,
        can_supervise: false,
      };
    });
    render(<CyclePoolPage />);
    expect(await screen.findByText("CYCLE-PN")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "处理" }));
    const claim = await screen.findByRole("button", { name: "领 取" });
    expect(claim.hasAttribute("disabled")).toBe(false);
    expect(listWorkItems).toHaveBeenCalledTimes(2);
  });

  it("只有主管权限时不提供领取，也不去拉待办", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_cycle_manage: true }));
    listUnits.mockResolvedValue({
      items: [{ ...unit, station: "testing" }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    render(<CyclePoolPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "检测" }));
    expect(await screen.findByText("CYCLE-PN")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "处理" }));
    expect(await screen.findByText("版本 3")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "领 取" })).toBeNull();
    expect(listWorkItems).not.toHaveBeenCalled();
  });

  it("待办版本失败时单位列表还在，并说明暂时不能领取", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_cycle_test: true }));
    listUnits.mockResolvedValue({
      items: [{ ...unit, station: "testing" }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    listWorkItems.mockRejectedValue({ response: { data: { detail: "待办服务不可用" } } });
    render(<CyclePoolPage />);
    expect(await screen.findByText("CYCLE-PN")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "处理" }));
    expect(await screen.findByText(/暂时不能领取：待办服务不可用/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "领 取" })).toBeNull();
  });

  it("结果只对应接口接受的去向", () => {
    expect(resultChoices("test", "good").options.map((item) => item.value)).toEqual(["ready"]);
    expect(resultChoices("test", "bad").options.map((item) => item.value)).toEqual(["repairing", "scrap"]);
    expect(resultChoices("repair", "fixed").required).toBe(true);
    expect(resultChoices("repair", "fixed").options.map((item) => item.value)).toEqual(["testing", "ready"]);
    expect(resultChoices("repair", "unfixed").options.map((item) => item.value)).toEqual(["scrap"]);
  });

  it("主管能看投递状态", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_cycle_manage: true }));
    render(<CyclePoolPage />);
    expect(await screen.findByText("序列号")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "处理" }));
    fireEvent.click(await screen.findByRole("button", { name: "投递状态" }));
    await waitFor(() => expect(sellIntent).toHaveBeenCalledWith(7));
    expect(await screen.findByText(/missing_list_price/)).toBeTruthy();
  });
});
