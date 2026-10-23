import { legacyAuthorizationState, prepareLegacyPageFixture } from "../../../../test/legacyAuthorization";
vi.mock("../../../../authz/AuthzProvider", async importOriginal => ({...(await importOriginal<typeof import("../../../../authz/AuthzProvider")>()), useAuthz: () => legacyAuthorizationState()}));
function render(...args: Parameters<typeof fixtureRender>) {prepareLegacyPageFixture({"page_purchase_analytics": true}); return fixtureRender(...args);}
import { cleanup, render as fixtureRender, screen, within } from "../../../../test/antdRender";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PurchaseAnalyticsOrderDetail } from "../../../../api/purchaseAnalytics";
import OrderDetailDrawer from "../OrderDetailDrawer";

const fetchDetail = vi.hoisted(() => vi.fn());
vi.mock("../../../../api/purchaseAnalytics", () => ({
  fetchPurchaseAnalyticsOrderDetail: fetchDetail,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// 缺口含税单：单头仅有未税原值，单头名义为 inc；行金额仍是含税原值，行名义为 ex。
const GAP_DETAIL: PurchaseAnalyticsOrderDetail = {
  order_id: 101, order_no: "CG-GAP", order_date: "2026-10-18",
  source_type: "销售订单", purchaser: "采购员", data_status: "已生效",
  is_tax_inclusive: true, tax_rate: 0, tax_rate_effective: 0.13,
  amount_ex: 1000, amount_inc: 1130, amount_paid: null, nominal_side: "inc",
  linked_sales_order_no: null, salesperson: null, customer_id: null, customer: null,
  lines_count: 1,
  lines: [{
    line_id: 1, line_no: 1, part_id: 1, pn_std: "PN-GAP", description: null,
    brand: null, qty: 1, unit_price: 1130, line_amount: 1130, category: "硬盘",
    line_amount_inc: 1130, line_amount_ex: 1000,
  }],
  category_matched: { lines: 1, amount_inc: 1130, amount_ex: 1000, amount_paid: 1130 },
};

async function openDrawer(detail: PurchaseAnalyticsOrderDetail, basis: ("inc" | "ex" | "paid")[] = ["inc", "ex", "paid"]) {
  fetchDetail.mockResolvedValue({ data: detail });
  // 明确提供已认证legacy会话，金额展示测试不依赖缺失Provider。
  render(
    <OrderDetailDrawer
      orderId={101}
      category={["硬盘"]}
      basis={basis}
      ownerToken="legacy-page-fixture-owner"
      onOwnerStale={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  await screen.findByText("采购单 CG-GAP");
  return screen.getByRole("dialog");
}

function expectNote(scope: HTMLElement, side: "inc" | "ex" | null) {
  const notes = within(scope).queryAllByText("名义（按 13% 推算）");
  expect(notes).toHaveLength(side === null ? 0 : 1);
  if (side !== null) {
    expect(notes[0]).toHaveAttribute("aria-label", side === "inc" ? "名义含税说明" : "名义不含税说明");
  }
}

describe("D-39 抽屉：单头缺列翻转与行级结构性名义侧分别展示", () => {
  it.each([
    [true, 0, "inc", "ex"],
    [true, null, "inc", "ex"],
    [false, 0, "ex", "inc"],
    [false, null, "ex", "inc"],
    [null, 0, "ex", "inc"],
    [null, null, "ex", "inc"],
  ] as const)("标记=%s 税率=%s：单头=%s，分类合计和行=%s", async (inclusive, rate, headSide, lineSide) => {
    const dialog = await openDrawer({
      ...GAP_DETAIL, is_tax_inclusive: inclusive, tax_rate: rate, nominal_side: headSide,
      ...(inclusive === true ? {} : {
        amount_paid: 1130,
        lines: [{ ...GAP_DETAIL.lines[0], line_amount_ex: 1130, line_amount_inc: 1276.9 }],
        category_matched: { lines: 1, amount_inc: 1276.9, amount_ex: 1130, amount_paid: 1130 },
      }),
    });
    const headLabel = headSide === "inc" ? "含税金额" : "不含税金额";
    expectNote(within(dialog).getByText(headLabel), headSide);
    const matched = within(dialog).getByRole("alert");
    expectNote(matched, lineSide);
    const row = within(dialog).getByRole("row", { name: /PN-GAP/ });
    const cells = within(row).getAllByRole("cell");
    // 三个末尾金额列依次为实付、含税、不含税；实付和原始侧均不得被打名义标签。
    expectNote(cells[cells.length - 3], null);
    expectNote(cells[cells.length - 2], lineSide === "inc" ? "inc" : null);
    expectNote(cells[cells.length - 1], lineSide === "ex" ? "ex" : null);
  });

  it.each([0.13, 0.06, 0.006])("已标税率 %s：所有金额均不标名义", async (rate) => {
    const dialog = await openDrawer({ ...GAP_DETAIL, tax_rate: rate, tax_rate_effective: rate, nominal_side: null });
    expectNote(dialog, null);
  });

  it("金额被遮蔽时，税率与标记仍在也不显示行级或分类名义标签", async () => {
    const dialog = await openDrawer({
      ...GAP_DETAIL, nominal_side: null, amount_inc: null, amount_ex: null, amount_paid: null,
      lines: [{ ...GAP_DETAIL.lines[0], unit_price: null, line_amount: null, line_amount_inc: null, line_amount_ex: null }],
      category_matched: { lines: 1, amount_inc: null, amount_ex: null, amount_paid: null },
    });
    expectNote(dialog, null);
  });

  it("推算金额为零仍标名义，单头两侧皆空不影响有金额的明细行", async () => {
    const dialog = await openDrawer({
      ...GAP_DETAIL, nominal_side: null, amount_inc: null, amount_ex: null,
      lines: [{ ...GAP_DETAIL.lines[0], line_amount: 0, line_amount_inc: 0, line_amount_ex: 0 }],
      category_matched: { lines: 1, amount_inc: 0, amount_ex: 0, amount_paid: 0 },
    });
    expectNote(within(dialog).getByRole("alert"), "ex");
    expectNote(within(dialog).getByRole("row", { name: /PN-GAP/ }), "ex");
  });

  it("只选择实付时不渲染行税基列；分类合计仍按其展示金额标注", async () => {
    const dialog = await openDrawer(GAP_DETAIL, ["paid"]);
    expect(within(dialog).queryByRole("columnheader", { name: "行含税" })).toBeNull();
    expect(within(dialog).queryByRole("columnheader", { name: "行不含税" })).toBeNull();
    expectNote(within(dialog).getByRole("row", { name: /PN-GAP/ }), null);
    expectNote(within(dialog).getByRole("alert"), "ex");
  });
});
