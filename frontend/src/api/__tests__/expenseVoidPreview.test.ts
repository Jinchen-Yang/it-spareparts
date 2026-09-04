import { describe, expect, it } from "vitest";
import { normalizeExpenseVoidPreview } from "../imports";

const ready = {
  filename: "page.xlsx", status: "ready", contract: "XSDD-P", contracts: ["XSDD-P"],
  rows_incoming: 2, dropped_no_contract: 0,
  void: { rows: 1, amount: "1200.00", already_void_rows: 0 },
  void_rows: [{ raw_line_id: "BXD-3#1@abc", linked_sales_order_no: "XSDD-P", bxd_no: "BXD-3",
    line_no: 1, expense_date: "2026-05-03", person: "丙", reason: "租金",
    data_status: "已结束", amount: "1200.00" }],
  void_rows_truncated: false, preview_token: "abc.def",
};

describe("normalizeExpenseVoidPreview", () => {
  it("accepts a ready preview with token and rows", () => {
    const p = normalizeExpenseVoidPreview(ready);
    expect(p?.status).toBe("ready");
    expect(p?.void?.rows).toBe(1);
    expect(p?.void_rows[0].reason).toBe("租金");
    expect(p?.preview_token).toBe("abc.def");
  });
  it("rejects a ready preview without a token — 没有令牌就不能提交", () => {
    expect(normalizeExpenseVoidPreview({ ...ready, preview_token: "" })).toBeNull();
    expect(normalizeExpenseVoidPreview({ ...ready, preview_token: undefined })).toBeNull();
  });
  it("rejects malformed rows rather than guessing", () => {
    expect(normalizeExpenseVoidPreview({ ...ready, void_rows: [{ amount: 1 }] })).toBeNull();
    expect(normalizeExpenseVoidPreview({ ...ready, status: "whatever" })).toBeNull();
  });
  it("keeps a masked amount as an opaque string", () => {
    const p = normalizeExpenseVoidPreview({
      ...ready, void: { ...ready.void, amount: "***" },
      void_rows: [{ ...ready.void_rows[0], amount: "***" }],
    });
    expect(p?.void?.amount).toBe("***");
  });
  it("suppressed previews need no token", () => {
    const p = normalizeExpenseVoidPreview({ filename: "x.xlsx", status: "suppressed", reason: "multi_contract" });
    expect(p?.status).toBe("suppressed");
    expect(p?.preview_token).toBeNull();
  });
});
