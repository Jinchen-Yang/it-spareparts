import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { cleanupAntd, render } from "../../test/antdRender";

const { suggestParts, unifiedSearch, fetchOverview } = vi.hoisted(() => ({
  suggestParts: vi.fn(), unifiedSearch: vi.fn(), fetchOverview: vi.fn(),
}));
vi.mock("../../api/search", async (original) => ({
  ...await original<typeof import("../../api/search")>(), suggestParts, unifiedSearch, fetchOverview,
}));
vi.mock("../../api", () => ({ default: { get: vi.fn(async () => ({ data: { categories: [] } })) } }));
vi.mock("../../components/parts/PartGalleryPanel", () => ({ default: () => null }));
vi.mock("../../components/parts/InlinePartEditModal", () => ({ default: () => null }));
vi.mock("../../components/pools/PoolReferencePanel", () => ({ default: () => null }));
import PartSearchPage from "../PartSearchPage";

let search = "";
function Probe() { search = useLocation().search; return null; }
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });
afterEach(cleanupAntd);

it("输入联想后选定候选，按候选 PN 精确查询并打开稳定身份的型号全景", async () => {
  const candidate = { part_id: 42, pn_std: "0231-AC4L", brand: "华为", manufacturer: "Huawei",
    summary: "候选简介", key_specs: [], match_type: "prefix", match_reason: "PN前缀", matched_terms: ["0231"] };
  suggestParts.mockResolvedValue([candidate]);
  unifiedSearch.mockResolvedValue({ total: 0, page: 1, page_size: 20, items: [] });
  fetchOverview.mockResolvedValue({
    part: { id: 42, pn_std: "0231-AC4L", description: "华为硬盘", brand: "华为", category_major: "存储",
      category_minor: null, unit: null, needs_review: false, machine_or_part: null, locked_fields: [], redirected_from: null },
    purchases_recent: [], sales_recent: [], inventory: [], substitutes: [],
    profit_summary: { avg_purchase_cost: null, avg_sale_price: null, avg_cost_moving: null, avg_cost_fifo: null,
      avg_margin_moving: null, avg_margin_fifo: null, total_qty_sold: 0 },
    inquiry_ref: { min_money: null, max_money: null, last_money: null, count: 0 },
    sales_velocity: { qty_sold_90d: 0, monthly_avg_90d: 0, last_sale_date: null }, sales_recent_restricted: false,
  });
  render(<MemoryRouter initialEntries={["/parts?category_major=旧品类&group_id=7"]}><PartSearchPage /><Probe /></MemoryRouter>);
  await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByRole("textbox", { name: "型号或关键词" }), { target: { value: "0231" } });
  const option = await screen.findByRole("option");
  expect(suggestParts).toHaveBeenCalledWith("0231", 10, expect.any(AbortSignal));
  expect(unifiedSearch).toHaveBeenCalledTimes(1);
  fireEvent.click(option);
  await waitFor(() => expect(unifiedSearch).toHaveBeenLastCalledWith("0231-AC4L", { pageSize: 20, filters: {} }));
  await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ part_id: 42 }));
  expect(await screen.findByText("型号全景：")).toBeVisible();
  expect(new URLSearchParams(search).get("q")).toBe("0231-AC4L");
  expect(new URLSearchParams(search).get("part_id")).toBe("42");
  expect(new URLSearchParams(search).has("category_major")).toBe(false);
  expect(new URLSearchParams(search).get("group_id")).toBe("7");
});
