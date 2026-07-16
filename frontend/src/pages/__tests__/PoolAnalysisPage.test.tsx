/** 池分析详情页（/pool-analysis/:groupId 独立深链）行为测试。
 *
 * 断言口径：
 * - 深链打开即按 URL 窗口取数（刷新语义等价）；非法 groupId 走 404 空态不请求；
 * - 采购/销售横向柱状排名：平均/合计切换改变喂给图表的指标；
 * - 订单板块点单号 → 弹窗按单号精确召回订单内容；
 * - 治理受限：约束价显示「无权限」，与「未设置」分离。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const fetchPoolAnalysis = vi.fn();
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
  fetchPoolAnalysisOrderDetail: (...a: unknown[]) => fetchPoolAnalysisOrderDetail(...a),
}));
vi.mock("../../components/charts/HorizontalMetricBar", () => ({
  default: (p: { mode: string; metric: string; items: Array<{ pn: string; value: number | null }>;
    onPartClick?: (partId: number) => void }) => (
    <div data-testid={`metric-bar-${p.mode}`} data-metric={p.metric}
      data-clickable={String(!!p.onPartClick)}
      data-values={p.items.map((i) => `${i.pn}:${i.value}`).join("|")} />),
}));

import PoolAnalysisPage from "../PoolAnalysisPage";

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

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/pool-analysis/:groupId" element={<PoolAnalysisPage />} />
        <Route path="/boss" element={<div>看板桩</div>} />
        <Route path="/parts" element={<div>型号查询页桩</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  fetchPoolAnalysis.mockResolvedValue(DETAIL);
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
    expect(screen.getByText("成员销售排名（高→低）").parentElement).toHaveTextContent("当前关注");
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

describe("横向柱状排名：平均/合计切换", () => {
  it("默认平均单价；切合计后喂给图表的指标与数值同步变化", async () => {
    renderAt("/pool-analysis/12");
    const bar = await screen.findByTestId("metric-bar-purchase");
    expect(bar.dataset.metric).toBe("average");
    expect(bar.dataset.values).toContain("PN-B:110");     // 均价
    const seg = screen.getByLabelText("采购排名指标：平均单价或金额合计");
    fireEvent.click(within2(seg, "金额合计"));
    await waitFor(() => {
      const b = screen.getByTestId("metric-bar-purchase");
      expect(b.dataset.metric).toBe("total");
      expect(b.dataset.values).toContain("PN-B:1100");    // 合计
    });
  });

  it("无型号查询页权限时柱状图不提供失效的 PN 跳转", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({
      page_boss_board: true, page_parts: false,
    }));
    renderAt("/pool-analysis/12");
    expect(await screen.findByTestId("metric-bar-purchase")).toHaveAttribute("data-clickable", "false");
    expect(screen.getByTestId("metric-bar-sales")).toHaveAttribute("data-clickable", "false");
  });
});

// Segmented 内部按文本找可点击项
function within2(root: HTMLElement, text: string): HTMLElement {
  const el = Array.from(root.querySelectorAll<HTMLElement>("*"))
    .find((n) => n.textContent === text && n.children.length === 0);
  if (!el) throw new Error(`Segmented 选项未找到: ${text}`);
  return el;
}

describe("订单板块", () => {
  it("点采购单号 → 专用池分析端点按唯一 order_id 召回完整订单", async () => {
    renderAt("/pool-analysis/12?from=2026-06-01&to=2026-06-30");
    const link = await screen.findByRole("button", { name: "查看订单 CG-77 内容" });
    expect(link.tagName).toBe("BUTTON");
    fireEvent.click(link);
    await waitFor(() => expect(fetchPoolAnalysisOrderDetail).toHaveBeenCalledWith("purchase", 77));
    const dialog = await screen.findByRole("dialog");
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
    expect(screen.getByLabelText("采购排名指标：平均单价或金额合计")).toHaveClass("ant-segmented-disabled");
    expect(screen.getByLabelText("销售排名指标：平均单价或金额合计")).toHaveClass("ant-segmented-disabled");
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
