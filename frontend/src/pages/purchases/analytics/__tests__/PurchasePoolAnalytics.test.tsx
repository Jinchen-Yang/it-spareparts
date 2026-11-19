import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import PurchasePoolAnalytics from "../PurchasePoolAnalytics";

const api = vi.hoisted(() => ({ parts: vi.fn(), sources: vi.fn(), members: vi.fn() }));
vi.mock("../../../../api/purchasePoolAnalytics", () => ({
  fetchPoolParts: api.parts,
  fetchPoolSources: api.sources,
  fetchPoolMembers: api.members,
}));
const row = {
  part_id: 7,
  pn: "PN-7",
  description: "库存主档描述",
  unit: "件",
  orders: 4,
  lines: 8,
  quantity: 9,
  pool_id: 11,
  pool_name: "池甲",
  suppliers: 1,
  price: 137,
  pool_price: 120,
  ceiling: null,
  delta_pool: 0.1417,
  delta_cap: null,
  price_orders: 3,
  price_qty: 7,
  price_min: 100,
  price_max: 150,
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  localStorage.clear();
});
function mount(entry = "/") {
  const onDrill = vi.fn(),
    onOwnerStale = vi.fn();
  render(
    <MemoryRouter initialEntries={[entry]}>
      <PurchasePoolAnalytics
        filters={{ date_from: "2025-01-01" }}
        scope={{ buyer: "经办" }}
        ownerToken={null}
        costRestricted={false}
        customerRestricted={false}
        supplierRestricted={false}
        onDrill={onDrill}
        onOwnerStale={onOwnerStale}
      />
    </MemoryRouter>,
  );
  return { onDrill, onOwnerStale };
}
describe("采购 PN 工作台", () => {
  it("单位未填的来源、成员使用 null 单位，采购单下钻保留相同范围", async () => {
    const missingUnit = { ...row, unit: null, quantity: null };
    api.parts.mockResolvedValue({ data: { items: [missingUnit], price_visible: true, supplier_visible: true, has_more: false } });
    api.sources.mockResolvedValue({ data: { items: [{ ...missingUnit, supplier: "未知单位来源", supplier_id: 23 }], has_more: false } });
    api.members.mockResolvedValue({ data: { items: [{ ...missingUnit, part_id: 8, pn: "同池 PN" }], has_more: false } });
    const { onDrill } = mount();
    fireEvent.click(await screen.findByRole("button", { name: /PN-7.*目录/ }));
    const source = await screen.findByRole("button", { name: "PN-7 · 未知单位来源 · 采购单" });
    for (const fetch of [api.sources, api.members]) {
      expect(fetch.mock.calls[0][1].unit).toBeNull();
      expect(JSON.parse(fetch.mock.calls[0][1].scope)).toEqual({ buyer: "经办" });
    }
    fireEvent.click(source);
    expect(onDrill).toHaveBeenLastCalledWith({ partId: "7", pn: "PN-7", unit: null, supplier: "23", _line: "1" }, expect.any(String));
    fireEvent.click(screen.getByRole("button", { name: "同池 PN 的采购单" }));
    expect(onDrill).toHaveBeenLastCalledWith({ partId: "8", pn: "同池 PN", unit: null, _line: "1" }, expect.any(String));
  });
  it("在选中 PN 后才取价区间与成员，下钻携带单位及供应商 ID", async () => {
    api.parts.mockResolvedValue({
      data: { items: [row], price_visible: true, supplier_visible: true, has_more: false },
    });
    api.sources.mockResolvedValue({
      data: { items: [{ ...row, supplier: "供货甲", supplier_id: 23 }], has_more: false },
    });
    api.members.mockResolvedValue({ data: { items: [], has_more: false } });
    const { onDrill } = mount();
    const part = await screen.findByRole("button", { name: /PN-7.*目录/ });
    expect(api.sources).not.toHaveBeenCalled();
    fireEvent.click(part);
    const source = await screen.findByRole("button", { name: "PN-7 · 供货甲 · 采购单" });
    expect(api.sources.mock.calls[0][1]).toMatchObject({
      unit: "件",
      part_id: 7,
      scope: '{"buyer":"经办"}',
    });
    expect(api.members.mock.calls[0][1].pool_id).toBe(11);
    fireEvent.click(source);
    expect(onDrill).toHaveBeenCalledWith(
      { partId: "7", pn: "PN-7", unit: "件", supplier: "23", _line: "1" },
      expect.any(String),
    );
    expect(await screen.findByText(/100.00 – 150.00 · 均 137.00/)).toBeInTheDocument();
  });
  it("隐藏价格和供应商时不取供应商明细，过期账号不能发起下钻", async () => {
    api.parts.mockResolvedValue({
      data: {
        items: [{ ...row, pool_id: null }],
        price_visible: false,
        supplier_visible: false,
        has_more: false,
      },
    });
    const { onDrill, onOwnerStale } = mount();
    fireEvent.click(await screen.findByRole("button", { name: /PN-7.*目录/ }));
    expect(screen.queryByText("PN 均价（元）")).not.toBeInTheDocument();
    expect(api.sources).not.toHaveBeenCalled();
    localStorage.setItem("token", "另一会话");
    fireEvent.click(screen.getByRole("button", { name: "PN-7 · 采购单" }));
    await waitFor(() => expect(onOwnerStale).toHaveBeenCalled());
    expect(onDrill).not.toHaveBeenCalled();
  });
});
