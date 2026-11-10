import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listUnits = vi.fn();
const selfPass = vi.fn();
const sellIntent = vi.fn();

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
  listWorkItems: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 100, can_supervise: false })),
}));

import CyclePoolPage from "../CyclePoolPage";

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
