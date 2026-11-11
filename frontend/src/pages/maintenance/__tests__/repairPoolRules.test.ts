import { describe, expect, it } from "vitest";

import {
  SHOP_STOCK_FORBIDDEN_DETAIL,
  SHOP_STOCK_REQUIRED_DETAIL,
  commandOutcome,
  createdOnBusinessToday,
  detailOf,
  nextCommandAttempt,
  repairQtyError,
  settleCommandAttempt,
  shopStockCheck,
  visibleShopStockCheck,
  voidReasonError,
  type ShopStockIntent,
} from "../repairPoolRules";

function intent(patch: Partial<ShopStockIntent> & Pick<ShopStockIntent, "status" | "attempts">): ShopStockIntent {
  return {
    block_code: null,
    last_error_code: null,
    receipt_status: null,
    ...patch,
  };
}

describe("商城已手工减库存", () => {
  it("没有推送、刚记下的待发送、以及确定没加库存时不勾选", () => {
    expect(shopStockCheck(null)).toBe("omit");
    expect(shopStockCheck(undefined)).toBe("omit");
    expect(shopStockCheck(intent({ status: "pending", attempts: 0 }))).toBe("omit");
    for (const block of [
      "missing_catalog_identity",
      "missing_formal_image",
      "missing_category_mapping",
      "missing_list_price",
      "publish_api_missing_stock_update",
    ]) {
      expect(shopStockCheck(intent({
        status: "pending",
        attempts: 3,
        receipt_status: "blocked",
        block_code: block,
      }))).toBe("omit");
    }
    expect(shopStockCheck(intent({
      status: "pending",
      attempts: 2,
      last_error_code: "remote_busy",
    }))).toBe("omit");
    expect(shopStockCheck(intent({
      status: "failed",
      attempts: 1,
      last_error_code: "conflict",
    }))).toBe("omit");
    expect(shopStockCheck(intent({
      status: "delivering",
      attempts: 1,
    }))).toBe("omit");
  });

  it("已经入账，或应答可能丢失时必须勾选", () => {
    expect(shopStockCheck(intent({ status: "delivered", attempts: 1 }))).toBe("required");
    expect(shopStockCheck(intent({ status: "pending", attempts: 0, receipt_status: "applied" }))).toBe("required");
    expect(shopStockCheck(intent({ status: "pending", attempts: 1, receipt_status: "unchanged" }))).toBe("required");
    expect(shopStockCheck(intent({
      status: "blocked",
      attempts: 1,
      block_code: "publish_disabled",
    }))).toBe("required");
    expect(shopStockCheck(intent({ status: "pending", attempts: 2 }))).toBe("required");
    expect(shopStockCheck(intent({
      status: "pending",
      attempts: 2,
      last_error_code: "remote_busy",
      receipt_status: "applied",
    }))).toBe("required");
    expect(shopStockCheck(intent({
      status: "failed",
      attempts: 1,
      receipt_status: "applied",
    }))).toBe("required");
    expect(shopStockCheck(intent({
      status: "delivering",
      attempts: 1,
      receipt_status: "unchanged",
    }))).toBe("required");
  });

  it("missing_operator 作废时禁止勾选，即使已经尝试过", () => {
    expect(shopStockCheck(intent({
      status: "pending",
      attempts: 4,
      block_code: "missing_operator",
      receipt_status: "blocked",
    }))).toBe("forbid");
    expect(shopStockCheck(intent({
      status: "delivered",
      attempts: 1,
      block_code: "missing_operator",
      receipt_status: "applied",
    }))).toBe("forbid");
  });

  it("服务端拒绝文案覆盖本地判断", () => {
    const fresh = intent({ status: "pending", attempts: 0 });
    expect(visibleShopStockCheck(fresh, SHOP_STOCK_REQUIRED_DETAIL)).toBe("required");
    expect(visibleShopStockCheck(fresh, SHOP_STOCK_FORBIDDEN_DETAIL)).toBe("forbid");
    expect(visibleShopStockCheck(fresh, "该记录已作废")).toBe("omit");
  });
});

describe("幂等键", () => {
  const mint = () => "key-new";

  it("同一内容复用键，内容变了才换新键", () => {
    const first = nextCommandAttempt(null, "a", mint);
    expect(first).toEqual({ attempt: { fingerprint: "a", key: "key-new", unknown: false }, blocked: false });
    expect(nextCommandAttempt(first.attempt, "a", () => "other")).toEqual({
      attempt: first.attempt,
      blocked: false,
    });
    expect(nextCommandAttempt(first.attempt, "b", () => "other").attempt.key).toBe("other");
  });

  it("结果未知时不许改内容再发", () => {
    const unknown = settleCommandAttempt(
      { fingerprint: "a", key: "kept", unknown: false },
      "unknown",
    );
    expect(nextCommandAttempt(unknown, "b", mint)).toEqual({ attempt: unknown, blocked: true });
    expect(settleCommandAttempt(unknown!, "ok")).toBeNull();
    expect(settleCommandAttempt(unknown!, "rejected")?.unknown).toBe(false);
  });
});

describe("件数、原因和业务日", () => {
  it("合计不得超过待修，且至少一件", () => {
    expect(repairQtyError(2, 1, 3)).toBeNull();
    expect(repairQtyError(2, 1, 2)).toBe("修好数与废品数之和超过当前待修件数");
    expect(repairQtyError(0, 0, 3)).toBe("至少填写一件");
    expect(repairQtyError(1.5, 0, 3)).toBe("修好件数和废品件数须为 0–1000 的整数");
    expect(repairQtyError(1001, 0, 2000)).toBe("修好件数和废品件数须为 0–1000 的整数");
  });

  it("作废原因去掉空白后 1 到 256 字", () => {
    expect(voidReasonError("  ")).toBe("请填写作废原因");
    expect(voidReasonError("填反了")).toBeNull();
    expect(voidReasonError("啊".repeat(257))).toBe("作废原因最长 256 字");
  });

  it("按上海日期判断是不是当天", () => {
    const noon = new Date("2026-11-11T04:00:00Z");
    expect(createdOnBusinessToday("2026-11-11T15:30:00Z", noon)).toBe(true);
    expect(createdOnBusinessToday("2026-11-10T15:30:00Z", noon)).toBe(false);
    expect(createdOnBusinessToday("2026-11-11T16:00:00Z", new Date("2026-11-11T16:30:00Z"))).toBe(true);
    expect(createdOnBusinessToday("2026-11-11T15:59:59Z", new Date("2026-11-11T16:30:00Z"))).toBe(false);
    expect(createdOnBusinessToday("not-a-date", noon)).toBe(false);
  });
});

describe("错误文案", () => {
  it("只展示字符串 detail", () => {
    expect(detailOf({ response: { data: { detail: "版本冲突：记录已被他人处理，请刷新后重试" } } }))
      .toBe("版本冲突：记录已被他人处理，请刷新后重试");
    expect(detailOf({ response: { data: { detail: [{ msg: "field" }] } } })).toBe("操作没有完成");
    expect(detailOf(new Error("boom"))).toBe("操作没有完成");
  });

  it("4xx 可以换内容，5xx 和断网保持原请求", () => {
    expect(commandOutcome({ response: { status: 409 } })).toBe("rejected");
    expect(commandOutcome({ response: { status: 422 } })).toBe("rejected");
    expect(commandOutcome({ response: { status: 500 } })).toBe("unknown");
    expect(commandOutcome(new Error("network"))).toBe("unknown");
  });
});
