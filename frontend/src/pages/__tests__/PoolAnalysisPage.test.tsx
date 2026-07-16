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
      purchase_metrics: metrics(900, 90), sales_metrics: metrics(2160, 180) },
    { part_id: 102, pn_std: "PN-B", description: "内存 16G 兼容", brand: "金士顿",
      purchase_price: { wavg: 110, supply: null },
      sale_price: { wavg: 200, qty_sold: 8 },
      purchase_premium_pct: 0.22, sale_premium_pct: 0.11,
      brand_premium_purchase: true, brand_premium_sale: false,
      purchase_metrics: metrics(1100, 110), sales_metrics: metrics(1600, 200) },
  ],
  customer_cross_brand: { restricted: false, multi_brand_customers: 1,
    customers: [{ customer: "客户甲", brand_count: 2, concentration: 0.6 }] },
  purchase_metrics: metrics(2000, 100), sales_metrics: metrics(3760, 188),
  max_purchase_price: 105, min_sale_price: 160,
  purchase_violation_count: 1, sale_violation_count: null,
  manual_reference_restricted: false,
  purchase_orders: { restricted: false, total: 1, page: 1, page_size: 20, items: [
    { order_no: "CG-77", order_date: "2026-06-10", purchaser: "张三", supplier: "供应商A",
      source_type: "销售订单", line_id: 1, part_id: 101, pn_std: "PN-A",
      quantity: 5, purchase_unit_price_ex_tax: 95, purchase_line_value_ex_tax: 475 }] },
  sales_orders: { restricted: false, total: 1, page: 1, page_size: 20, items: [
    { order_no: "XS-88", order_date: "2026-06-12", salesperson: "李四", customer: "客户甲",
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
  it("点采购单号 → 弹窗按单号精确召回并展示订单内容", async () => {
    renderAt("/pool-analysis/12?from=2026-06-01&to=2026-06-30");
    const link = await screen.findByRole("button", { name: "查看订单 CG-77 内容" });
    expect(link.tagName).toBe("BUTTON");
    fireEvent.click(link);
    await waitFor(() => expect(dashboardPurchaseOrders).toHaveBeenCalledWith(
      expect.objectContaining({ order_no: "CG-77", status: "全部" })));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("采购订单 CG-77");
    // 弹窗内是完整行明细表（约束价列在场；页面 Descriptions 里同名标签是合法复现）
    expect((await within(dialog).findAllByText("人工最高采购价")).length).toBeGreaterThan(0);
    expect(within(dialog).getByLabelText("进入池「内存互通池」分析详情"))
      .toHaveAttribute("href", "/pool-analysis/12?from=2026-06-01&to=2026-06-30");
  });

  it("旧后端未声明 v2 契约时精确订单弹窗失败关闭", async () => {
    dashboardPurchaseOrders.mockResolvedValueOnce({ data: {
      total: 1, page: 1, page_size: 20, as_of: "2026-07-15",
      effective_sort: "order_date", ranking_restricted: false, items: [{
        order_id: 1, order_no: "别的单", part_count: 1,
      }],
    } });
    renderAt("/pool-analysis/12");
    fireEvent.click(await screen.findByRole("button", { name: "查看订单 CG-77 内容" }));
    expect(await screen.findByText(/服务升级中，精确订单详情暂不可用/)).toBeInTheDocument();
    expect(screen.queryByText("别的单")).toBeNull();
  });
});

describe("治理权限", () => {
  it("data_pool_price_governance=false：只隐藏人工约束，历史均价仍可见", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({ data_pool_price_governance: false }));
    fetchPoolAnalysis.mockResolvedValue({ ...DETAIL,
      max_purchase_price: null, min_sale_price: null,
      purchase_violation_count: null, sale_violation_count: null,
      manual_reference_restricted: true });
    renderAt("/pool-analysis/12");
    await screen.findByText("内存互通池");
    expect(screen.getAllByText("无约束价权限").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("窗口销售").closest("td")).toHaveTextContent("¥3,760");
    expect(screen.queryByText("未设置")).toBeNull();
  });

  it("字段受限不隐藏整行或经办人，并把无权限与无数据区分", async () => {
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
      purchase_metrics: null,
      members: DETAIL.members.map((member) => ({ ...member, purchase_metrics: null })),
      purchase_orders: { ...DETAIL.purchase_orders, items: [{
        ...DETAIL.purchase_orders.items[0], supplier: null,
        purchase_unit_price_ex_tax: null, purchase_line_value_ex_tax: null,
      }] },
      sales_orders: { ...DETAIL.sales_orders, restricted: false, items: [{
        ...DETAIL.sales_orders.items[0], customer: null,
      }] },
    });

    renderAt("/pool-analysis/12");
    await screen.findByText("内存互通池");
    expect(screen.getByText("张三")).toBeInTheDocument();
    expect(screen.getByText("李四")).toBeInTheDocument();
    expect(screen.getAllByText("无价格权限").length).toBeGreaterThan(0);
    expect(screen.getByText("无供应商权限")).toBeInTheDocument();
    expect(screen.getByText("无客户权限")).toBeInTheDocument();
    expect(screen.getAllByText(/¥210/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/¥420/).length).toBeGreaterThan(0);
    expect(screen.queryByText("当前账号无逐单销售明细查看权限（仅聚合可见）。")).toBeNull();
  });
});
