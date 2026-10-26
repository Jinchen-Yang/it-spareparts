import { beforeEach, expect, it, vi } from "vitest";
const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }));
vi.mock("../../api", () => ({ api: http }));
import { presales } from "../presales";
beforeEach(() => {
  Object.values(http).forEach((fn) => fn.mockReset().mockResolvedValue({ data: { id: 1, version: 2 } }));
});

it("使用主键、各主体版本与受众账号字符串传递原始契约，不补隐藏字段", async () => {
  const body = { kind: "sales_inquiry" as const, assignee_ids: [4, 5],
    offer_audience: { mode: "include" as const, user_ids: ["sale-a"], group_ids: [2] },
    lines: [{ part_id: 8, qty: "2.500", ref_offer_id: 9 }, { pn_raw: "待建 PN", field_values: [{ new_name: "期望交期", value: "一周" }] }] };
  expect(await presales.createInquiry(body)).toEqual({ id: 1, version: 2 });
  expect(http.post).toHaveBeenCalledWith("/presales/inquiries", body);
  await presales.bindPart(20, { expected_version: 3, part_id: 8 });
  expect(http.post).toHaveBeenLastCalledWith("/presales/lines/20/bind-part", { expected_version: 3, part_id: 8 });
  await presales.saveOffer(1, 20, { expected_version: 7, offer_id: 9, unit_price: "12.50" });
  expect(http.post).toHaveBeenLastCalledWith("/presales/inquiries/1/lines/20/offers", { expected_version: 7, offer_id: 9, unit_price: "12.50" });
  await presales.updateOffer(9, { expected_version: 8, validity_days: 3 });
  expect(http.patch).toHaveBeenCalledWith("/presales/offers/9", { expected_version: 8, validity_days: 3 });
});

it("撤回、重新提交、追加、复制、关单与撤销调用对应端点", async () => {
  await presales.withdrawInquiry(1, { expected_version: 2 });
  expect(http.post).toHaveBeenLastCalledWith("/presales/inquiries/1/withdraw", { expected_version: 2 });
  await presales.resubmitInquiry(1, { expected_version: 3 });
  expect(http.post).toHaveBeenLastCalledWith("/presales/inquiries/1/resubmit", { expected_version: 3 });
  await presales.appendLines(1, { expected_version: 4, lines: [{ pn_raw: "PN" }] });
  expect(http.post).toHaveBeenLastCalledWith("/presales/inquiries/1/lines", { expected_version: 4, lines: [{ pn_raw: "PN" }] });
  await presales.copyInquiry(1, { expected_version: 5 });
  expect(http.post).toHaveBeenLastCalledWith("/presales/inquiries/1/copy", { expected_version: 5 });
  await presales.closeInquiry(1, { expected_version: 6, outcome: "won", won_offer_id: 9 });
  expect(http.post).toHaveBeenLastCalledWith("/presales/inquiries/1/close", { expected_version: 6, outcome: "won", won_offer_id: 9 });
  await presales.withdrawOffer(9, { expected_version: 8, reason: "报错价格" });
  expect(http.post).toHaveBeenLastCalledWith("/presales/offers/9/withdraw", { expected_version: 8, reason: "报错价格" });
});

it("报价库查询及实时取消参数按接口传递", async () => {
  await presales.library([2, 3]);
  expect(http.get).toHaveBeenCalledWith("/presales/library", { params: { part_ids: "2,3" } });
  const controller = new AbortController();
  await presales.searchLibrary("PN *", controller.signal);
  expect(http.get).toHaveBeenLastCalledWith("/presales/library/search", { params: { q: "PN *" }, signal: controller.signal });
});
