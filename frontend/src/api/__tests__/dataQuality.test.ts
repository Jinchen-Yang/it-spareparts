import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();

vi.mock("../../api", () => ({
  default: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}));

import {
  decideDataQualityIssue, getDataQualityIssue, listDataQualityIssues, reopenDataQualityIssue,
} from "../dataQuality";

const wire = {
  id: 1, status: "open", side: "purchase", line_id: 10, part_id: 5,
  import_batch_id: 9, rule_code: "unit_price_outlier", rule_version: "1",
  source_fingerprint: "fp", detected_by: "system", detected_at: "2026-07-16T00:00:00Z",
  reviewed_by: null, reviewed_at: null, review_note: null, version: 1,
  created_at: "2026-07-16T00:00:00Z", updated_at: "2026-07-16T00:00:00Z",
  fact: {
    order_id: 3, order_no: "CG-1", order_date: "2026-07-15", purchaser: "采购甲",
    salesperson: null, part_id: 5, pn_std: "PN-1", description: "硬盘",
    qty: 1, unit: "块", unit_price: 100, line_amount: 100,
    batch: { id: 9, filename: "采购.xlsx", file_type: "purchase", uploaded_by: "数据员",
      uploaded_at: "2026-07-15T18:00:00Z" },
  },
};

beforeEach(() => vi.clearAllMocks());

describe("data quality API contract", () => {
  it("清单筛选只通过唯一客户端下发，并解包分页响应", async () => {
    const page = { total: 1, page: 2, page_size: 20, items: [wire], price_restricted: false };
    get.mockResolvedValue({ data: page });
    const params = { status: "open" as const, side: "purchase" as const, q: "CG-1", page: 2, page_size: 20 };

    await expect(listDataQualityIssues(params)).resolves.toMatchObject({
      total: 1, page: 2, page_size: 20,
      items: [{ order_no: "CG-1", pn_std: "PN-1", handler: "采购甲", quantity: 1,
        unit_price: 100, import_batch_name: "采购.xlsx", price_restricted: false }],
    });
    expect(get).toHaveBeenCalledWith("/data-quality/issues", { params });
  });

  it("详情、决策和重新打开使用稳定逐条路径与 version 乐观锁", async () => {
    const detail = {
      ...wire,
      evidence: { unit_price: "100.00", median: "80.00" },
      audit: [{ action: "create", before: null, after: null, reason: null,
        operated_by: "system", operated_at: "2026-07-16T00:00:00Z" }],
      price_restricted: false,
      evidence_restricted: false,
    };
    get.mockResolvedValue({ data: detail });
    post.mockResolvedValue({ data: { id: 1, status: "confirmed_valid", version: 2 } });

    await expect(getDataQualityIssue(1)).resolves.toMatchObject({
      order_no: "CG-1", handler: "采购甲", evidence: { unit_price: "100.00" },
      batch: { filename: "采购.xlsx", imported_by: "数据员" },
      audits: [{ username: "system", action: "create" }],
    });
    expect(get).toHaveBeenCalledWith("/data-quality/issues/1");

    const decision = { decision: "confirmed_valid" as const, version: 1, note: "原单核实无误" };
    await decideDataQualityIssue(1, decision);
    expect(post).toHaveBeenNthCalledWith(1, "/data-quality/issues/1/decision", decision);
    expect(get).toHaveBeenNthCalledWith(2, "/data-quality/issues/1");

    const reopen = { version: 2, note: "收到新凭证" };
    await reopenDataQualityIssue(1, reopen);
    expect(post).toHaveBeenNthCalledWith(2, "/data-quality/issues/1/reopen", reopen);
    expect(get).toHaveBeenNthCalledWith(3, "/data-quality/issues/1");
  });

  it("evidence=null 与 audit 单数键正确收口，受限价格不能经清单或详情回流", async () => {
    get.mockResolvedValueOnce({ data: {
      total: 1, page: 1, page_size: 20, price_restricted: true,
      items: [{ ...wire, fact: { ...wire.fact, unit_price: undefined, line_amount: undefined } }],
    } });
    const page = await listDataQualityIssues({ page: 1 });
    expect(page.items[0]).toMatchObject({ unit_price: null, price_restricted: true });

    get.mockResolvedValueOnce({ data: {
      ...wire,
      fact: { ...wire.fact, unit_price: undefined, line_amount: undefined },
      evidence: null,
      audit: [{ action: "decision", before: null, after: null, reason: "已核实",
        operated_by: "复核员", operated_at: "2026-07-16T01:00:00Z" }],
      price_restricted: true,
      evidence_restricted: true,
    } });
    const detail = await getDataQualityIssue(1);
    expect(detail.price_restricted).toBe(true);
    expect(detail.evidence_restricted).toBe(true);
    expect(detail.evidence).toEqual({});
    expect(detail.fact.unit_price).toBeNull();
    expect(detail.fact.line_amount).toBeNull();
    expect(detail.audits[0]).toMatchObject({ username: "复核员", note: "已核实" });
  });
});
