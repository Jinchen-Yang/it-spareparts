import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api: { get, post } }));

import {
  MAINTENANCE_BATCH_TRANSFER_BASE,
  normalizeMaintenanceReceiptRuling,
  ruleMaintenanceReceiptConflict,
} from "../maintenanceBatchTransfer";

beforeEach(() => vi.clearAllMocks());

const rulingBody = {
  contract_no: "20240101-0001",
  receipt_no: "SK-9",
  receipt_date: "2026-01-10",
  actual_amount: "120.00",
  reason: "已核对银行回单，以本文件为准",
};

describe("台账冲突人工裁决客户端（D-16 / REQ #56 #57）", () => {
  it("回执按契约校验：id 统一成字符串，累计允许字符串 / 数字 / null", () => {
    expect(normalizeMaintenanceReceiptRuling({
      ruling_id: 7,
      superseded_receipt_id: "9",
      new_receipt_id: 10,
      affected_months: [
        { report_month: "2026-01-01", current_cumulative: "100.00", derived_cumulative: 120 },
        { report_month: "2026-02-01", current_cumulative: null, derived_cumulative: "120.00" },
      ],
    })).toEqual({
      ruling_id: "7",
      superseded_receipt_id: "9",
      new_receipt_id: "10",
      affected_months: [
        { report_month: "2026-01-01", current_cumulative: "100.00", derived_cumulative: "120" },
        { report_month: "2026-02-01", current_cumulative: null, derived_cumulative: "120.00" },
      ],
    });
  });

  it("形状不对一律返回 null：缺 id、月份缺 report_month、累计是对象", () => {
    expect(normalizeMaintenanceReceiptRuling(null)).toBeNull();
    expect(normalizeMaintenanceReceiptRuling({ ruling_id: "1", affected_months: [] })).toBeNull();
    expect(normalizeMaintenanceReceiptRuling({
      ruling_id: "1", superseded_receipt_id: "2", new_receipt_id: "3", affected_months: [{ current_cumulative: "1" }],
    })).toBeNull();
    expect(normalizeMaintenanceReceiptRuling({
      ruling_id: "1", superseded_receipt_id: "2", new_receipt_id: "3",
      affected_months: [{ report_month: "2026-01-01", current_cumulative: {}, derived_cumulative: null }],
    })).toBeNull();
  });

  it("POST /receipt-rulings 原样提交裁决体，回执合法才返回", async () => {
    post.mockResolvedValue({
      data: { ruling_id: "r-1", superseded_receipt_id: "9", new_receipt_id: "10", affected_months: [] },
    });
    await expect(ruleMaintenanceReceiptConflict(rulingBody)).resolves.toEqual({
      ruling_id: "r-1", superseded_receipt_id: "9", new_receipt_id: "10", affected_months: [],
    });
    expect(post).toHaveBeenCalledWith(`${MAINTENANCE_BATCH_TRANSFER_BASE}/receipt-rulings`, rulingBody);
  });

  it("回执不合法时抛错，不把半截结果当成裁决成功", async () => {
    post.mockResolvedValue({ data: { ok: true } });
    await expect(ruleMaintenanceReceiptConflict(rulingBody)).rejects.toThrow("裁决回执格式无法识别");
  });
});
