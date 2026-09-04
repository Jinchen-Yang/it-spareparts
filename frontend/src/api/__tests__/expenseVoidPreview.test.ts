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
  it("accepts server-masked (null) amounts — 无成本可见权限的账号也能走完预演", () => {
    const p = normalizeExpenseVoidPreview({
      ...ready, void: { ...ready.void, amount: null },
      void_rows: [{ ...ready.void_rows[0], amount: null }],
    });
    expect(p?.status).toBe("ready");
    expect(p?.void?.amount).toBeNull();
    expect(p?.void_rows[0].amount).toBeNull();
  });
  it("too_large carries the summary but no token and no rows", () => {
    const p = normalizeExpenseVoidPreview({
      filename: "page.xlsx", status: "too_large", contract: "XSDD-P",
      void: { rows: 6000, amount: "1.00", already_void_rows: 0 }, row_cap: 5000,
    });
    expect(p?.status).toBe("too_large");
    expect(p?.void?.rows).toBe(6000);
    expect(p?.row_cap).toBe(5000);
    expect(p?.preview_token).toBeNull();
  });
  it("suppressed previews need no token", () => {
    const p = normalizeExpenseVoidPreview({ filename: "x.xlsx", status: "suppressed", reason: "multi_contract" });
    expect(p?.status).toBe("suppressed");
    expect(p?.preview_token).toBeNull();
  });
});
