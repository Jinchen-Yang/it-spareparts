import { cleanupAntd, renderWithoutMotion as render } from "../../test/antdRender";
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
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import type { Location, NavigateFunction } from "react-router-dom";
import type { Overview } from "../../api";

const unifiedSearch = vi.fn();
const fetchOverview = vi.fn();
const fetchMasterEditState = vi.fn();
const fetchPoolReference = vi.fn();
const masterCategories = vi.fn();
const masterEdit = vi.fn();

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

vi.mock("../../api/search", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../../api/search")>();
  return { ...mod,
    unifiedSearch: (...a: unknown[]) => unifiedSearch(...a),
    fetchOverview: (...a: unknown[]) => fetchOverview(...a),
    fetchMasterEditState: (...a: unknown[]) => fetchMasterEditState(...a),
    fetchMasterEditCategories: async () => (await masterCategories()).data.categories };
});
vi.mock("../../api/poolAnalysis", () => ({
  fetchPoolReference: (...a: unknown[]) => fetchPoolReference(...a),
}));
vi.mock("../../components/gallery/GalleryPicker", () => ({
  default: ({ items, mode, onClose }: { items: { partId: number; pn: string }[]; mode: string; onClose: () => void }) => (
    <div role="dialog" aria-label="图库选择器桩">
      <span>{`${mode}:${items[0].partId}:${items[0].pn}`}</span>
      <button onClick={onClose}>关闭图库选择器桩</button>
    </div>
  ),
}));
vi.mock("../../api", () => {
  const api = {
    get: vi.fn(async () => ({ data: { categories: [] } })),
    post: vi.fn(), delete: vi.fn(), patch: vi.fn(),
  };
  return {
    default: api,
    api,
    masterCategories: (...a: unknown[]) => masterCategories(...a),
    masterEdit: (...a: unknown[]) => masterEdit(...a),
  };
});

import PartSearchPage from "../PartSearchPage";
import InlinePartEditModal from "../../components/parts/InlinePartEditModal";

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

const editorState = (part: Overview["part"]) => ({
  part_id: part.id, pn_std: part.pn_std,
  identity: { part_id: part.id, pn_std: part.pn_std, status: "active", merged_into_id: null },
  fields: {
    description: part.description, brand: part.brand,
    category_major: part.category_major, category_minor: part.category_minor,
    machine_or_part: part.machine_or_part, unit: part.unit,
  },
  specs: {}, version_mode: "field_snapshot",
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

// fireEvent 只等待同步更新；这里的 API mock 立即返回 Promise，须让 React 提交
// 点击引发的 PN → part_id 规范化后再断言，避免 1s 轮询与重渲染抢时间。
async function clickAndFlush(element: HTMLElement) {
  await act(async () => { fireEvent.click(element); });
}

function renderAt(url: string) {
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/parts" element={<><PartSearchPage /><Probe /></>} />
        <Route path="/pool-analysis/:groupId" element={<><div>池分析页桩</div><Probe /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "admin-token");
  localStorage.setItem("role", "admin");
  unifiedSearch.mockResolvedValue(emptyResp);
  fetchOverview.mockResolvedValue(ovFix());
  fetchMasterEditState.mockImplementation(async (id: number) => editorState({
    ...ovFix(id, id === 100 ? "SUB-002" : "SUB-001").part,
    description: id === 100 ? "第二型号描述" : "目标型号描述",
    category_major: "服务器配件", category_minor: "磁盘",
  }));
  masterCategories.mockResolvedValue({ data: { categories: [] } });
  masterEdit.mockResolvedValue({
    data: { id: 99, pn_std: "SUB-001", updated: ["description"], locked_fields: ["description"] },
  });
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
afterEach(async () => {
  await cleanupAntd();
  vi.restoreAllMocks();
});

describe("稳定深链", () => {
  it("图库多选下载入口以 modal 打开当前型号，关闭后清理", async () => {
    renderAt("/parts?part_id=42");
    await screen.findByText("型号全景：");
    fireEvent.click(screen.getByRole("button", { name: "多选下载" }));
    expect(screen.getByRole("dialog", { name: "图库选择器桩" })).toHaveTextContent("modal:42:02311DYQ");
    fireEvent.click(screen.getByRole("button", { name: "关闭图库选择器桩" }));
    expect(screen.queryByRole("dialog", { name: "图库选择器桩" })).toBeNull();
  });

  it("切换全景型号会关闭图库选择器，新入口使用新型号", async () => {
    fetchOverview.mockImplementation(async (key: { part_id: number }) => ovFix(key.part_id, key.part_id === 43 ? "PN-43" : "02311DYQ"));
    renderAt("/parts?part_id=42");
    await screen.findByText("型号全景：");
    fireEvent.click(screen.getByRole("button", { name: "多选下载" }));
    act(() => nav("/parts?part_id=43"));
    await screen.findByRole("button", { name: "修改型号 PN-43 的描述和品类" });
    expect(screen.queryByRole("dialog", { name: "图库选择器桩" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "多选下载" }));
    expect(screen.getByRole("dialog", { name: "图库选择器桩" })).toHaveTextContent("modal:43:PN-43");
  });

  it("替代料折叠可用 Tab 到达并用空格和 Enter 切换，保留记忆与编辑权限遮蔽", async () => {
    localStorage.setItem("role", "readonly");
    localStorage.setItem("ps_subs_open", "0");
    const user = userEvent.setup();
    renderAt("/parts?part_id=42");
    await screen.findByText("型号全景：");
    const toggle = screen.getByRole("button", { name: /替代料 · 通用号/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    for (let i = 0; i < 36 && document.activeElement !== toggle; i++) await user.tab();
    expect(toggle).toHaveFocus();
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("暂无替代料")).toBeVisible();
    expect(screen.queryByRole("button", { name: "添加替代料" })).not.toBeInTheDocument();
    expect(localStorage.getItem("ps_subs_open")).toBe("1");
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("暂无替代料")).not.toBeInTheDocument();
    expect(localStorage.getItem("ps_subs_open")).toBe("0");
  });

  it("/parts?part_id=42 打开即自动加载并展示该型号全景（无需再点搜索）", async () => {
    renderAt("/parts?part_id=42");
    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ part_id: 42 }));
    await screen.findByText("型号全景：");
    expect(screen.getAllByText("02311DYQ").length).toBeGreaterThan(0);
    expect(unifiedSearch).not.toHaveBeenCalled();   // 纯 part_id 深链不触发搜索
  });

  it("采购销售优先展示后端双税价，采购未标注口径仍按未税原值", async () => {
    fetchOverview.mockResolvedValue({
      ...ovFix(),
      purchases_recent: [
        {
          order_no: "PO-EXPLICIT", order_date: "2026-07-01", supplier: "供应商甲",
          qty: 1, unit_price: 113, source_type: "销售订单", is_tax_inclusive: true,
          price_inc: 120, price_ex: 99,
        },
        {
          order_no: "PO-UNKNOWN", order_date: "2026-07-02", supplier: "供应商乙",
          qty: 1, unit_price: 100, source_type: "销售订单", is_tax_inclusive: null,
          price_inc: 100, price_ex: 88.5,
        },
      ],
      sales_recent: [{
        order_no: "SO-EXPLICIT", order_date: "2026-07-03", customer: "客户甲",
        qty: 1, unit_price: 226, price_inc: 250, price_ex: 200,
      }],
    });

    renderAt("/parts?part_id=42");
    await screen.findByText("型号全景：");

    expect(screen.getByText("¥120")).toBeInTheDocument();
    expect(screen.getByText("¥99")).toBeInTheDocument();
    expect(screen.getByText("¥113")).toBeInTheDocument();
    expect(screen.getByText("¥100")).toBeInTheDocument();
    await clickAndFlush(screen.getByRole("tab", { name: "销售历史(近20)" }));
    expect(screen.getByText("¥200")).toBeInTheDocument();
    expect(screen.queryByText("¥88.5")).toBeNull();
    expect(screen.queryByText("¥226")).toBeNull();
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

  it("池分析上下文保留在型号深链，显式返回可完整重放", async () => {
    renderAt("/parts?part_id=42&group_id=12&range=custom&date_from=2026-06-01&date_to=2026-06-30&side=sales&purchase_type=补库&employee=李四&price_sort=weighted_avg&price_order=desc");
    await screen.findByText("型号全景：");
    fireEvent.click(screen.getByRole("link", { name: "返回互通池分析" }));
    await screen.findByText("池分析页桩");
    const query = new URLSearchParams(curLoc.search);
    expect(curLoc.pathname).toBe("/pool-analysis/12");
    expect(Object.fromEntries(query)).toEqual({
      range: "custom", from: "2026-06-01", to: "2026-06-30", side: "sales",
      purchase_type: "补库", employee: "李四",
      price_sort: "weighted_avg", price_order: "desc",
    });
  });
});

describe("精确即唯一", () => {
  it("精确型号全景保留近90天同源池价格参考卡", async () => {
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
    fireEvent.click(screen.getByText("池价格参考"));
    expect(screen.getByLabelText("采购参考"))
      .toHaveTextContent("池均价 含 ¥565 · 不含 ¥500");
    expect(screen.getByLabelText("销售参考")).toHaveTextContent("池均价 ¥800");
  });

  it("摘要、基本信息、库存先于订单历史，采购和销售通过页签切换", async () => {
    renderAt("/parts?part_id=42");

    const summary = await screen.findByRole("region", { name: "型号摘要" });
    const orderHistory = screen.getByRole("group", { name: "型号订单历史" });
    const basicInfo = screen.getByRole("region", { name: "型号基本信息" });
    const inventory = screen.getByRole("region", { name: "型号库存" });
    expect(summary.compareDocumentPosition(basicInfo) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(basicInfo.compareDocumentPosition(inventory) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(inventory.compareDocumentPosition(orderHistory) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(summary).getByText("动态可用库存")).toBeVisible();
    expect(screen.getByRole("tab", { name: "采购历史(近20)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("region", { name: "销售订单历史" })).toBeNull();
    await clickAndFlush(screen.getByRole("tab", { name: "销售历史(近20)" }));
    expect(screen.getByRole("region", { name: "销售订单历史" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "采购订单历史" })).toBeNull();
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
    const similarRegion = screen.getByLabelText("相似型号候选");
    const summary = screen.getByRole("region", { name: "型号摘要" });
    expect(similarRegion.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await clickAndFlush(within(similarRegion).getByRole("button", { name: "02311DYA" }));
    expect(fetchOverview).toHaveBeenLastCalledWith({ part_id: 43 });
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

describe("摘要权限与空态", () => {
  it.each([
    { cost: false, profit: false },
    { cost: false, profit: true },
    { cost: true, profit: false },
    { cost: true, profit: true },
  ])("成本 $cost / 利润 $profit：保持原权限组合，销售明细继续遮蔽", async ({ cost, profit }) => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: cost, data_profit: profit }));
    fetchOverview.mockResolvedValue({
      ...ovFix(),
      profit_summary: { ...ovFix().profit_summary, avg_cost_moving: 123, avg_margin_moving: 0.45 },
      sale_price_ref: { ref_sale_price: 226, ref_sale_samples: 5, ref_window_days: 30 },
      stock_dynamic: { dynamic_qty: 7 },
      sales_recent_restricted: true,
      sales_recent: [{ order_no: "SHOULD-NOT-SHOW", customer: "不可见客户", qty: 1, unit_price: 999 }],
    });
    renderAt("/parts?part_id=42");
    const summary = await screen.findByRole("region", { name: "型号摘要" });
    expect(within(summary).getByLabelText("近期成交参考价")).toHaveTextContent("¥200");
    expect(within(summary).getByLabelText("动态可用库存")).toHaveTextContent("7");
    expect(!!within(summary).queryByLabelText("移动加权单位成本")).toBe(cost);
    expect(!!within(summary).queryByLabelText("移动加权毛利率")).toBe(cost && profit);
    if (!cost) expect(summary).not.toHaveTextContent("¥123");
    if (!cost || !profit) expect(summary).not.toHaveTextContent("45%");
    await clickAndFlush(screen.getByRole("tab", { name: "销售历史(近20)" }));
    expect(screen.getByText(/按权限，销售逐单成交明细不可见/)).toBeVisible();
    expect(screen.queryByText("SHOULD-NOT-SHOW")).toBeNull();
    expect(screen.queryByText("不可见客户")).toBeNull();
    expect(within(summary).queryByText("销售单号")).toBeNull();
  });

  it("更多价格统计里的双口径金额分两行堆叠，长金额可换行不横溢", async () => {
    fetchOverview.mockResolvedValue({ ...ovFix(), profit_summary: {
      ...ovFix().profit_summary, avg_cost_fifo: 1234567.89 } });
    renderAt("/parts?part_id=42");
    const stats = await screen.findByRole("region", { name: "更多价格统计" });
    const label = within(stats).getByText((_, el) => el?.textContent === "不含 ");
    const pair = label.parentElement!.parentElement!;
    expect(pair).toHaveStyle({ flexDirection: "column" });
    expect(stats.querySelector("[style*='nowrap']")).toBeNull();
  });

  it("负动态库存用深色危险色类名，不用 accent-soft 底上不足 4.5 的 danger", async () => {
    fetchOverview.mockResolvedValue({ ...ovFix(), stock_dynamic: {
      dynamic_qty: -3, anchor_qty: 0, anchor_date: null, in_qty: 0, out_sales: 3, out_maint: 0 } });
    renderAt("/parts?part_id=42");
    const summary = await screen.findByRole("region", { name: "型号摘要" });
    const value = within(within(summary).getByLabelText("动态可用库存")).getByText("-3");
    expect(value).toHaveClass("is-negative");
    expect(value.getAttribute("style") ?? "").not.toContain("--mb-danger");
  });

  it("缺参考价和动态库存时不把未知显示成零", async () => {
    renderAt("/parts?part_id=42");
    const summary = await screen.findByRole("region", { name: "型号摘要" });
    expect(within(summary).getByText("暂无成交参考价")).toBeVisible();
    expect(within(summary).getByLabelText("动态可用库存")).toHaveTextContent("—");
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

  it("A → 慢 B → A 时，B 的迟到响应不会覆盖已恢复的 A", async () => {
    const delayedB = deferred<ReturnType<typeof ovFix>>();
    const partA = {
      ...ovFix(42, "PART-A"),
      part: { ...ovFix(42, "PART-A").part, description: "父型号 A" },
    };
    const partB = {
      ...ovFix(77, "PART-B"),
      part: { ...ovFix(77, "PART-B").part, description: "父型号 B" },
    };
    fetchOverview.mockImplementation(async (key: any) =>
      key.part_id === 77 ? delayedB.promise : partA);

    renderAt("/parts?part_id=42");
    expect(await screen.findByText("父型号 A")).toBeInTheDocument();
    act(() => nav("/parts?part_id=77"));
    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ part_id: 77 }));
    act(() => nav("/parts?part_id=42"));
    expect(screen.getByText("父型号 A")).toBeInTheDocument();

    delayedB.resolve(partB);
    await act(async () => { await delayedB.promise; });

    expect(curLoc.search).toContain("part_id=42");
    expect(screen.getByText("父型号 A")).toBeInTheDocument();
    expect(screen.queryByText("父型号 B")).toBeNull();
  });
});

describe("通用号 PN 查看与详情编辑", () => {
  const parentWithSubstitute = {
    ...ovFix(),
    substitutes: [{
      pn_std: "SUB-001", description: "旧描述", source: "manual", relation: "互替",
      via: null, stock_qty: 3,
    }],
  };
  const targetOverview = {
    ...ovFix(99, "SUB-001"),
    part: {
      ...ovFix(99, "SUB-001").part,
      description: "目标型号描述",
      category_major: "服务器配件",
      category_minor: "磁盘",
    },
  };

  it("管理员点击通用号 PN 只进入目标型号，修改入口位于目标详情的描述旁", async () => {
    fetchOverview.mockImplementation(async (key: any) =>
      key.pn_std === "SUB-001" ? targetOverview : parentWithSubstitute);

    renderAt("/parts?part_id=42");
    const viewButton = await screen.findByRole("button", { name: "查看型号 SUB-001" });
    expect(screen.queryByRole("button", { name: "编辑备件 SUB-001" })).toBeNull();
    await clickAndFlush(viewButton);

    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ pn_std: "SUB-001" }));
    await waitFor(() => expect(curLoc.search).toContain("part_id=99"));
    expect(await screen.findByText("目标型号描述")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "修改型号 SUB-001 的描述和品类" })).toBeInTheDocument();
    expect(screen.queryByText("就地编辑备件 SUB-001")).toBeNull();
    expect(masterEdit).not.toHaveBeenCalled();
  });

  it("有主数据权限的人从详情描述旁修改，保存后刷新当前型号而不跳回父型号", async () => {
    fetchOverview.mockImplementation(async (key: any) =>
      key.pn_std === "SUB-001" ? targetOverview : parentWithSubstitute);
    masterCategories.mockResolvedValue({
      data: {
        categories: [{ code: "01", name: "存储设备", children: [{ code: "0101", name: "硬盘" }] }],
      },
    });
    masterEdit.mockResolvedValue({
      data: {
        id: 99, pn_std: "SUB-001",
        updated: ["description", "category_major", "category_minor"],
        locked_fields: ["description", "category_major", "category_minor"],
      },
    });

    renderAt("/parts?part_id=42");
    const viewButton = await screen.findByRole("button", { name: "查看型号 SUB-001" });
    expect(viewButton.closest(".ant-table")).toHaveClass("ant-table-scroll-horizontal");
    expect(within(screen.getByRole("region", { name: "更多价格统计" })).getByText("平均销售价"))
      .toBeVisible();
    expect(viewButton).toHaveAttribute("type", "button");
    viewButton.focus();
    expect(viewButton).toHaveFocus();
    await clickAndFlush(viewButton);

    await waitFor(() => expect(curLoc.search).toContain("part_id=99"));
    const editButton = await screen.findByRole("button", { name: "修改型号 SUB-001 的描述和品类" });
    expect(editButton).toHaveAttribute("type", "button");
    editButton.focus();
    expect(editButton).toHaveFocus();
    fireEvent.click(editButton);

    await screen.findByText("就地编辑备件 SUB-001");
    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ pn_std: "SUB-001" }));
    fireEvent.change(screen.getByLabelText("描述"), { target: { value: "新描述：2TB SAS 企业盘" } });

    fireEvent.mouseDown(within(screen.getByRole("dialog")).getByLabelText("一级品类"));
    await screen.findByRole("option", { name: "存储设备" });
    const majorOption = screen.getAllByText("存储设备")
      .find((node) => node.closest(".ant-select-item-option"));
    fireEvent.click(majorOption!.closest(".ant-select-item-option")!);
    fireEvent.mouseDown(within(screen.getByRole("dialog")).getByLabelText("二级品类"));
    await screen.findByRole("option", { name: "硬盘" });
    const minorOption = screen.getAllByText("硬盘")
      .find((node) => node.closest(".ant-select-item-option"));
    fireEvent.click(minorOption!.closest(".ant-select-item-option")!);
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => expect(masterEdit).toHaveBeenCalledWith({
      part_id: 99,
      pn_std: "SUB-001",
      base_snapshot: editorState(targetOverview.part),
      description: "新描述：2TB SAS 企业盘",
      category_major: "存储设备",
      category_minor: "硬盘",
    }, "admin-token"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "就地编辑备件 SUB-001" })).toBeNull());
    expect(curLoc.search).toContain("part_id=99");
    expect(curLoc.search).not.toContain("pn=SUB-001");
    expect(fetchOverview.mock.calls.filter(([key]) => key.part_id === 99)).toHaveLength(1);
  });

  it("采购账号有主数据编辑权限时，在详情描述旁看到同一修改入口", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({ page_master_data: true }));
    fetchOverview.mockImplementation(async (key: any) =>
      key.pn_std === "SUB-001" ? targetOverview : parentWithSubstitute);

    renderAt("/parts?part_id=42");
    const viewButton = await screen.findByRole("button", { name: "查看型号 SUB-001" });
    expect(screen.queryByRole("button", { name: "编辑备件 SUB-001" })).toBeNull();
    await clickAndFlush(viewButton);

    await waitFor(() => expect(fetchOverview).toHaveBeenCalledWith({ pn_std: "SUB-001" }));
    await waitFor(() => expect(curLoc.search).toContain("part_id=99"));
    expect(screen.getByRole("button", { name: "修改型号 SUB-001 的描述和品类" })).toBeInTheDocument();
    expect(screen.queryByText("就地编辑备件 SUB-001")).toBeNull();
    expect(masterEdit).not.toHaveBeenCalled();
  });

  it("没有主数据编辑权限的人可查看型号，但详情描述旁没有修改入口", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({ page_master_data: false }));
    fetchOverview.mockImplementation(async (key: any) =>
      key.pn_std === "SUB-001" ? targetOverview : parentWithSubstitute);

    renderAt("/parts?part_id=42");
    await clickAndFlush(await screen.findByRole("button", { name: "查看型号 SUB-001" }));

    await waitFor(() => expect(curLoc.search).toContain("part_id=99"));
    expect(await screen.findByText("目标型号描述")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "修改型号 SUB-001 的描述和品类" })).toBeNull();
  });

  it("当前登录周期无主数据权限时，篡改 localStorage 后重渲染也不会点亮修改入口", async () => {
    localStorage.setItem("role", "sales");
    localStorage.setItem("permissions", JSON.stringify({ page_master_data: false }));
    fetchOverview.mockResolvedValue(targetOverview);

    renderAt("/parts?part_id=99");
    expect(await screen.findByText("目标型号描述")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "修改型号 SUB-001 的描述和品类" })).toBeNull();

    localStorage.setItem("permissions", JSON.stringify({ page_master_data: true }));
    fireEvent.change(screen.getByPlaceholderText(/输入型号/), {
      target: { value: "触发无关重渲染" },
    });

    expect(screen.queryByRole("button", { name: "修改型号 SUB-001 的描述和品类" })).toBeNull();
  });

  it("没有主数据编辑权限时，即使父级误传可编辑也不会加载或提交", () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({ page_master_data: false }));

    render(
      <InlinePartEditModal
        open
        canEdit
        contextKey="parent-42"
        pn="SUB-001"
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );

    expect(screen.queryByRole("dialog", { name: "就地编辑备件 SUB-001" })).toBeNull();
    expect(fetchOverview).not.toHaveBeenCalled();
    expect(masterCategories).not.toHaveBeenCalled();
    expect(masterEdit).not.toHaveBeenCalled();
  });

  it("编辑器打开后 token 先切换，role 尚未更新时保存仍会失败关闭", async () => {
    fetchOverview.mockResolvedValue(targetOverview);

    render(
      <InlinePartEditModal
        open
        canEdit
        contextKey="parent-42"
        pn="SUB-001"
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );

    await screen.findByDisplayValue("目标型号描述");
    fireEvent.change(screen.getByLabelText("描述"), { target: { value: "不应提交" } });
    localStorage.setItem("token", "purchaser-token");
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

    expect(masterEdit).not.toHaveBeenCalled();
  });

  it("旧型号保存迟到时，不会关闭或刷新后来打开的编辑会话", async () => {
    const delayedSave = deferred<{ data: {
      id: number; pn_std: string; updated: string[]; locked_fields: string[];
    } }>();
    const secondOverview = {
      ...ovFix(100, "SUB-002"),
      part: {
        ...ovFix(100, "SUB-002").part,
        description: "第二型号描述",
        category_major: "服务器配件",
        category_minor: "磁盘",
      },
    };
    fetchOverview.mockImplementation(async (key: any) =>
      key.pn_std === "SUB-002" ? secondOverview : targetOverview);
    masterEdit.mockReturnValueOnce(delayedSave.promise);
    const onClose = vi.fn();
    const onSaved = vi.fn();

    const view = render(
      <InlinePartEditModal
        open
        canEdit
        contextKey="parent-42"
        pn="SUB-001"
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    await screen.findByDisplayValue("目标型号描述");
    fireEvent.change(screen.getByLabelText("描述"), { target: { value: "型号一新描述" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(masterEdit).toHaveBeenCalled());

    view.rerender(
      <InlinePartEditModal
        open
        canEdit
        contextKey="parent-42"
        pn="SUB-002"
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    expect(await screen.findByRole("dialog", { name: "就地编辑备件 SUB-002" })).toBeInTheDocument();
    await screen.findByDisplayValue("第二型号描述");

    delayedSave.resolve({
      data: {
        id: 99,
        pn_std: "SUB-001",
        updated: ["description"],
        locked_fields: ["description"],
      },
    });
    await act(async () => { await delayedSave.promise; });

    expect(screen.getByRole("dialog", { name: "就地编辑备件 SUB-002" })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("保存期间切换父型号，旧响应不会把页面带回原父型号", async () => {
    const delayedSave = deferred<{ data: {
      id: number; pn_std: string; updated: string[]; locked_fields: string[];
    } }>();
    const secondParent = {
      ...ovFix(77, "PARENT-2"),
      part: { ...ovFix(77, "PARENT-2").part, description: "第二父型号" },
    };
    fetchOverview.mockImplementation(async (key: any) => {
      if (key.pn_std === "SUB-001") return targetOverview;
      if (key.part_id === 77) return secondParent;
      return parentWithSubstitute;
    });
    masterEdit.mockReturnValueOnce(delayedSave.promise);

    renderAt("/parts?part_id=42");
    await clickAndFlush(await screen.findByRole("button", { name: "查看型号 SUB-001" }));
    await waitFor(() => expect(curLoc.search).toContain("part_id=99"));
    fireEvent.click(await screen.findByRole("button", { name: "修改型号 SUB-001 的描述和品类" }));
    await screen.findByDisplayValue("目标型号描述");
    fireEvent.change(screen.getByLabelText("描述"), { target: { value: "迟到保存" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(masterEdit).toHaveBeenCalled());

    act(() => nav("/parts?part_id=77"));
    expect(await screen.findByText("第二父型号")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "就地编辑备件 SUB-001" })).toBeNull();

    delayedSave.resolve({
      data: {
        id: 99,
        pn_std: "SUB-001",
        updated: ["description"],
        locked_fields: ["description"],
      },
    });
    await act(async () => { await delayedSave.promise; });

    expect(curLoc.search).toContain("part_id=77");
    expect(screen.getByText("第二父型号")).toBeInTheDocument();
    expect(fetchOverview.mock.calls.filter(([key]) => key.part_id === 77)).toHaveLength(1);
  });

  it("保存成功后的当前型号刷新迟到时，离开详情页不会复活旧型号", async () => {
    const delayedRefresh = deferred<typeof targetOverview>();
    let targetRefreshes = 0;
    fetchOverview.mockImplementation(async (key: any) => {
      if (key.pn_std === "SUB-001") return targetOverview;
      if (key.part_id === 99) {
        targetRefreshes += 1;
        return delayedRefresh.promise;
      }
      if (key.part_id === 42) return parentWithSubstitute;
      return ovFix(key.part_id);
    });

    renderAt("/parts?part_id=42");
    await clickAndFlush(await screen.findByRole("button", { name: "查看型号 SUB-001" }));
    await waitFor(() => expect(curLoc.search).toContain("part_id=99"));
    fireEvent.click(await screen.findByRole("button", { name: "修改型号 SUB-001 的描述和品类" }));
    await screen.findByDisplayValue("目标型号描述");
    fireEvent.change(screen.getByLabelText("描述"), { target: { value: "已保存但刷新未回" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(targetRefreshes).toBe(1));

    act(() => nav("/parts"));
    await screen.findByText("搜索并点击型号查看全景");
    delayedRefresh.resolve(targetOverview);
    await act(async () => { await delayedRefresh.promise; });

    expect(curLoc.search).toBe("");
    expect(screen.queryByText("型号全景：")).toBeNull();
  });
});
