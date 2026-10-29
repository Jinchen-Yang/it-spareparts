import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import type { NavigateFunction } from "react-router-dom";
import type { Overview } from "../../api";
import type { Offer } from "../../api/presales";
import type { PartSuggestion, UnifiedSearchItem, UnifiedSearchResp } from "../../api/search";
import { cleanupAntd, render } from "../../test/antdRender";

const { suggestParts, unifiedSearch, fetchOverview, library } = vi.hoisted(() => ({
  suggestParts: vi.fn(), unifiedSearch: vi.fn(), fetchOverview: vi.fn(), library: vi.fn(),
}));
vi.mock("../../api/search", async (original) => ({
  ...await original<typeof import("../../api/search")>(), suggestParts, unifiedSearch, fetchOverview,
}));
vi.mock("../../api/presales", () => ({ presales: { library } }));
vi.mock("../../api", () => ({ default: { get: vi.fn(async () => ({ data: { categories: [] } })) } }));
vi.mock("../../components/parts/PartGalleryPanel", () => ({ default: () => null }));
vi.mock("../../components/parts/InlinePartEditModal", () => ({ default: () => null }));
vi.mock("../../components/pools/PoolReferencePanel", () => ({ default: () => null }));
vi.mock("../../components/gallery/PartThumb", () => ({ default: () => null }));
vi.mock("../../components/pricing/PriceRefBadge", () => ({ default: () => null }));
vi.mock("../presales/PartInquiryHistory", () => ({ default: () => null }));
import PartSearchPage from "../PartSearchPage";

function item(id = 42): UnifiedSearchItem {
  return { part_id: id, pn_std: `PN-${id}`, description: "硬盘", brand: "华为", category: "备件",
    category_major: "备件", needs_review: false, is_excluded: false,
    match_type: "exact_pn", pool_group_id: null, pool_name: null };
}
function suggestion(id = 42): PartSuggestion {
  return { part_id: id, pn_std: `PN-${id}`, match_type: "prefix", match_reason: "PN前缀", matched_terms: ["PN"] };
}
function response(ids: number[], exact = false, similarIds: number[] = []): UnifiedSearchResp {
  return { total: ids.length, page: 1, page_size: 20, exact,
    items: ids.map(item), similar_items: similarIds.map(item) };
}
function overview(id = 42): Overview {
  return {
    part: { id, pn_std: `PN-${id}`, description: "硬盘", brand: "华为", category_major: "备件",
      category_minor: null, unit: null, needs_review: false, machine_or_part: null,
      locked_fields: [], redirected_from: null },
    purchases_recent: [], sales_recent: [], inventory: [], substitutes: [],
    profit_summary: { avg_purchase_cost: null, avg_sale_price: null, avg_cost_moving: null,
      avg_cost_fifo: null, avg_margin_moving: null, avg_margin_fifo: null, total_qty_sold: 0 },
    inquiry_ref: { min_money: null, max_money: null, last_money: null, count: 0 },
    sales_velocity: { qty_sold_90d: 0, monthly_avg_90d: 0, last_sale_date: null },
    sales_recent_restricted: false,
  };
}
function offer(overrides: Partial<Offer> = {}): Offer {
  return { id: 1, inquiry_id: 1, line_id: 1, part_id: 42, version: 1, revision_no: 1,
    status: "published", unit_price: "1180", is_tax_inclusive: true, no_stock: false,
    valid_from: "2020-10-29T02:00:00Z", reporter: { id: 7, name: "采购甲" }, ...overrides };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
let navigate: NavigateFunction;
function Probe() { navigate = useNavigate(); return null; }
function renderAt(url = "/parts?q=PN") {
  render(<MemoryRouter initialEntries={[url]}><PartSearchPage /><Probe /></MemoryRouter>);
}
function card(title: string) {
  return within(screen.getByText(title, { exact: true }).closest(".ant-card") as HTMLElement);
}
function expectGreen(tag: HTMLElement) {
  expect(tag).toHaveClass("ant-tag-green", "presales-price-tag");
  expect(tag).toHaveAttribute("title", "采购甲 报价");
}

beforeEach(() => {
  vi.clearAllMocks();
  library.mockReset();
  unifiedSearch.mockReset();
  localStorage.clear();
  localStorage.setItem("role", "sales");
  localStorage.setItem("permissions", JSON.stringify({ page_presales: true }));
  suggestParts.mockResolvedValue([]);
  unifiedSearch.mockResolvedValue(response([]));
  fetchOverview.mockImplementation(async ({ part_id }: { part_id: number }) => overview(part_id));
  library.mockResolvedValue({});
});
afterEach(cleanupAntd);

describe("型号查询最新报价", () => {
  it("精确匹配、相似型号和自动打开的全景显示绿色标签，复用同一批报价", async () => {
    unifiedSearch.mockResolvedValue(response([42], true, [73]));
    library.mockResolvedValue({
      42: [offer({ status: "withdrawn", more: [offer({ id: 2 })] })],
      73: [offer({ id: 3, part_id: 73, unit_price: "980" })],
    });
    renderAt("/parts?q=PN-42");
    await screen.findByText("精确匹配");
    expectGreen(await card("精确匹配").findByText(/最新 ¥1,180 含税 · 10-29/));
    expectGreen(card("相似型号").getByText(/最新 ¥980 含税/));
    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ part_id: 42 }));
    const title = (await screen.findByText("型号全景：")).closest(".ant-card-head-title") as HTMLElement;
    expectGreen(within(title).getByText(/最新 ¥1,180 含税/));
    expect(library).toHaveBeenCalledTimes(1);
    expect(library).toHaveBeenCalledWith([42, 73]);
  });

  it("搜索结果表多个 PN 显示绿色标签，仅按排序后的 ID 批量请求一次", async () => {
    unifiedSearch.mockResolvedValue(response([73, 42]));
    library.mockResolvedValue({ 42: [offer()], 73: [offer({ id: 2, part_id: 73, unit_price: "980" })] });
    renderAt();
    await screen.findByText("搜索结果");
    expectGreen(await card("搜索结果").findByText(/最新 ¥1,180 含税/));
    expectGreen(card("搜索结果").getByText(/最新 ¥980 含税/));
    expect(library).toHaveBeenCalledTimes(1);
    expect(library).toHaveBeenCalledWith([42, 73]);
  });

  it("PN 联想下拉复用绿色标签，多个候选和失焦后重开只发一次批量请求", async () => {
    suggestParts.mockResolvedValue([suggestion(73), suggestion(42)]);
    library.mockResolvedValue({ 42: [offer()], 73: [offer({ id: 2, part_id: 73, unit_price: "980" })] });
    renderAt("/parts");
    const input = screen.getByRole("textbox", { name: "型号或关键词" });
    fireEvent.change(input, { target: { value: "PN" } });
    const list = within(await screen.findByRole("listbox"));
    expectGreen(await list.findByText(/最新 ¥1,180 含税/));
    expectGreen(list.getByText(/最新 ¥980 含税/));
    expect(library).toHaveBeenCalledTimes(1);
    expect(library).toHaveBeenCalledWith([42, 73]);
    fireEvent.blur(input, { relatedTarget: screen.getByRole("button", { name: /搜\s*索/ }) });
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.focus(input);
    const reopened = within(await screen.findByRole("listbox"));
    expectGreen(reopened.getByText(/最新 ¥1,180 含税/));
    expect(suggestParts).toHaveBeenCalledTimes(1);
    expect(library).toHaveBeenCalledTimes(1);
  });

  it("输入新联想词时丢弃旧报价响应，即使新候选仍是同一型号", async () => {
    const oldPrice = deferred<Record<number, Offer[]>>();
    const newPrice = deferred<Record<number, Offer[]>>();
    const newSuggestions = deferred<PartSuggestion[]>();
    suggestParts.mockResolvedValueOnce([suggestion(42)]).mockReturnValueOnce(newSuggestions.promise);
    library.mockReturnValueOnce(oldPrice.promise).mockReturnValueOnce(newPrice.promise);
    renderAt("/parts");
    const input = screen.getByRole("textbox", { name: "型号或关键词" });
    fireEvent.change(input, { target: { value: "PN" } });
    await waitFor(() => expect(library).toHaveBeenCalledWith([42]));
    fireEvent.change(input, { target: { value: "PN-4" } });
    await act(async () => oldPrice.resolve({ 42: [offer()] }));
    await waitFor(() => expect(suggestParts).toHaveBeenCalledTimes(2));
    await act(async () => newSuggestions.resolve([suggestion(42)]));
    await waitFor(() => expect(library).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/最新 ¥/)).toBeNull();
    await act(async () => newPrice.resolve({ 42: [offer({ unit_price: "980" })] }));
    expectGreen(await screen.findByText(/最新 ¥980 含税/));
    expect(screen.queryByText(/最新 ¥1,180/)).toBeNull();
  });

  it("提交搜索立即作废同型号候选的旧报价请求，结果返回后重新查价", async () => {
    const candidatePrice = deferred<Record<number, Offer[]>>();
    const searchResult = deferred<UnifiedSearchResp>();
    const resultPrice = deferred<Record<number, Offer[]>>();
    suggestParts.mockResolvedValue([suggestion(42)]);
    unifiedSearch.mockReturnValue(searchResult.promise);
    library.mockReturnValueOnce(candidatePrice.promise).mockReturnValueOnce(resultPrice.promise);
    renderAt("/parts");
    fireEvent.change(screen.getByRole("textbox", { name: "型号或关键词" }), { target: { value: "PN" } });
    await waitFor(() => expect(library).toHaveBeenCalledWith([42]));
    fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(1));
    await act(async () => candidatePrice.resolve({ 42: [offer()] }));
    expect(screen.queryByText(/最新 ¥/)).toBeNull();
    expect(library).toHaveBeenCalledTimes(1);
    await act(async () => searchResult.resolve(response([42])));
    await waitFor(() => expect(library).toHaveBeenCalledTimes(2));
    expect(library).toHaveBeenLastCalledWith([42]);
    expect(screen.queryByText(/最新 ¥/)).toBeNull();
    await act(async () => resultPrice.resolve({ 42: [offer({ unit_price: "980" })] }));
    expectGreen(await card("搜索结果").findByText(/最新 ¥980 含税/));
    expect(screen.queryByText(/最新 ¥1,180/)).toBeNull();
  });

  it("非管理员没有 page_presales 权限时，结果和联想均不请求或展示报价", async () => {
    localStorage.setItem("permissions", JSON.stringify({ page_parts: true }));
    unifiedSearch.mockResolvedValue(response([42]));
    suggestParts.mockResolvedValue([suggestion(42)]);
    library.mockResolvedValue({ 42: [offer()] });
    renderAt();
    await screen.findByText("搜索结果");
    fireEvent.change(screen.getByRole("textbox", { name: "型号或关键词" }), { target: { value: "PN-4" } });
    await screen.findByRole("option");
    expect(library).not.toHaveBeenCalled();
    expect(screen.queryByText(/最新 ¥/)).toBeNull();
  });

  it("管理员即使 page_presales 为 false 仍可查看报价", async () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({ page_presales: false }));
    unifiedSearch.mockResolvedValue(response([42]));
    library.mockResolvedValue({ 42: [offer()] });
    renderAt();
    expectGreen(await screen.findByText(/最新 ¥1,180 含税/));
    expect(library).toHaveBeenCalledWith([42]);
  });

  it("无有效报价的型号不显示报价标签或暂无报价占位", async () => {
    unifiedSearch.mockResolvedValue(response([42, 73]));
    library.mockResolvedValue({ 42: [offer({ status: "stale" }), offer({ id: 2, no_stock: true })] });
    renderAt();
    await waitFor(() => expect(library).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/最新 ¥|暂无报价/)).toBeNull();
    expect(screen.getByText("搜索结果")).toBeVisible();
  });

  it("报价请求失败静默保留搜索结果", async () => {
    unifiedSearch.mockResolvedValue(response([42]));
    library.mockRejectedValue(new Error("报价库不可用"));
    renderAt();
    await waitFor(() => expect(library).toHaveBeenCalledTimes(1));
    expect(screen.getByText("搜索结果")).toBeVisible();
    expect(screen.queryByText(/最新 ¥|报价库不可用|加载失败/)).toBeNull();
    expect(document.querySelector(".ant-message-error")).toBeNull();
  });

  it("新搜索替代旧搜索后，迟到的报价响应不会覆盖新型号报价", async () => {
    const old = deferred<Record<number, Offer[]>>();
    unifiedSearch.mockResolvedValueOnce(response([42])).mockResolvedValue(response([73]));
    library.mockReturnValueOnce(old.promise)
      .mockResolvedValue({ 73: [offer({ part_id: 73, unit_price: "980" })] });
    renderAt("/parts?q=old");
    await waitFor(() => expect(library).toHaveBeenCalledWith([42]));
    act(() => navigate("/parts?q=new"));
    expectGreen(await screen.findByText(/最新 ¥980 含税/));
    await act(async () => old.resolve({ 42: [offer()] }));
    expect(screen.getByText(/最新 ¥980 含税/)).toBeVisible();
    expect(screen.queryByText(/最新 ¥1,180/)).toBeNull();
  });

  it("新搜索仍在等待型号结果时，就已丢弃旧报价请求", async () => {
    const oldPrice = deferred<Record<number, Offer[]>>();
    const newSearch = deferred<UnifiedSearchResp>();
    unifiedSearch.mockResolvedValueOnce(response([42])).mockReturnValueOnce(newSearch.promise);
    library.mockReturnValueOnce(oldPrice.promise)
      .mockResolvedValue({ 73: [offer({ part_id: 73, unit_price: "980" })] });
    renderAt("/parts?q=old");
    await waitFor(() => expect(library).toHaveBeenCalledWith([42]));
    act(() => navigate("/parts?q=new"));
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(2));
    await act(async () => oldPrice.resolve({ 42: [offer()] }));
    expect(screen.queryByText(/最新 ¥/)).toBeNull();
    await act(async () => newSearch.resolve(response([73])));
    expectGreen(await screen.findByText(/最新 ¥980 含税/));
  });
});
