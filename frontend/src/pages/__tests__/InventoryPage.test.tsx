import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanupAntd, renderWithoutMotion } from "../../test/antdRender";

const get = vi.hoisted(() => vi.fn());
vi.mock("../../api", () => ({ default: { get } }));

import InventoryPage from "../InventoryPage";

const row = (partId: number, pn: string) => ({
  part_id: partId, pn_std: pn, description: "测试备件", brand: null,
  dynamic_qty: 3, anchor_qty: 3, anchor_date: "2026-10-19", in_qty: 0,
  out_sales: 0, out_maint: 0, warehouses: [],
});

beforeEach(() => {
  get.mockReset();
  localStorage.clear();
});
afterEach(cleanupAntd);

it("库存加载失败可直接重试并恢复列表", async () => {
  get.mockRejectedValueOnce({ response: { status: 503, data: { detail: "库存查询暂不可用" } } });
  get.mockResolvedValueOnce({
    data: {
      items: [row(1, "PN-RETRY")],
      total: 1,
    },
  });
  renderWithoutMotion(<InventoryPage />);

  const retry = await screen.findByRole("button", { name: /重\s*试/ });
  expect(screen.getByText("加载失败，请点上方重试")).toBeInTheDocument();
  fireEvent.click(retry);

  expect(await screen.findByText("PN-RETRY")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("button", { name: /重\s*试/ })).toBeNull());
  expect(get).toHaveBeenCalledTimes(2);
  expect(get).toHaveBeenLastCalledWith("/inventory/dynamic", {
    params: { q: undefined, page: 1, page_size: 20 },
  });
});

it("翻页失败重试原页与原搜索条件，不使用未提交的搜索草稿", async () => {
  get.mockResolvedValueOnce({ data: { items: [row(1, "PN-FIRST")], total: 40 } });
  get.mockRejectedValueOnce({ response: { status: 503, data: { detail: "库存查询暂不可用" } } });
  get.mockResolvedValueOnce({ data: { items: [row(2, "PN-SECOND")], total: 40 } });
  renderWithoutMotion(<InventoryPage />);
  expect(await screen.findByText("PN-FIRST")).toBeInTheDocument();
  fireEvent.click(screen.getByTitle("2"));

  const retry = await screen.findByRole("button", { name: /重\s*试/ });
  fireEvent.change(screen.getByPlaceholderText("型号 / 描述 / 品牌"), {
    target: { value: "尚未提交的关键词" },
  });
  fireEvent.click(retry);

  expect(await screen.findByText("PN-SECOND")).toBeInTheDocument();
  expect(get.mock.calls.map(([, config]) => config.params.page)).toEqual([1, 2, 2]);
  expect(get).toHaveBeenLastCalledWith("/inventory/dynamic", {
    params: { q: undefined, page: 2, page_size: 20 },
  });
});
