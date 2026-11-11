import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("../../api", () => ({ api: mocks }));

import {
  attestShopAdjustment,
  getRepairRecord,
  getRepairSellIntent,
  listRepairPool,
  listRepairRecords,
  listRepairSummary,
  submitRepairRecord,
  voidRepairRecord,
} from "../cyclePool";

beforeEach(() => {
  mocks.get.mockReset();
  mocks.post.mockReset();
  mocks.get.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } });
  mocks.post.mockResolvedValue({ data: { record_id: "rec-1" } });
});

describe("待修池接口", () => {
  it("待修、记录和采购三数走契约路径，空搜索不传 q", async () => {
    await listRepairPool({ q: "MZ7", page: 2, page_size: 20 });
    expect(mocks.get).toHaveBeenCalledWith("/cycle/pool", {
      params: { q: "MZ7", page: 2, page_size: 20 },
    });
    await listRepairPool({ q: "", page: 1, page_size: 20 });
    expect(mocks.get).toHaveBeenLastCalledWith("/cycle/pool", {
      params: { page: 1, page_size: 20 },
    });
    await listRepairRecords({ status: "voided", part_id: 9, q: "AB", page: 1, page_size: 20 });
    expect(mocks.get).toHaveBeenLastCalledWith("/cycle/repair-records", {
      params: { status: "voided", part_id: 9, q: "AB", page: 1, page_size: 20 },
    });
    await listRepairSummary({ part_ids: "1,2", page: 1, page_size: 20 });
    expect(mocks.get).toHaveBeenLastCalledWith("/cycle/summary", {
      params: { part_ids: "1,2", page: 1, page_size: 20 },
    });
  });

  it("提交、作废和登记减库存带 Idempotency-Key，路径里的记录号会转义", async () => {
    await submitRepairRecord({ part_id: 101, fixed_qty: 2, scrap_qty: 1 }, "submit-key");
    expect(mocks.post).toHaveBeenCalledWith(
      "/cycle/repair-records",
      { part_id: 101, fixed_qty: 2, scrap_qty: 1 },
      { headers: { "Idempotency-Key": "submit-key" } },
    );
    await voidRepairRecord(
      "rec/1",
      { expected_version: 1, reason: "填反了", shop_stock_manually_reduced: false },
      "void-key",
    );
    expect(mocks.post).toHaveBeenLastCalledWith(
      "/cycle/repair-records/rec%2F1/void",
      { expected_version: 1, reason: "填反了", shop_stock_manually_reduced: false },
      { headers: { "Idempotency-Key": "void-key" } },
    );
    await getRepairRecord("rec/1");
    expect(mocks.get).toHaveBeenLastCalledWith("/cycle/repair-records/rec%2F1");
    await getRepairSellIntent("rec/1");
    expect(mocks.get).toHaveBeenLastCalledWith("/cycle/repair-records/rec%2F1/sell-intent");
    await attestShopAdjustment("rec/1", { expected_version: 2 }, "attest-key");
    expect(mocks.post).toHaveBeenLastCalledWith(
      "/cycle/repair-records/rec%2F1/attest-shop-adjustment",
      { expected_version: 2 },
      { headers: { "Idempotency-Key": "attest-key" } },
    );
  });
});
