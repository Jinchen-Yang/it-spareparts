import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();

vi.mock("../../api", () => ({
  default: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}));

import {
  decideDataQualityIssue, getDataQualityIssue, listDataQualityIssues, reopenDataQualityIssue,
} from "../dataQuality";

const item = {
  id: 1, status: "open", side: "purchase", order_date: null, order_no: "CG-1",
  pn_std: "PN-1", handler: "采购甲", quantity: 1, unit: "块", unit_price: 100,
  rule_code: "unit_price_outlier", rule_label: "价格待核实", import_batch_id: 9,
  import_batch_name: "采购.xlsx", updated_at: "2026-07-16T00:00:00Z", version: 1,
};

beforeEach(() => vi.clearAllMocks());

describe("data quality API contract", () => {
  it("清单筛选只通过唯一客户端下发，并解包分页响应", async () => {
    const page = { total: 1, page: 2, page_size: 20, items: [item] };
    get.mockResolvedValue({ data: page });
    const params = { status: "open" as const, side: "purchase" as const, q: "CG-1", page: 2, page_size: 20 };

    await expect(listDataQualityIssues(params)).resolves.toEqual(page);
    expect(get).toHaveBeenCalledWith("/data-quality/issues", { params });
  });

  it("详情、决策和重新打开使用稳定逐条路径与 version 乐观锁", async () => {
    const detail = { ...item, fact: {}, evidence: {}, order: {}, batch: null, audits: [] };
    get.mockResolvedValue({ data: detail });
    post.mockResolvedValue({ data: detail });

    await expect(getDataQualityIssue(1)).resolves.toEqual(detail);
    expect(get).toHaveBeenCalledWith("/data-quality/issues/1");

    const decision = { decision: "confirmed_valid" as const, version: 1, note: "原单核实无误" };
    await decideDataQualityIssue(1, decision);
    expect(post).toHaveBeenNthCalledWith(1, "/data-quality/issues/1/decision", decision);

    const reopen = { version: 2, note: "收到新凭证" };
    await reopenDataQualityIssue(1, reopen);
    expect(post).toHaveBeenNthCalledWith(2, "/data-quality/issues/1/reopen", reopen);
  });
});
