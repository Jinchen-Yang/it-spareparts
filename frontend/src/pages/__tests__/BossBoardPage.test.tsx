/** 老板经营看板 v2 行为测试。
 *
 * 断言口径：
 * - URL 深链：/boss?range=7d&part_id=&pool=&buyer=… 打开即按筛选取数（刷新语义等价）；
 * - 订单表 PN 首屏直出（pn_preview 链接），点单号展开完整行明细（池均价/约束价/差额/状态）；
 * - 筛选变化 push 进历史，浏览器后退可恢复；
 * - 权限三态：无成本权限 ≠ 暂无数据；受限销售显示"无明细权限"；
 * - 池列表表头 合计↔均价 循环切换：aria-label 完整、排序字段同步切换。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import type { Location, NavigateFunction } from "react-router-dom";
import dayjs from "dayjs";

const dashboardKpi = vi.fn();
const dashboardTrend = vi.fn();
const dashboardPartRanking = vi.fn();
const dashboardSales = vi.fn();
const dashboardPurchaseOrders = vi.fn();
const dashboardPools = vi.fn();
const dashboardPool = vi.fn();

vi.mock("../../api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
  api: { get: vi.fn(), post: vi.fn() },
  dashboardKpi: (...a: unknown[]) => dashboardKpi(...a),
  dashboardTrend: (...a: unknown[]) => dashboardTrend(...a),
  dashboardPartRanking: (...a: unknown[]) => dashboardPartRanking(...a),
  dashboardSales: (...a: unknown[]) => dashboardSales(...a),
  dashboardPurchaseOrders: (...a: unknown[]) => dashboardPurchaseOrders(...a),
  dashboardPools: (...a: unknown[]) => dashboardPools(...a),
  dashboardPool: (...a: unknown[]) => dashboardPool(...a),
}));
// 图表在 jsdom 无 canvas：打桩捕获 props
vi.mock("../../components/charts/BusinessTrendChart", () => ({
  default: (p: { granularity: string; data: unknown[] }) => (
    <div data-testid="trend-chart" data-granularity={p.granularity} data-points={p.data.length} />),
}));
vi.mock("../../components/PartPicker", () => ({
  default: (p: { value?: number | null }) => (
    <input aria-label="PN 型号筛选" readOnly value={p.value ?? ""} />),
}));

import BossBoardPage from "../BossBoardPage";

const D = "YYYY-MM-DD";
const KPI = {
  window: { date_from: null, date_to: null, as_of: "2026-07-15", future_excluded: true },
  sales_ex_tax: 1000, purchase_ex_tax: 800, sales_costed_ex_tax: 900, gross_profit: 100,
  gross_margin: 0.1111, cost_coverage: 0.9, sales_uncosted_ex_tax: 100, excluded_revenue: 0,
  orders_active: 5, orders_in_progress: 1, orders_cancelled: 0, orders_future: 0, anomaly_lines: 0,
};
const REF_OK = { reference_status: "within_limit", pool_avg_delta: -5, pool_avg_delta_pct: -0.05,
  manual_limit_delta: -10, manual_limit_delta_pct: -0.02 };

const purchaseRow = (over: Record<string, unknown> = {}) => ({
  order_id: 11, order_no: "CG-001", order_date: "2026-07-10", occurred_date: "2026-07-10",
  is_future: false, purchaser: "张三", source_type: "销售订单", data_status: "已生效",
  linked_sales_order: "XS-9", part_count: 3, pn_count: 3, total_qty: 6, total_quantity: 6,
  total_ex_tax: 600, total_amount: 600,
  pn_preview: ["PN-A", "PN-B"],
  parts: [
    { line_id: 1, part_id: 101, pn_std: "PN-A", description: "内存条", brand: "三星",
      quantity: 2, unit_price_ex_tax: 100, amount: 200, in_stats_scope: true,
      pool_group_id: 7, pool_name: "内存池", pool_avg_purchase_price: 105,
      max_purchase_price: 110, ...REF_OK },
    { line_id: 2, part_id: 102, pn_std: "PN-B", description: "硬盘", brand: "希捷",
      quantity: 2, unit_price_ex_tax: 120, amount: 240, in_stats_scope: true,
      pool_group_id: null, pool_name: null, pool_avg_purchase_price: null,
      max_purchase_price: null, reference_status: "no_pool",
      pool_avg_delta: null, pool_avg_delta_pct: null, manual_limit_delta: null, manual_limit_delta_pct: null },
    { line_id: 3, part_id: 103, pn_std: "PN-C", description: "CPU", brand: "英特尔",
      quantity: 2, unit_price_ex_tax: 80, amount: 160, in_stats_scope: true,
      pool_group_id: 7, pool_name: "内存池", pool_avg_purchase_price: 70,
      max_purchase_price: 75, reference_status: "above_manual_max",
      pool_avg_delta: 10, pool_avg_delta_pct: 0.14, manual_limit_delta: 5, manual_limit_delta_pct: 0.07 },
  ],
  ...over,
});

const ordersResp = (items: unknown[], over: Record<string, unknown> = {}) => ({
  total: items.length, page: 1, page_size: 10, as_of: "2026-07-15",
  contract_version: 2,
  effective_sort: "order_date", ranking_restricted: false,
  profit_restricted: false, cost_restricted: false,
  parts_restricted: false, manual_reference_restricted: false, items, ...over,
});

const poolItem = (over: Record<string, unknown> = {}) => ({
  group_id: 7, name: "内存池", description: null, member_count: 5,
  needs_calibration: false, oversized: false,
  demand_qty: 10, demand_revenue_ex_tax: 1000, theoretical_saving: 200, supply_available_upper: 100,
  purchase_metrics: { total_amount: 5000, total_quantity: 50, weighted_avg_unit_price: 100,
    order_count: 8, latest_date: "2026-07-01" },
  sales_metrics: { total_amount: 8000, total_quantity: 40, weighted_avg_unit_price: 200,
    order_count: 6, latest_date: "2026-07-08" },
  max_purchase_price: 110, min_sale_price: 150,
  purchase_violation_count: 2, sale_violation_count: null,
  ...over,
});

const poolsResp = (items: unknown[], over: Record<string, unknown> = {}) => ({
  total: items.length, page: 1, page_size: 10,
  window: { date_from: null, date_to: null, as_of: "2026-07-15" },
  sort: "savings", effective_sort: "savings",
  ranking_restricted: false, ranking_capped: false, items, ...over,
});

const rankingResp = {
  window: { date_from: null, date_to: null, as_of: "2026-07-15", cost_method: "moving_avg" },
  filters: { part_id: null, pn: null, pool_group_id: null },
  profitable: [{ part_id: 101, pn_std: "PN-A", description: "内存条", brand: "三星",
    qty_sold: 5, revenue: 500, order_count: 3, revenue_costed: 500, no_cost: 0, lines: 5,
    gross_profit_moving: 100, gross_profit_fifo: 90, gross_margin_moving: 0.2, gross_margin_fifo: 0.18,
    purchase_price: { wavg: 80, median: 80, min: 70, max: 90, samples: 4, last_date: "2026-07-01" },
    sale_price: null, cost_coverage: 1, pool_group_id: 7, pool_name: "内存池" }],
  loss: [], profit_restricted: false,
  counts: { total_parts: 3, with_cost: 2, profitable: 1, loss: 0, no_cost_parts: 1 },
  ranking: { total: 3, page: 1, page_size: 50, sort: "gross_profit", effective_sort: "gross_profit",
    order: "desc", ranking_restricted: false, items: [] },
};

let curLoc!: Location;
let nav!: NavigateFunction;
function Probe() { curLoc = useLocation(); nav = useNavigate(); return null; }

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/boss" element={<><BossBoardPage /><Probe /></>} />
        <Route path="/pool-analysis/:groupId" element={<div>池分析详情页桩</div>} />
        <Route path="/parts" element={<div>型号查询页桩</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  dashboardKpi.mockResolvedValue({ data: KPI });
  dashboardTrend.mockResolvedValue({ data: { granularity: "day", series: [
    { period: "2026-07-10", sales_ex_tax: 100, purchase_ex_tax: 80, gross_profit: 20 }] } });
  dashboardPartRanking.mockResolvedValue({ data: rankingResp });
  dashboardSales.mockResolvedValue({ data: ordersResp([]) });
  dashboardPurchaseOrders.mockResolvedValue({ data: ordersResp([purchaseRow()]) });
  dashboardPools.mockResolvedValue({ data: poolsResp([poolItem()]) });
});
afterEach(cleanup);

describe("URL 深链与筛选下发", () => {
  it("/boss?range=7d&part_id=5&pool=3&buyer=张&sp=李 打开即按筛选取数", async () => {
    renderAt("/boss?range=7d&part_id=5&pn=PN-X&pool=3&buyer=张&sp=李");
    const from = dayjs().subtract(6, "day").format(D);
    const to = dayjs().format(D);
    await waitFor(() => expect(dashboardPurchaseOrders).toHaveBeenCalledWith(
      expect.objectContaining({ date_from: from, date_to: to, part_id: 5, pool_group_id: 3, purchaser: "张" })));
    await waitFor(() => expect(dashboardSales).toHaveBeenCalledWith(
      expect.objectContaining({ part_id: 5, pool_group_id: 3, salesperson: "李" })));
    await waitFor(() => expect(dashboardPartRanking).toHaveBeenCalledWith(
      expect.objectContaining({ part_id: 5, pool_group_id: 3 })));
    // KPI/趋势仅时间口径，不带 PN/池
    await waitFor(() => expect(dashboardKpi).toHaveBeenCalledWith({ date_from: from, date_to: to }));
  });

  it("切时间范围 push 进历史；后退恢复旧筛选", async () => {
    renderAt("/boss");
    await screen.findAllByText("最近采购");
    fireEvent.click(screen.getByText("今天"));
    await waitFor(() => expect(curLoc.search).toContain("range=today"));
    const today = dayjs().format(D);
    await waitFor(() => expect(dashboardPurchaseOrders).toHaveBeenCalledWith(
      expect.objectContaining({ date_from: today, date_to: today })));

    act(() => nav(-1));   // 后退：回到默认近30天
    await waitFor(() => expect(curLoc.search).not.toContain("range=today"));
    const from30 = dayjs().subtract(29, "day").format(D);
    await waitFor(() => expect(dashboardPurchaseOrders).toHaveBeenLastCalledWith(
      expect.objectContaining({ date_from: from30 })));
  });

  it("仅选择时间范围也显示清除按钮，并可恢复默认近30天", async () => {
    renderAt("/boss");
    fireEvent.click(await screen.findByText("今天"));
    const clear = await screen.findByRole("button", { name: "清除全部筛选" });
    fireEvent.click(clear);
    await waitFor(() => expect(curLoc.search).not.toContain("range=today"));
    const from30 = dayjs().subtract(29, "day").format(D);
    await waitFor(() => expect(dashboardPurchaseOrders).toHaveBeenLastCalledWith(
      expect.objectContaining({ date_from: from30, date_to: dayjs().format(D) })));
  });

  it("URL 自定义时间 from>to 不下发反向窗口，退回默认30天且仍可清除", async () => {
    renderAt("/boss?range=custom&from=2026-07-20&to=2026-07-01");
    const from30 = dayjs().subtract(29, "day").format(D);
    await waitFor(() => expect(dashboardKpi).toHaveBeenCalledWith({
      date_from: from30, date_to: dayjs().format(D),
    }));
    expect(screen.getByRole("button", { name: "清除全部筛选" })).toBeInTheDocument();
  });

  it("URL 不可能日期不下发，避免后端 422", async () => {
    renderAt("/boss?range=custom&from=2026-02-31&to=2026-03-31");
    const from30 = dayjs().subtract(29, "day").format(D);
    await waitFor(() => expect(dashboardKpi).toHaveBeenCalledWith({
      date_from: from30, date_to: dayjs().format(D),
    }));
  });

  it.each([
    "/boss?od_from=2026-07-20&od_to=2026-07-01",
    "/boss?od_from=2026-07-01",
  ])("非法或半开的趋势下钻窗口整体失效，不下发给订单接口：%s", async (url) => {
    renderAt(url);
    const from30 = dayjs().subtract(29, "day").format(D);
    const today = dayjs().format(D);
    await waitFor(() => expect(dashboardPurchaseOrders).toHaveBeenCalledWith(
      expect.objectContaining({ date_from: from30, date_to: today })));
    expect(dashboardPurchaseOrders).not.toHaveBeenCalledWith(
      expect.objectContaining({ date_from: "2026-07-20", date_to: "2026-07-01" }));
    expect(screen.queryByText(/订单块已按选中期筛选/)).toBeNull();
    expect(screen.getByRole("button", { name: "清除全部筛选" })).toBeInTheDocument();
  });
});

describe("最近订单：PN 首屏直出 + 展开明细", () => {
  it("订单行直接显示 pn_preview 链接与 +N 更多；不用先展开", async () => {
    renderAt("/boss");
    // PN-A 同时出现在盈亏榜（合法复现），断言至少一个带稳定深链
    const pnLinks = await screen.findAllByRole("link", { name: "查看型号 PN-A 全景" });
    expect(pnLinks.some((l) => l.getAttribute("href") === "/parts?part_id=101")).toBe(true);
    expect(screen.getByText("+1 更多").closest("button")).not.toBeNull();
  });

  it("短暂连接旧版后端缺少 parts/pn_preview/pn_count 时受控降级，不崩溃", async () => {
    dashboardPurchaseOrders.mockResolvedValue({ data: ordersResp([
      purchaseRow({ parts: undefined, pn_preview: undefined, pn_count: undefined }),
    ], { contract_version: undefined }) });
    renderAt("/boss");
    expect(await screen.findByText("旧版接口：暂无PN明细")).toBeInTheDocument();
    const orderButton = screen.getByRole("button", { name: /订单 CG-001，展开明细/ });
    expect(orderButton.tagName).toBe("BUTTON");
    fireEvent.click(orderButton);
    expect(await screen.findByText(/当前服务版本尚未返回 PN 明细/)).toBeInTheDocument();
  });

  it("旧契约忽略 v2 筛选时失败关闭，不展示可能错误的订单", async () => {
    dashboardPurchaseOrders.mockResolvedValue({ data: ordersResp([
      purchaseRow({ parts: undefined, pn_preview: undefined, pn_count: undefined }),
    ], { contract_version: undefined }) });
    renderAt("/boss?part_id=5&pn=PN-X");
    expect(await screen.findByText(/服务升级中，当前 PN、互通池或采购员筛选暂不可用/))
      .toBeInTheDocument();
    expect(screen.queryByText("CG-001")).toBeNull();
  });

  it("点单号展开完整明细：池归属/池均价/约束价/差额/分析状态齐全", async () => {
    renderAt("/boss");
    const orderLink = await screen.findByRole("button", { name: /订单 CG-001，展开明细/ });
    fireEvent.click(orderLink);
    await screen.findAllByText("人工最高采购价");
    expect(screen.getAllByText("内存池").length).toBeGreaterThan(0);          // 池链接
    expect(screen.getAllByText("超采购上限").length).toBeGreaterThan(0);       // 越线行状态
    expect(screen.getAllByText("未入池").length).toBeGreaterThan(0);           // 无池行
    // 订单级分析状态概要：1 行越线
    expect(screen.getAllByText("越线 1 行").length).toBeGreaterThan(0);
  });
});

describe("权限三态（无权限 ≠ 暂无数据）", () => {
  it("orders_restricted=true：明确显示无逐单销售权限，不伪装成共0单", async () => {
    dashboardSales.mockResolvedValue({ data: ordersResp([], { orders_restricted: true, total: null }) });
    renderAt("/boss");
    expect(await screen.findByText("当前账号无逐单销售订单权限（仅聚合数据可见）。"))
      .toBeInTheDocument();
    expect(screen.queryByText("共 0 单")).toBeNull();
  });

  it("page_boss_board=true 但 page_parts=false：PN 只读展示，不生成不可访问深链", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({ page_boss_board: true, page_parts: false }));
    renderAt("/boss");
    await screen.findAllByText("PN-A");
    expect(screen.queryByRole("link", { name: "查看型号 PN-A 全景" })).toBeNull();
  });

  it("非管理员权限快照缺少 page_parts 时也不生成死链", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({ page_boss_board: true }));
    renderAt("/boss");
    await screen.findAllByText("PN-A");
    expect(screen.queryByRole("link", { name: "查看型号 PN-A 全景" })).toBeNull();
  });

  it("data_purchase_cost=false：采购金额列显示「无成本权限」而非空", async () => {
    localStorage.setItem("role", "boss");
    localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: false, data_profit: true }));
    dashboardPurchaseOrders.mockResolvedValue({ data: ordersResp(
      [purchaseRow({ total_amount: null, total_ex_tax: null })], { cost_restricted: true }) });
    renderAt("/boss");
    await screen.findAllByText("无成本权限");
    // 警告状态仍可见（状态非金额）：订单级概要照常显示
    expect(await screen.findByText("越线 1 行")).toBeInTheDocument();
  });

  it("受限销售 parts_restricted：PN 列与人员列显示「无明细权限/无权限」", async () => {
    dashboardSales.mockResolvedValue({ data: ordersResp(
      [{ order_id: 21, order_no: "XS-1", order_date: "2026-07-09", occurred_date: "2026-07-09",
         is_future: false, salesperson: null, customer: null, business_type: null,
         data_status: "已生效", part_count: 2, pn_count: 2, total_qty: 3, total_quantity: 3,
         total_revenue: 300, total_amount: 300, total_gross_profit: null, linked_purchase: false,
         parts: [], pn_preview: [] }],
      { parts_restricted: true }) });
    renderAt("/boss");
    await screen.findAllByText("无明细权限");
    expect(screen.getAllByText("无权限").length).toBeGreaterThan(0);
  });
});

describe("互通池列表：表头 合计↔均价 循环切换", () => {
  it("切换按钮 aria-label 完整；点击后文案与排序字段同步", async () => {
    renderAt("/boss");
    // 先按采购指标列排序（当前=金额合计）
    const toggle = await screen.findByRole("button",
      { name: "采购指标：当前显示金额合计(未税)，点击切换为平均单价" });
    const th = toggle.closest("th")!;
    fireEvent.click(th);
    await waitFor(() => expect(dashboardPools).toHaveBeenCalledWith(
      expect.objectContaining({ sort: "purchase_total" })));
    // 表头循环切换 → 显示口径与排序字段一起变
    fireEvent.click(toggle);
    await screen.findByRole("button",
      { name: "采购指标：当前显示平均单价(未税)，点击切换为金额合计" });
    await waitFor(() => expect(dashboardPools).toHaveBeenCalledWith(
      expect.objectContaining({ sort: "purchase_average" })));
  });

  it("越线计数 null 双语义：无约束显示「未设约束」，非 0 数字红标", async () => {
    renderAt("/boss");
    const table = (await screen.findAllByRole("table")).find((t) => within(t).queryByText("内存池"))!;
    expect(within(table).getByText("未设约束")).toBeInTheDocument();   // 销售侧未设约束
    expect(within(table).getByText("2")).toBeInTheDocument();          // 采购侧 2 行越线
  });

  it("点击池名进入 /pool-analysis/:groupId 深链", async () => {
    renderAt("/boss?range=custom&from=2026-06-01&to=2026-06-30");
    const table = (await screen.findAllByRole("table")).find((t) => within(t).queryByText("内存池"))!;
    const link = within(table).getByLabelText("进入池「内存池」分析详情");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "/pool-analysis/7?from=2026-06-01&to=2026-06-30");
    fireEvent.click(link);
    await screen.findByText("池分析详情页桩");
  });

  it("订单明细和盈亏榜的池链接都保留当前 from/to", async () => {
    renderAt("/boss?range=custom&from=2026-06-01&to=2026-06-30");
    fireEvent.click(await screen.findByRole("button", { name: /订单 CG-001，展开明细/ }));
    const links = await screen.findAllByLabelText("进入池「内存池」分析详情");
    expect(links.length).toBeGreaterThanOrEqual(3); // 池列表、盈亏榜、订单明细
    for (const link of links) {
      expect(link.tagName).toBe("A");
      expect(link).toHaveAttribute("href", "/pool-analysis/7?from=2026-06-01&to=2026-06-30");
    }
  });
});

describe("趋势与盈亏榜", () => {
  it("趋势图接收粒度与数据点；切粒度 replace 不堆历史", async () => {
    renderAt("/boss");
    const chart = await screen.findByTestId("trend-chart");
    expect(chart.dataset.granularity).toBe("day");
    fireEvent.click(screen.getByText("月"));
    await waitFor(() => expect(curLoc.search).toContain("gran=month"));
    await waitFor(() => expect(dashboardTrend).toHaveBeenCalledWith(
      expect.objectContaining({ granularity: "month" })));
  });

  it("盈亏榜显示成本覆盖率与未配成本营收提示；无成本型号不入榜说明在场", async () => {
    renderAt("/boss");
    await screen.findByText(/无成本 1（毛利未知，不入正式排名）/);
    // KPI 卡与盈亏榜说明各出现一次（双提示，合法复现）
    expect((await screen.findAllByText(/未配成本营收/)).length).toBeGreaterThanOrEqual(2);
  });
});
