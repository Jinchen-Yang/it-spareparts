/** 池分析详情页（/pool-analysis/:groupId 独立深链）行为测试。
 *
 * 断言口径：
 * - 深链打开即按 URL 窗口取数（刷新语义等价）；非法 groupId 走 404 空态不请求；
 * - 采购/销售股票式价格区间图：筛选、排序、点击和竞态；
 * - 订单板块点单号 → 弹窗按单号精确召回订单内容；
 * - 治理受限：约束价显示「无权限」，与「未设置」分离。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { Grid } from "antd";
import type { PoolPriceMapResponse } from "../../api/poolAnalysis";

const fetchPoolAnalysis = vi.fn();
const fetchPoolPriceMap = vi.fn();
const fetchPoolAnalysisOrderDetail = vi.fn();
const dashboardPurchaseOrders = vi.fn();
const dashboardSales = vi.fn();

vi.mock("../../api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
  api: { get: vi.fn(), post: vi.fn() },
  dashboardPurchaseOrders: (...a: unknown[]) => dashboardPurchaseOrders(...a),
  dashboardSales: (...a: unknown[]) => dashboardSales(...a),
}));
vi.mock("../../api/poolAnalysis", () => ({
  fetchPoolAnalysis: (...a: unknown[]) => fetchPoolAnalysis(...a),
  fetchPoolPriceMap: (...a: unknown[]) => fetchPoolPriceMap(...a),
  fetchPoolAnalysisOrderDetail: (...a: unknown[]) => fetchPoolAnalysisOrderDetail(...a),
}));
vi.mock("../../components/charts/PoolPnPriceMap", async () => {
  const React = await import("react");
  return {
    default: (p: { data: { side: string; price_restricted: boolean;
      members: Array<{ part_id: number; pn_std?: string | null }> };
      onPartOpen?: (partId: number) => void; isMobile?: boolean }) => {
      const [selectedPartId, setSelectedPartId] = React.useState<number | null>(null);
      const selected = p.data.members.find((member) => member.part_id === selectedPartId);
      const activate = (partId: number) => p.isMobile
        ? setSelectedPartId(partId) : p.onPartOpen?.(partId);
      return <div data-testid="price-map-stub" data-side={p.data.side}
        data-restricted={String(p.data.price_restricted)} data-clickable={String(!!p.onPartOpen)}
        data-mobile={String(!!p.isMobile)}>
        <button onClick={() => activate(p.data.members[0].part_id)}>查看图中型号全景</button>
        <div data-testid="price-map-equivalent-table">
          {p.data.members.map((member) => <button key={member.part_id}
            onClick={() => activate(member.part_id)}>
            选择 {member.pn_std ?? `#${member.part_id}`}
          </button>)}
        </div>
        {selected && <div data-testid="price-map-selected">已选择 {selected.pn_std}
          <button onClick={() => p.onPartOpen?.(selected.part_id)}>查看型号全景</button>
        </div>}
      </div>;
    },
  };
});

import PoolAnalysisPage from "../PoolAnalysisPage";

const breakpoint = vi.spyOn(Grid, "useBreakpoint");

const metrics = (amount: number, avg: number) => ({
  total_amount: amount, total_quantity: 10, weighted_avg_unit_price: avg,
  order_count: 3, latest_date: "2026-07-01",
  pool_avg_delta: null, pool_avg_delta_pct: null,
  manual_limit_delta: null, manual_limit_delta_pct: null,
});
const priceStats = (amount: number, average: number) => ({
  weighted_avg: average, median: average - 2, min: average - 10, max: average + 10,
  latest: average, total_amount: amount, total_qty: 10, order_count: 3, line_count: 4,
  latest_date: "2026-07-01", violation_count: 1,
});
const reference = (amount: number, average: number, partAverage: number | null = null) => ({
  restricted: false,
  pool_stats: priceStats(amount, average),
  part_stats: partAverage == null ? null : priceStats(partAverage * 10, partAverage),
  constraint: { status: "set" as const, value: average + 5 },
  delta_to_pool_avg: partAverage == null ? null : partAverage - average,
  delta_to_constraint: partAverage == null ? null : partAverage - average - 5,
  relation_to_constraint: partAverage == null ? null : "above" as const,
});

const DETAIL = {
  group_id: 12, member_count: 2, needs_calibration: false, oversized: false,
  name: "内存互通池", description: "测试池",
  window: { date_from: "2026-06-01", date_to: "2026-06-30", as_of: "2026-07-15" },
  demand: { total_qty: 20, total_revenue_ex_tax: 4000, note: "" },
  supply_window: { date_from: "2025-07-15", date_to: "2026-07-15", as_of: "2026-07-15" },
  benchmark: { cost_part_id: 101, cost_ex_tax: 90, low_confidence: false, supply_ok: true,
    sale_part_id: 101, sale_ex_tax: 180 },
  savings: { theoretical_max: 500, supply_available_upper: 300, executable: null,
    label: "潜在降本机会（只读）", opportunities: [] },
  members: [
    { part_id: 101, pn_std: "PN-A", description: "内存 16G", brand: "三星",
      purchase_price: { wavg: 90, supply: { purchase_orders: 3, suppliers: 2 } },
      sale_price: { wavg: 180, qty_sold: 12 },
      purchase_premium_pct: 0, sale_premium_pct: 0,
      brand_premium_purchase: false, brand_premium_sale: false,
      purchase_metrics: metrics(900, 90), sales_metrics: metrics(2160, 180),
      purchase_reference: reference(2000, 100, 90), sales_reference: reference(3760, 188, 180) },
    { part_id: 102, pn_std: "PN-B", description: "内存 16G 兼容", brand: "金士顿",
      purchase_price: { wavg: 110, supply: null },
      sale_price: { wavg: 200, qty_sold: 8 },
      purchase_premium_pct: 0.22, sale_premium_pct: 0.11,
      brand_premium_purchase: true, brand_premium_sale: false,
      purchase_metrics: metrics(1100, 110), sales_metrics: metrics(1600, 200),
      purchase_reference: reference(2000, 100, 110), sales_reference: reference(3760, 188, 200) },
  ],
  customer_cross_brand: { restricted: false, multi_brand_customers: 1,
    customers: [{ customer: "客户甲", brand_count: 2, concentration: 0.6 }] },
  purchase_metrics: metrics(2000, 100), sales_metrics: metrics(3760, 188),
  purchase_reference: reference(2000, 100), sales_reference: reference(3760, 188),
  max_purchase_price: 105, min_sale_price: 160,
  purchase_violation_count: 1, sale_violation_count: null,
  manual_reference_restricted: false,
  purchase_orders: { restricted: false, total: 1, page: 1, page_size: 20, items: [
    { order_id: 77, order_no: "CG-77", order_date: "2026-06-10", purchaser: "张三", supplier: "供应商A",
      source_type: "销售订单", line_id: 1, part_id: 101, pn_std: "PN-A",
      quantity: 5, purchase_unit_price_ex_tax: 95, purchase_line_value_ex_tax: 475 }] },
  sales_orders: { restricted: false, total: 1, page: 1, page_size: 20, items: [
    { order_id: 88, order_no: "XS-88", order_date: "2026-06-12", salesperson: "李四", customer: "客户甲",
      business_type: "备件销售", line_id: 9, part_id: 102, pn_std: "PN-B",
      quantity: 2, sale_unit_price_ex_tax: 210, sale_line_value_ex_tax: 420, counts_revenue: true }] },
};

const PRICE_MAP = {
  contract_version: 1 as const, side: "purchase" as const, basis: "ex_tax" as const,
  price_restricted: false,
  pool: { group_id: 12, name: "内存互通池", member_count: 2 },
  window: { ...DETAIL.window, range: "90d" },
  filters: { purchase_type: null, employee: null },
  sort: "pn" as const, order: "asc" as const, effective_sort: "pn" as const,
  effective_order: "asc" as const,
  current_constraint: { status: "set" as const, value: 105, changed_at: null, input_basis: "ex_tax" as const },
  pool_stats: { weighted_avg: 100, median: 98, min: 80, max: 120, latest: 110,
    total_qty: 20, order_count: 4, line_count: 5, latest_date: "2026-06-30" },
  excluded: { inactive_orders: 0, nonpositive_price: 0, nonpositive_qty: 0, future_orders: 0,
    non_revenue_sales: 0, suspected_records: 0, confirmed_source_error_excluded: 0 },
  members: DETAIL.members.map((member) => ({ part_id: member.part_id, pn_std: member.pn_std,
    description: member.description, brand: member.brand,
    stats: { weighted_avg: member.purchase_metrics.weighted_avg_unit_price,
      median: member.purchase_metrics.weighted_avg_unit_price, min: 80, max: 120, latest: 110,
      total_qty: 10, order_count: 3, line_count: 4, latest_date: "2026-06-30" },
    current_reference: { relation: "below" as const, delta_amount: -5, delta_pct: -0.0476 },
    latest_raw_record: null, quality_counts: { suspected: 0, confirmed_source_error: 0 } })),
};

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/pool-analysis/:groupId" element={<PoolAnalysisPage />} />
        <Route path="/boss" element={<div>看板桩</div>} />
        <Route path="/pools" element={<><div>池列表桩</div><LocationProbe /></>} />
        <Route path="/parts" element={<><div>型号查询页桩</div><LocationProbe /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

async function expandPriceRange() {
  const button = await screen.findByRole("button", {
    name: /展开成员(?:采购|销售)价格区间/,
  });
  fireEvent.click(button);
  expect(button).toHaveAttribute("aria-expanded", "true");
}

let currentPath = "";
function LocationProbe() {
  const location = useLocation();
  currentPath = `${location.pathname}${location.search}`;
  return null;
}

beforeEach(() => {
  vi.clearAllMocks();
  breakpoint.mockReturnValue({ xs: false, sm: true, md: true, lg: true, xl: true, xxl: true });
  localStorage.clear();
  localStorage.setItem("role", "admin");
  fetchPoolAnalysis.mockResolvedValue(DETAIL);
  fetchPoolPriceMap.mockImplementation((_groupId: number, params: {
    side?: "purchase" | "sales"; range?: string; date_from?: string; date_to?: string;
    purchase_type?: string; employee?: string; sort?: string; order?: string;
  }) => Promise.resolve({
    ...PRICE_MAP,
    side: params.side ?? "purchase",
    window: {
      ...PRICE_MAP.window,
      range: params.date_from && params.date_to ? "custom" : (params.range ?? "90d"),
      date_from: params.date_from ?? PRICE_MAP.window.date_from,
      date_to: params.date_to ?? PRICE_MAP.window.date_to,
    },
    filters: { purchase_type: params.purchase_type ?? null, employee: params.employee ?? null },
    sort: params.sort ?? "pn", order: params.order ?? "asc",
  }));
  fetchPoolAnalysisOrderDetail.mockResolvedValue({
    side: "purchase", price_restricted: false,
    supplier_restricted: false, customer_restricted: false,
    order: { order_id: 77, order_no: "CG-77", order_date: "2026-06-10",
      purchaser: "张三", supplier: "供应商A", source_type: "销售订单",
      data_status: "已生效", purchase_order_amount_ex_tax: 475 },
    items: [{ line_id: 1, part_id: 101, pn_std: "PN-A", description: "内存 16G",
      brand: "三星", quantity: 5, unit: "个", purchase_original_unit_price: 95,
      purchase_unit_price_ex_tax: 95, purchase_line_value_ex_tax: 475,
      anomaly_flags: [], pool_group_id: 12, pool_name: "内存互通池" }],
  });
  dashboardPurchaseOrders.mockResolvedValue({ data: {
    contract_version: 2,
    total: 1, page: 1, page_size: 20, as_of: "2026-07-15", effective_sort: "order_date",
    ranking_restricted: false, cost_restricted: false, manual_reference_restricted: false,
    items: [{ order_id: 77, order_no: "CG-77", order_date: "2026-06-10", occurred_date: "2026-06-10",
      is_future: false, purchaser: "张三", source_type: "销售订单", data_status: "已生效",
      linked_sales_order: null, part_count: 1, pn_count: 1, total_qty: 5, total_quantity: 5,
      total_ex_tax: 475, total_amount: 475, pn_preview: ["PN-A"],
      parts: [{ line_id: 1, part_id: 101, pn_std: "PN-A", description: "内存 16G", brand: "三星",
        quantity: 5, unit_price_ex_tax: 95, amount: 475, in_stats_scope: true,
        pool_group_id: 12, pool_name: "内存互通池", pool_avg_purchase_price: 100,
        max_purchase_price: 105, reference_status: "within_limit",
        pool_avg_delta: -5, pool_avg_delta_pct: -0.05,
        manual_limit_delta: -10, manual_limit_delta_pct: -0.1 }] }] } });
});
afterEach(cleanup);

describe("页面信息层级", () => {
  it("成员、采购订单、销售订单先于底部价格区间，且价格区间默认折叠", async () => {
    renderAt("/pool-analysis/12");
    const memberSection = await screen.findByRole("region", { name: "成员型号" });
    const purchaseSection = screen.getByRole("region", { name: "采购订单" });
    const salesSection = screen.getByRole("region", { name: "销售订单" });
    const priceRangeSection = screen.getByRole("region", { name: "成员采购价格区间" });

    expect(memberSection.compareDocumentPosition(purchaseSection)
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(purchaseSection.compareDocumentPosition(salesSection)
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(salesSection.compareDocumentPosition(priceRangeSection)
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("button", { name: "展开成员采购价格区间" }))
      .toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("price-map-stub")).toBeNull();
    expect(screen.queryByLabelText("价格图区间排序")).toBeNull();
    expect(screen.queryByRole("button", { name: "切换价格图排序方向" })).toBeNull();
    await waitFor(() => expect(fetchPoolPriceMap).toHaveBeenCalled());
  });

  it("主动展开后恢复价格图与原排序能力", async () => {
    renderAt("/pool-analysis/12");
    await expandPriceRange();
    expect(await screen.findByTestId("price-map-stub")).toHaveAttribute("data-side", "purchase");
    expect(screen.getByLabelText("价格图区间排序")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "切换价格图排序方向" })).toBeEnabled();
  });
});

describe("深链与取数", () => {
  it("没有自定义日期时按 range 深链重放统计窗口", async () => {
    renderAt("/pool-analysis/12?range=365d");
    await waitFor(() => expect(fetchPoolAnalysis).toHaveBeenCalledWith(12, {
      range: "365d",
      purchase_page: 1, sales_page: 1, orders_page_size: 20,
    }));
  });

  it("完整 from/to 优先于 range，不把两套窗口同时发给后端", async () => {
    renderAt("/pool-analysis/12?range=365d&from=2026-06-01&to=2026-06-30");
    await waitFor(() => expect(fetchPoolAnalysis).toHaveBeenCalledWith(12, {
      date_from: "2026-06-01", date_to: "2026-06-30",
      purchase_page: 1, sales_page: 1, orders_page_size: 20,
    }));
  });

  it("side/pn 深链下发并定位当前方向与成员，不丢筛选", async () => {
    renderAt("/pool-analysis/12?range=90d&side=sales&pn=PN-B");
    await waitFor(() => expect(fetchPoolAnalysis).toHaveBeenCalledWith(12, {
      range: "90d", side: "sales", pn: "PN-B",
      purchase_page: 1, sales_page: 1, orders_page_size: 20,
    }));
    expect(screen.getByLabelText("当前关注方向")).toHaveTextContent("销售");
    const rows = await screen.findAllByRole("row");
    const memberRows = rows.filter((row) => row.textContent?.includes("PN-A") || row.textContent?.includes("PN-B"));
    expect(memberRows[0]).toHaveTextContent("PN-B");
    expect(memberRows[0]).toHaveTextContent("当前型号");
    expect(screen.getByText("成员销售价格区间").parentElement).toHaveTextContent("当前关注");
  });

  it("采购类型深链下发接口，未知值可显示，并由返回链接原样保留", async () => {
    renderAt("/pool-analysis/12?range=365d&side=sales&pn=PN-B&purchase_type=临时联合采购");
    await waitFor(() => expect(fetchPoolAnalysis).toHaveBeenCalledWith(12, {
      range: "365d", side: "sales", pn: "PN-B", purchase_type: "临时联合采购",
      purchase_page: 1, sales_page: 1, orders_page_size: 20,
    }));
    expect(screen.getByText("临时联合采购")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回互通池" }));
    await screen.findByText("池列表桩");
    const query = new URLSearchParams(currentPath.split("?")[1]);
    expect(query.get("range")).toBe("365d");
    expect(query.get("side")).toBe("sales");
    expect(query.get("pn")).toBe("PN-B");
    expect(query.get("purchase_type")).toBe("临时联合采购");
  });

  it("详情采购类型 Select 可输入未知值，并重置订单分页后重查", async () => {
    renderAt("/pool-analysis/12?pp=3&spg=2");
    await screen.findByText("内存互通池");
    const input = screen.getByRole("combobox", { name: "采购类型" });
    fireEvent.change(input, { target: { value: "全新采购流程" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13, which: 13 });
    await waitFor(() => expect(fetchPoolAnalysis).toHaveBeenLastCalledWith(12, expect.objectContaining({
      purchase_type: "全新采购流程", purchase_page: 1, sales_page: 1,
    })));
  });

  it("全员详情不再渲染旧推荐与节省语义", async () => {
    renderAt("/pool-analysis/12");
    await screen.findByText("内存互通池");
    expect(screen.queryByText(/潜在降本|理论节省|供应层面上限|性价比标杆/)).toBeNull();
  });

  it("成员 PN 与订单号使用原生可聚焦控件，支持键盘继续操作", async () => {
    renderAt("/pool-analysis/12?pn=PN-B");
    const pn = (await screen.findAllByRole("link", { name: "查看型号 PN-B 全景" }))[0];
    pn.focus();
    expect(document.activeElement).toBe(pn);
    expect(pn.tabIndex).toBe(0);
    const order = screen.getByRole("button", { name: "查看订单 CG-77 内容" });
    order.focus();
    expect(document.activeElement).toBe(order);
    expect(order.tabIndex).toBe(0);
  });

  it("/pool-analysis/12?from=&to= 打开即按窗口取数并渲染池信息", async () => {
    renderAt("/pool-analysis/12?from=2026-06-01&to=2026-06-30");
    await waitFor(() => expect(fetchPoolAnalysis).toHaveBeenCalledWith(12, {
      date_from: "2026-06-01", date_to: "2026-06-30",
      purchase_page: 1, sales_page: 1, orders_page_size: 20 }));
    await screen.findByText("内存互通池");
    expect(screen.getByText("人工最高采购价")).toBeInTheDocument();
    expect(screen.getByText("¥105")).toBeInTheDocument();
  });

  it("非法 groupId：404 空态，不发请求", async () => {
    renderAt("/pool-analysis/abc");
    await screen.findByText("无效的池编号");
    expect(fetchPoolAnalysis).not.toHaveBeenCalled();
  });

  it("不可能日期显示明确错误且不查询，避免 422 或扩大到全历史", async () => {
    renderAt("/pool-analysis/12?from=2026-02-31&to=2026-03-31");
    await screen.findByText("无效的统计时间范围");
    expect(fetchPoolAnalysis).not.toHaveBeenCalled();
  });

  it("from>to 的反向窗口显示明确错误且不查询", async () => {
    renderAt("/pool-analysis/12?from=2026-06-30&to=2026-06-01");
    await screen.findByText("无效的统计时间范围");
    expect(fetchPoolAnalysis).not.toHaveBeenCalled();
  });

  it("未知 range 显示明确错误且不查询，避免静默扩大范围", async () => {
    renderAt("/pool-analysis/12?range=forever");
    await screen.findByText("无效的统计时间范围");
    expect(fetchPoolAnalysis).not.toHaveBeenCalled();
  });
});

describe("股票式价格区间图", () => {
  it("按 URL 窗口、方向、采购类型与经办人请求同一 price-map 契约", async () => {
    renderAt("/pool-analysis/12?side=sales&range=365d&purchase_type=补库&employee=张三");
    await waitFor(() => expect(fetchPoolPriceMap).toHaveBeenCalledWith(12, {
      side: "sales", range: "365d", purchase_type: "补库", employee: "张三",
      sort: "pn", order: "asc",
    }));
    await expandPriceRange();
    expect(await screen.findByTestId("price-map-stub")).toHaveAttribute("data-side", "sales");
  });

  it("桌面图中型号一次点击进入全景，并携带完整池分析上下文", async () => {
    renderAt("/pool-analysis/12?from=2026-06-01&to=2026-06-30&side=purchase&purchase_type=补库&employee=张三&price_sort=weighted_avg&price_order=desc");
    await screen.findByText("内存互通池");
    await expandPriceRange();
    fireEvent.click(await screen.findByRole("button", { name: "查看图中型号全景" }));
    expect(await screen.findByText("型号查询页桩")).toBeInTheDocument();
    const query = new URLSearchParams(currentPath.split("?")[1]);
    expect(currentPath.split("?")[0]).toBe("/parts");
    expect(Object.fromEntries(query)).toEqual({
      part_id: "101", group_id: "12", range: "custom",
      date_from: "2026-06-01", date_to: "2026-06-30", side: "purchase",
      purchase_type: "补库", employee: "张三",
      price_sort: "weighted_avg", price_order: "desc",
    });
    expect(screen.queryByText("成员 PN-A 详情")).toBeNull();
  });

  it("移动端图形先开固定卡，卡片按钮再携上下文进入型号全景", async () => {
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    renderAt("/pool-analysis/12?range=365d&side=sales&employee=李四");
    await expandPriceRange();
    const map = await screen.findByTestId("price-map-stub");
    expect(map).toHaveAttribute("data-mobile", "true");
    fireEvent.click(within(map).getByRole("button", { name: "查看图中型号全景" }));
    expect(within(map).getByTestId("price-map-selected")).toHaveTextContent("PN-A");
    fireEvent.click(within(map).getByRole("button", { name: "查看型号全景" }));
    await screen.findByText("型号查询页桩");
    const query = new URLSearchParams(currentPath.split("?")[1]);
    expect(Object.fromEntries(query)).toEqual({
      part_id: "101", group_id: "12", range: "365d", side: "sales", employee: "李四",
    });
  });

  it("确定性延迟下旧采购响应最后到达也不能覆盖新销售图", async () => {
    let releasePurchase: ((value: typeof PRICE_MAP) => void) | undefined;
    fetchPoolPriceMap.mockImplementation((_groupId: number, params: { side?: "purchase" | "sales" }) => {
      if (params.side === "sales") return Promise.resolve({ ...PRICE_MAP, side: "sales" as const });
      return new Promise<typeof PRICE_MAP>((resolve) => { releasePurchase = resolve; });
    });
    renderAt("/pool-analysis/12?side=purchase");
    await screen.findByText("内存互通池");
    await expandPriceRange();
    fireEvent.click(within(screen.getByLabelText("当前关注方向")).getByText("销售"));
    await waitFor(() => expect(screen.getByTestId("price-map-stub")).toHaveAttribute("data-side", "sales"));
    await act(async () => releasePurchase?.(PRICE_MAP));
    expect(screen.getByTestId("price-map-stub")).toHaveAttribute("data-side", "sales");
  });

  it("scope 切换立即隐藏旧表和固定详情，新响应不得继承旧 PN 选择", async () => {
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    let releaseSales: ((value: PoolPriceMapResponse) => void) | undefined;
    fetchPoolPriceMap.mockImplementation((_groupId: number, params: { side?: "purchase" | "sales" }) => {
      if (params.side === "sales") {
        return new Promise<PoolPriceMapResponse>((resolve) => { releaseSales = resolve; });
      }
      return Promise.resolve(PRICE_MAP);
    });
    renderAt("/pool-analysis/12?side=purchase");
    await expandPriceRange();
    const purchaseMap = await screen.findByTestId("price-map-stub");
    fireEvent.click(within(purchaseMap).getByRole("button", { name: "选择 PN-A" }));
    expect(within(purchaseMap).getByTestId("price-map-selected")).toHaveTextContent("PN-A");

    fireEvent.click(within(screen.getByLabelText("当前关注方向")).getByText("销售"));
    expect(screen.queryByTestId("price-map-stub")).toBeNull();
    expect(screen.queryByTestId("price-map-equivalent-table")).toBeNull();
    expect(screen.queryByTestId("price-map-selected")).toBeNull();
    expect(screen.getByText("正在加载价格区间…")).toBeInTheDocument();

    await act(async () => releaseSales?.({ ...PRICE_MAP, side: "sales" }));
    const salesMap = await screen.findByTestId("price-map-stub");
    expect(salesMap).toHaveAttribute("data-side", "sales");
    expect(within(salesMap).queryByTestId("price-map-selected")).toBeNull();
  });

  it("价格图排序和方向进入 URL 驱动请求", async () => {
    renderAt("/pool-analysis/12");
    await expandPriceRange();
    await screen.findByTestId("price-map-stub");
    fireEvent.click(within(screen.getByLabelText("价格图区间排序")).getByText("加权均价"));
    await waitFor(() => expect(fetchPoolPriceMap).toHaveBeenLastCalledWith(12,
      expect.objectContaining({ sort: "weighted_avg", order: "desc" })));
    fireEvent.click(screen.getByRole("button", { name: "切换价格图排序方向" }));
    await waitFor(() => expect(fetchPoolPriceMap).toHaveBeenLastCalledWith(12,
      expect.objectContaining({ sort: "weighted_avg", order: "asc" })));
  });
});

describe("订单板块", () => {
  it("点采购单号 → 专用池分析端点按唯一 order_id 召回完整订单", async () => {
    renderAt("/pool-analysis/12?from=2026-06-01&to=2026-06-30");
    const link = await screen.findByRole("button", { name: "查看订单 CG-77 内容" });
    expect(link.tagName).toBe("BUTTON");
    fireEvent.click(link);
    await waitFor(() => expect(fetchPoolAnalysisOrderDetail).toHaveBeenCalledWith("purchase", 77));
    const dialog = await screen.findByRole("dialog");
    expect((dialog.closest(".ant-modal") ?? dialog).getAttribute("style"))
      .toContain("calc(100vw - 16px)");
    await within(dialog).findByText("采购订单 CG-77");
    expect(within(dialog).getByText("供应商A")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "查看互通池 内存互通池" }))
      .toHaveAttribute("href", "/pool-analysis/12?range=custom&from=2026-06-01&to=2026-06-30&pn=PN-A&side=purchase");
  });

  it("专用订单详情失败时给出明确错误，不回退旧老板端点", async () => {
    fetchPoolAnalysisOrderDetail.mockRejectedValueOnce(new Error("boom"));
    renderAt("/pool-analysis/12");
    fireEvent.click(await screen.findByRole("button", { name: "查看订单 CG-77 内容" }));
    expect(await screen.findByText("订单详情加载失败，请稍后重试")).toBeInTheDocument();
    expect(dashboardPurchaseOrders).not.toHaveBeenCalled();
  });

  it("专用订单治理受限时保留单号经办人，但金额与供应商明确隐藏", async () => {
    fetchPoolAnalysisOrderDetail.mockResolvedValueOnce({
      side: "purchase", price_restricted: true,
      supplier_restricted: true, customer_restricted: false,
      order: { order_id: 77, order_no: "CG-77", order_date: "2026-06-10",
        purchaser: "张三", supplier: null, source_type: "销售订单", data_status: "已生效",
        purchase_order_amount_ex_tax: null },
      items: [{ line_id: 1, part_id: 101, pn_std: "PN-A", description: "内存 16G",
        brand: "三星", quantity: 5, unit: "个", purchase_original_unit_price: null,
        purchase_unit_price_ex_tax: null, purchase_line_value_ex_tax: null,
        anomaly_flags: [], pool_group_id: 12, pool_name: "内存互通池" }],
    });
    renderAt("/pool-analysis/12");
    fireEvent.click(await screen.findByRole("button", { name: "查看订单 CG-77 内容" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("张三")).toBeInTheDocument();
    expect(within(dialog).getByText("无供应商权限")).toBeInTheDocument();
    expect(within(dialog).getAllByText("无池价格权限").length).toBeGreaterThanOrEqual(3);
    expect(within(dialog).queryByText(/¥95|¥475/)).toBeNull();
  });
});

describe("治理权限", () => {
  it("哨兵不一致时 constraint=restricted 仍关闭该侧全部价格、成员与排序", async () => {
    const sentinelRestricted = {
      ...reference(2000, 100),
      restricted: false,
      constraint: { status: "restricted" as const, value: null },
    };
    fetchPoolAnalysis.mockResolvedValue({
      ...DETAIL,
      purchase_reference: sentinelRestricted,
      members: DETAIL.members.map((member) => ({
        ...member, purchase_reference: sentinelRestricted,
      })),
    });

    renderAt("/pool-analysis/12?side=purchase");
    await screen.findByText("内存互通池");
    await expandPriceRange();
    expect(screen.getByTestId("price-map-stub")).toHaveAttribute("data-restricted", "false");
    expect(screen.getAllByText("无池价格权限").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText(/¥90|¥100|¥105/)).toBeNull();
  });

  it("data_pool_price_governance=false：价格、约束、差额与排序统一隐藏", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({ data_pool_price_governance: false }));
    const restricted = {
      restricted: true, pool_stats: null, part_stats: null,
      constraint: { status: "restricted" as const, value: null },
      delta_to_pool_avg: null, delta_to_constraint: null, relation_to_constraint: null,
    };
    fetchPoolAnalysis.mockResolvedValue({ ...DETAIL,
      purchase_reference: restricted, sales_reference: restricted,
      members: DETAIL.members.map((member) => ({ ...member,
        purchase_reference: restricted, sales_reference: restricted })),
      purchase_orders: { ...DETAIL.purchase_orders, items: DETAIL.purchase_orders.items.map((item) => ({
        ...item, purchase_unit_price_ex_tax: null, purchase_line_value_ex_tax: null,
      })) },
      sales_orders: { ...DETAIL.sales_orders, items: DETAIL.sales_orders.items.map((item) => ({
        ...item, sale_unit_price_ex_tax: null, sale_line_value_ex_tax: null,
      })) },
    });
    renderAt("/pool-analysis/12");
    await screen.findByText("内存互通池");
    expect(screen.getAllByText("无池价格权限").length).toBeGreaterThanOrEqual(4);
    await expandPriceRange();
    expect(screen.getByTestId("price-map-stub")).toHaveAttribute("data-restricted", "true");
    for (const option of within(screen.getByLabelText("价格图区间排序")).getAllByRole("radio")) {
      expect(option).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "切换价格图排序方向" })).toBeDisabled();
    expect(screen.queryByText(/¥3,760/)).toBeNull();
    expect(screen.queryByText("未设置")).toBeNull();
  });

  it("data_purchase_cost=false 不隐藏池采购价；供应商/客户权限单独处理", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_pool_analysis: true,
      data_purchase_cost: false,
      data_pool_price_governance: true,
      data_supplier: false,
      data_customer: false,
    }));
    fetchPoolAnalysis.mockResolvedValue({
      ...DETAIL,
      purchase_orders: { ...DETAIL.purchase_orders, items: [{
        ...DETAIL.purchase_orders.items[0], supplier: null,
      }] },
      sales_orders: { ...DETAIL.sales_orders, restricted: false, items: [{
        ...DETAIL.sales_orders.items[0], customer: null,
      }] },
    });

    renderAt("/pool-analysis/12");
    await screen.findByText("内存互通池");
    expect(screen.getByText("张三")).toBeInTheDocument();
    expect(screen.getByText("李四")).toBeInTheDocument();
    expect(screen.getByText("无供应商权限")).toBeInTheDocument();
    expect(screen.getByText("无客户权限")).toBeInTheDocument();
    expect(screen.getAllByText(/¥95/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/¥475/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/¥210/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/¥420/).length).toBeGreaterThan(0);
    expect(screen.queryByText("当前账号无逐单销售明细查看权限（仅聚合可见）。")).toBeNull();
  });
});

describe("390px 移动详情", () => {
  it("不用桌面宽表；成员卡片可 Tab，并可用 Enter 打开全屏指标详情，订单按钮可达", async () => {
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    renderAt("/pool-analysis/12?side=purchase&pn=PN-B");

    const page = await screen.findByTestId("pool-analysis-page");
    expect(page).toHaveStyle({ maxWidth: "100%", overflowX: "hidden" });
    expect(page.querySelector(".ant-table")).toBeNull();
    expect(screen.getByLabelText("采购类型筛选")).toHaveStyle({ width: "100%", maxWidth: "100%" });

    const member = screen.getByRole("button", { name: "查看成员 PN-B 价格详情" });
    member.focus();
    expect(document.activeElement).toBe(member);
    fireEvent.keyDown(member, { key: "Enter" });

    const drawer = await screen.findByText("成员 PN-B 详情");
    const dialog = drawer.closest(".ant-drawer") as HTMLElement;
    expect(dialog).toBeTruthy();
    expect(dialog.querySelector(".ant-drawer-content-wrapper")).toHaveStyle({ height: "100%" });
    expect(within(dialog).getByText("采购均价").parentElement).toHaveTextContent("¥110");
    expect(within(dialog).getByText("采购量").parentElement).toHaveTextContent("10");
    expect(within(dialog).getByText("采购 vs 池均").parentElement).toHaveTextContent("+¥10");
    expect(within(dialog).getByText("采购池约束").parentElement).toHaveTextContent("¥105");

    const order = screen.getByRole("button", { name: "查看采购订单 CG-77 内容" });
    order.focus();
    expect(document.activeElement).toBe(order);
  });
});
