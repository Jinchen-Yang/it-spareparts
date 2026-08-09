import { beforeEach, describe, expect, it, vi } from "vitest";

const { post, get } = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));

vi.mock("../../api", () => ({ api: { post, get } }));

import {
  armMaintenanceDemandDeleteIntent,
  createMaintenanceDemandDeleteIntent,
  executeMaintenanceDemandDeleteIntent,
  searchMaintenanceDemands,
} from "../maintenanceDemands";

describe("maintenance demand API contract", () => {
  beforeEach(() => vi.clearAllMocks());

  it("search terms and pagination travel only in the POST JSON body", () => {
    searchMaintenanceDemands({ q: "PN-123", page: 3, page_size: 25 });
    expect(post).toHaveBeenCalledWith("/maintenance/demands/search", {
      q: "PN-123",
      page: 3,
      page_size: 25,
    });
  });

  it("uses separate intent, arm and execute endpoints", () => {
    createMaintenanceDemandDeleteIntent({
      source_order_ids: ["RAW-1"],
      reason: "重复导入",
      idempotency_key: "delete-intent-key-1",
    });
    armMaintenanceDemandDeleteIntent("intent-1", "a".repeat(64));
    executeMaintenanceDemandDeleteIntent("intent-1", "a".repeat(64));

    expect(post).toHaveBeenNthCalledWith(1, "/maintenance/demands/delete-intents", {
      source_order_ids: ["RAW-1"],
      reason: "重复导入",
      idempotency_key: "delete-intent-key-1",
    });
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/maintenance/demands/delete-intents/intent-1/arm",
      { digest: "a".repeat(64) },
    );
    expect(post).toHaveBeenNthCalledWith(
      3,
      "/maintenance/demands/delete-intents/intent-1/execute",
      { digest: "a".repeat(64) },
    );
    expect(get).not.toHaveBeenCalled();
  });
});
