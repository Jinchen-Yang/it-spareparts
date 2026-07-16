/** 型号查询（统一搜索 + 稳定深链）的行为测试。
 *
 * 断言口径：
 * - /parts?part_id=<ID> 深链：打开即自动加载并展示该型号全景（刷新语义等价）；
 * - /parts?pn=<PN> 兼容：按 PN 解析后 URL 自动改写成 part_id 深链（replace 不堆历史）；
 * - 精确命中：唯一主结果 + 自动打开全景；相似候选只出现在"相似型号"独立区域，
 *   不与精确结果混排（不渲染普通"搜索结果"表）；
 * - 歧义：ambiguous=true → 明确警示，不自动打开任何型号；
 * - 点击结果行 → part_id push 进历史；浏览器后退/前进保持/恢复选中型号。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import type { Location, NavigateFunction } from "react-router-dom";

const unifiedSearch = vi.fn();
const fetchOverview = vi.fn();
const fetchPoolReference = vi.fn();

vi.mock("../../api/search", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../../api/search")>();
  return { ...mod,
    unifiedSearch: (...a: unknown[]) => unifiedSearch(...a),
    fetchOverview: (...a: unknown[]) => fetchOverview(...a) };
});
vi.mock("../../api/poolAnalysis", () => ({
  fetchPoolReference: (...a: unknown[]) => fetchPoolReference(...a),
}));
vi.mock("../../api", () => {
  const api = {
    get: vi.fn(async () => ({ data: { categories: [] } })),
    post: vi.fn(), delete: vi.fn(),
  };
  return { default: api, api };
});

import PartSearchPage from "../PartSearchPage";

const item = (over: Record<string, unknown> = {}) => ({
  part_id: 42, pn_std: "02311DYQ", description: "华为部件 DYQ", brand: "华为",
  category: "备件", category_major: "备件", needs_review: false, is_excluded: false,
  match_type: "exact_pn", matched_text: "02311DYQ", score: 1, match_reason: "PN精确匹配",
  pool_group_id: 7, pool_name: "华为互通池", ...over,
});

const ovFix = (id = 42, pn = "02311DYQ") => ({
  part: { id, pn_std: pn, description: "华为部件", brand: "华为", category_major: "备件",
          category_minor: null, unit: null, needs_review: false, machine_or_part: null,
          locked_fields: [], redirected_from: null },
  purchases_recent: [], sales_recent: [], inventory: [], substitutes: [],
  profit_summary: { avg_purchase_cost: null, avg_sale_price: null, avg_cost_moving: null,
                    avg_cost_fifo: null, avg_margin_moving: null, avg_margin_fifo: null,
                    total_qty_sold: 0 },
  inquiry_ref: { min_money: null, max_money: null, last_money: null, count: 0 },
  sales_velocity: { qty_sold_90d: 0, monthly_avg_90d: 0, last_sale_date: null },
  sales_recent_restricted: false,
});

const emptyResp = { total: 0, page: 1, page_size: 20, exact: false, ambiguous: false,
                    low_confidence: true, items: [], similar_items: [] };

// 声明式 MemoryRouter（不走数据路由的 Request 构造，绕开 jsdom×undici 的跨 realm
// AbortSignal 校验）；location/navigate 用探针拿出来做断言与前进/后退。
let curLoc!: Location;
let nav!: NavigateFunction;
function Probe() {
  curLoc = useLocation();
  nav = useNavigate();
  return null;
}

function renderAt(url: string) {
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/parts" element={<><PartSearchPage /><Probe /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  unifiedSearch.mockResolvedValue(emptyResp);
  fetchOverview.mockResolvedValue(ovFix());
  fetchPoolReference.mockResolvedValue({
    part_id: 42, pn_std: "02311DYQ", pool: null,
    window: { range: "90d", date_from: null, date_to: null }, basis: "ex_tax",
    purchase_reference: { restricted: false, pool_stats: null, part_stats: null,
      constraint: { status: "unset", value: null }, delta_to_pool_avg: null,
      delta_to_constraint: null, relation_to_constraint: "unset" },
    sales_reference: { restricted: false, pool_stats: null, part_stats: null,
      constraint: { status: "unset", value: null }, delta_to_pool_avg: null,
      delta_to_constraint: null, relation_to_constraint: "unset" },
  });
});
afterEach(cleanup);

describe("稳定深链", () => {
  it("/parts?part_id=42 打开即自动加载并展示该型号全景（无需再点搜索）", async () => {
    renderAt("/parts?part_id=42");
    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ part_id: 42 }));
    await screen.findByText("型号全景：");
    expect(screen.getAllByText("02311DYQ").length).toBeGreaterThan(0);
    expect(unifiedSearch).not.toHaveBeenCalled();   // 纯 part_id 深链不触发搜索
  });

  it("/parts?pn=<PN> 兼容入口：解析后 URL 自动改写成稳定 part_id 深链", async () => {
    renderAt("/parts?pn=02311DYQ");
    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ pn_std: "02311DYQ" }));
    await waitFor(() => expect(curLoc.search).toContain("part_id=42"));
    expect(curLoc.search).not.toContain("pn=");
    await screen.findByText("型号全景：");
  });

  it("非法 part_id 不请求、不崩", async () => {
    renderAt("/parts?part_id=abc");
    await Promise.resolve();
    expect(fetchOverview).not.toHaveBeenCalled();
  });
});

describe("精确即唯一", () => {
  it("精确型号全景在历史卡片前显示近90天同源池价格参考卡", async () => {
    fetchPoolReference.mockResolvedValue({
      part_id: 42, pn_std: "02311DYQ",
      pool: { group_id: 7, name: "华为互通池", member_count: 8 },
      window: { range: "90d", date_from: "2026-04-01", date_to: "2026-06-29" },
      basis: "ex_tax",
      purchase_reference: { restricted: false,
        pool_stats: { weighted_avg: 500, median: 490, min: 400, max: 600, latest: 520,
          total_amount: 5000, total_qty: 10, order_count: 3, line_count: 4 },
        part_stats: { weighted_avg: 540, median: 530, min: 500, max: 600, latest: 520,
          total_amount: 1080, total_qty: 2, order_count: 2, line_count: 2 },
        constraint: { status: "set", value: 550 }, delta_to_pool_avg: 40,
        delta_to_constraint: -10, relation_to_constraint: "below" },
      sales_reference: { restricted: false,
        pool_stats: { weighted_avg: 800, median: 790, min: 700, max: 900, latest: 820,
          total_amount: 8000, total_qty: 10, order_count: 4, line_count: 4 },
        part_stats: { weighted_avg: 820, median: 810, min: 780, max: 850, latest: 820,
          total_amount: 1640, total_qty: 2, order_count: 2, line_count: 2 },
        constraint: { status: "unset", value: null }, delta_to_pool_avg: 20,
        delta_to_constraint: null, relation_to_constraint: "unset" },
    });
    renderAt("/parts?part_id=42");

    await waitFor(() => expect(fetchPoolReference).toHaveBeenCalledWith(42, { range: "90d" }));
    expect(await screen.findByRole("region", { name: "02311DYQ 的池价格参考" })).toBeInTheDocument();
    expect(screen.getByLabelText("采购参考")).toHaveTextContent("池均价 ¥500.00");
    expect(screen.getByLabelText("销售参考")).toHaveTextContent("池均价 ¥800.00");
  });

  it("精确命中：唯一主结果自动开全景；相似候选只在'相似型号'区，不混排", async () => {
    unifiedSearch.mockResolvedValue({
      total: 1, page: 1, page_size: 20, exact: true, ambiguous: false, low_confidence: false,
      items: [item()],
      similar_items: [
        item({ part_id: 43, pn_std: "02311DYA", match_type: "fuzzy_pn",
               score: 0.8, match_reason: "PN相似0.80" }),
        item({ part_id: 44, pn_std: "02311DYB", match_type: "fuzzy_pn",
               score: 0.78, match_reason: "PN相似0.78" }),
      ],
    });
    renderAt("/parts?q=02311DYQ");
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalled());
    // 主结果区 = 精确匹配卡；不渲染普通"搜索结果"表（相似 PN 不抢占精确结果）
    await screen.findByText("精确匹配");
    expect(screen.queryByText("搜索结果")).toBeNull();
    // 相似型号独立区域
    await screen.findByText("相似型号");
    expect(screen.getAllByText("02311DYA").length).toBeGreaterThan(0);
    // 自动打开唯一主结果（URL 带 part_id，replace 语义）
    await waitFor(() => expect(curLoc.search).toContain("part_id=42"));
    await screen.findByText("型号全景：");
    // 池身份展示
    expect(screen.getAllByText(/华为互通池/).length).toBeGreaterThan(0);
  });

  it("歧义（同写法多个型号）：明确警示，不自动打开", async () => {
    unifiedSearch.mockResolvedValue({
      total: 2, page: 1, page_size: 20, exact: false, ambiguous: true, low_confidence: false,
      items: [item({ pn_std: "AB-100", part_id: 51 }),
              item({ pn_std: "AB100", part_id: 52 })],
      similar_items: [],
    });
    renderAt("/parts?q=AB100");
    await screen.findByText("该写法命中多个型号（歧义）");
    expect(curLoc.search).not.toContain("part_id");
    expect(fetchOverview).not.toHaveBeenCalled();
  });
});

describe("浏览器历史", () => {
  it("点击结果行 → part_id 入 URL；后退关闭全景、前进恢复选中型号", async () => {
    unifiedSearch.mockResolvedValue({
      total: 2, page: 1, page_size: 20, exact: false, ambiguous: false, low_confidence: false,
      items: [item({ match_type: "fuzzy_pn", score: 0.7, match_reason: "PN相似0.70" }),
              item({ part_id: 43, pn_std: "02311DYA", match_type: "fuzzy_pn",
                     score: 0.65, match_reason: "PN相似0.65" })],
      similar_items: [],
    });
    fetchOverview.mockImplementation(async (key: any) =>
      key.part_id === 43 ? ovFix(43, "02311DYA") : ovFix(42, "02311DYQ"));
    renderAt("/parts?q=02311DY");
    const link = await screen.findByText("02311DYQ");
    fireEvent.click(link);
    await waitFor(() => expect(curLoc.search).toContain("part_id=42"));
    await screen.findByText("型号全景：");

    act(() => nav(-1));   // 后退：回到未选中状态
    await waitFor(() => expect(curLoc.search).not.toContain("part_id"));
    await waitFor(() => expect(screen.queryByText("型号全景：")).toBeNull());

    act(() => nav(1));    // 前进：恢复选中型号
    await waitFor(() => expect(curLoc.search).toContain("part_id=42"));
    await screen.findByText("型号全景：");
  });

  it("搜索提交把 q 推进 URL 并清掉旧选中", async () => {
    renderAt("/parts?part_id=42");
    await screen.findByText("型号全景：");
    const input = screen.getByPlaceholderText(/输入型号/);
    fireEvent.change(input, { target: { value: "ST8000" } });
    fireEvent.click(screen.getByRole("button", { name: /搜 索|搜索/ }));
    await waitFor(() => {
      expect(curLoc.search).toContain("q=ST8000");
      expect(curLoc.search).not.toContain("part_id");
    });
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledWith(
      "ST8000", expect.objectContaining({ pageSize: 20 })));
  });
});
