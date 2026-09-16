import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api: { get, post } }));

import { getBoardProjectOrders } from "../maintenanceBossBoard";
import { searchMaintenanceDemands } from "../maintenanceDemands";
import type { MaintenanceOrderContact } from "../maintenanceOrderContact";

beforeEach(() => vi.clearAllMocks());

describe("contact read API clients", () => {
  it.each(["visible", "restricted"] as const)("preserves %s contact fields on both endpoints", async (state) => {
    const contact: MaintenanceOrderContact = {
      contact_info_state: state,
      receiver: state === "visible" ? "合成联系人" : null,
      receiver_address: state === "visible" ? "合成长地址".repeat(80) : null,
      receiver_phone: state === "visible" ? "00123456789" : null,
    };
    const row = { source_order_id: "RAW-1", ...contact };
    get.mockResolvedValue({ data: { rows: [row], total: 1 } });
    post.mockResolvedValue({ data: { items: [row], total: 1 } });
    const params = { page: 1, page_size: 20 };
    const board = await getBoardProjectOrders("project/1", params);
    const search = await searchMaintenanceDemands(params);
    expect(get).toHaveBeenCalledWith("/maintenance/boss-board/projects/project%2F1/orders", { params });
    expect(post).toHaveBeenCalledWith("/maintenance/demands/search", params);
    expect(board.data.rows[0]).toEqual(row);
    expect(search.data.items[0]).toEqual(row);
  });
});
