import { authzSnapshot, authzTestState } from "../../../../test/authzFixtures";
// 销售看板页签（D-44）：页签落 URL、分组排行升降序与下钻、无客户权限的维度门控、
// 趋势粒度推荐与桶下钻、KPI 上月对比与新老客、订单导出、金额区间口径门控。
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchSalesAnalyticsOptions = vi.fn();
const fetchSalesAnalyticsSummary = vi.fn();
const fetchSalesAnalyticsOrders = vi.fn();
const fetchSalesAnalyticsOrderDetail = vi.fn();
const fetchSalesAnalyticsGroups = vi.fn();
const fetchSalesAnalyticsTrend = vi.fn();
const downloadSalesOrdersCsv = vi.fn();

vi.mock("../../../../api/salesAnalytics", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../../api/salesAnalytics");
  return {
    ...actual,
    fetchSalesAnalyticsOptions: (...a: unknown[]) => fetchSalesAnalyticsOptions(...a),
    fetchSalesAnalyticsSummary: (...a: unknown[]) => fetchSalesAnalyticsSummary(...a),
    fetchSalesAnalyticsOrders: (...a: unknown[]) => fetchSalesAnalyticsOrders(...a),
    fetchSalesAnalyticsOrderDetail: (...a: unknown[]) => fetchSalesAnalyticsOrderDetail(...a),
    fetchSalesAnalyticsGroups: (...a: unknown[]) => fetchSalesAnalyticsGroups(...a),
    fetchSalesAnalyticsTrend: (...a: unknown[]) => fetchSalesAnalyticsTrend(...a),
    downloadSalesOrdersCsv: (...a: unknown[]) => downloadSalesOrdersCsv(...a),
  };
});
// jsdom 无 canvas：mock 图表壳，只保留空态文案；带 onEvents.click 的图渲染一个
// 「模拟点击」按钮（点击第 0 个类目），让桶/条下钻可以在页面级测试里触发——
// 排行横条按 name（yAxis 类目首项）反查，趋势柱图按 dataIndex（xAxis 首桶）。
vi.mock("../../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText, ariaLabel, onEvents, option }: {
    empty?: boolean; emptyText?: string; ariaLabel?: string;
    onEvents?: { click?: (p: unknown) => void };
    option?: { xAxis?: { data?: unknown[] }; yAxis?: { data?: unknown[] } };
  }) =>
    empty ? <div>{emptyText ?? "空"}</div> : (
      <div data-testid="chart" aria-label={ariaLabel}>
        {onEvents?.click && (
          <button
            type="button"
            aria-label={`模拟点击${ariaLabel ?? ""}`}
            onClick={() => onEvents.click?.({
              dataIndex: 0,
              name: option?.yAxis?.data?.[0] ?? option?.xAxis?.data?.[0],
            })}
          >
            模拟点击
          </button>
        )}
      </div>
    ),
}));

vi.mock("../../../../authz/AuthzProvider", async () => ({
  ...await vi.importActual<typeof import("../../../../authz/AuthzProvider")>("../../../../authz/AuthzProvider"),
  useAuthz: () => authzTestState(authzSnapshot({ enabled: false, read: false, export: false, scope: "none", customer_count: 0, reason: "legacy", field_permissions: [] }, {version: "legacy", canManage: false})),
  AuthzUnavailable: () => <div>权限尚未确认</div>,
}));

import SalesAnalyticsPage from "../SalesAnalyticsPage";

const CAPTURED_REQUEST = expect.objectContaining({ token: expect.any(String), snapshotVersion: expect.any(String), generation: expect.any(Number), queryKey: expect.any(String), signal: expect.any(AbortSignal) });
const TODAY = "2026-10-17";

const KPI = {
  orders: 5, amount_ex_tax_total: 1000, amount_inc_tax_total: 1130, amount_inc_tax_missing: 0,
  revenue_total: 800, customers: 3, salespeople: 2, revenue_costed: 700, gross_profit: 210,
  gross_margin: 0.3, cost_coverage: 0.875, new_customers: 1, returning_customers: 2,
};
const SUMMARY = {
  window: { date_from: TODAY, date_to: TODAY },
  filters_echo: { business_type: [], customer: null, salesperson: null },
  as_of: TODAY, kpi: KPI, line_mode: false,
  compare: { date_from: "2026-09-17", date_to: "2026-09-17", kpi: { ...KPI, orders: 4, amount_ex_tax_total: 800 } },
  compare_available: true,
};

const GROUPS = {
  group_by: "customer", line_level: false, sort: "amount_ex_tax", order: "desc",
  amount_basis: "amount_ex_tax", top: 20, total_groups: 3,
  groups: [
    { key: "A公司", is_empty: false, orders: 3, customers: 1, amount_ex_tax: 700, amount_inc_tax: 791,
      revenue: 600, gross_profit: 180, gross_margin: 0.3, avg_order: 233.33, share: 0.7, cumulative_share: 0.7,
      compare: { orders: 2, customers: 1, amount_ex_tax: 500, amount_inc_tax: 565, revenue: 400,
        gross_profit: 100, gross_margin: 0.25, avg_order: 250 } },
    { key: "未填", is_empty: true, orders: 2, customers: 0, amount_ex_tax: 300, amount_inc_tax: 339,
      revenue: 200, gross_profit: 30, gross_margin: 0.15, avg_order: 150, share: 0.3, cumulative_share: 1 },
  ],
  other: null,
  concentration: { top1_share: 0.7, top5_share: 1, top10_share: 1 },
  compare_window: { date_from: "2026-09-17", date_to: "2026-09-17" },
  compare_available: true,
};

const TREND = {
  granularity: "day", date_from: TODAY, date_to: TODAY,
  buckets: [{ bucket: TODAY, orders: 5, customers: 3, amount_ex_tax: 1000, amount_inc_tax: 1130,
    revenue: 800, gross_profit: 210, gross_margin: 0.3, avg_order: 200 }],
  compare: null, compare_window: null, compare_available: true,
  weekday: [1, 2, 3, 4, 5, 6, 7].map((d) => ({ isodow: d, orders: d === 6 ? 5 : 0,
    amount_ex_tax: d === 6 ? 1000 : 0, amount_inc_tax: 0, revenue: 0 })),
};

let locationSearch = "";
function LocationProbe() {
  locationSearch = useLocation().search;
  return null;
}
function renderPage(initial = "/sales/analytics") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LocationProbe />
      <SalesAnalyticsPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "sales-fixture-owner");
  localStorage.setItem("role", "boss");
  localStorage.setItem("permissions", JSON.stringify({
    data_purchase_cost: true, data_reconciliation: true, data_customer: true, data_profit: true,
  }));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-10-17T02:00:00Z"));
  fetchSalesAnalyticsOptions.mockResolvedValue({ data: {
    business_types: [{ value: "备件销售", orders: 9 }],
    amount_range: { amount_ex_tax_max: 8000, amount_inc_tax_max: 9040, revenue_max: 7000 },
  } });
  fetchSalesAnalyticsSummary.mockResolvedValue({ data: SUMMARY });
  fetchSalesAnalyticsOrders.mockResolvedValue({ data: {
    total: 0, page: 1, page_size: 50, as_of: TODAY, sort: "amount_ex_tax", order: "desc",
    include_returns: false, items: [],
  } });
  fetchSalesAnalyticsGroups.mockImplementation((q: Record<string, unknown>) =>
    Promise.resolve({ data: { ...GROUPS, group_by: q.group_by, sort: q.sort ?? "amount_ex_tax",
      order: q.order ?? "desc" } }));
  fetchSalesAnalyticsTrend.mockResolvedValue({ data: TREND });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("销售看板 KPI 条（D-44）", () => {
  it("毛利 / 成交客户 / 新老客 / 上月对比展示；关闭对比只隐藏、不重新请求", async () => {
    renderPage();
    expect(await screen.findByText(/毛利率 30\.0%/)).toBeInTheDocument();
    expect(screen.getByText("新客 1 · 老客 2")).toBeInTheDocument();
    // 单数 5 vs 上月 4 → +25.0%
    expect(screen.getAllByText(/↑\+25\.0%/).length).toBeGreaterThan(0);
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("switch", { name: "对比上月" }));
    await waitFor(() => expect(screen.queryByText(/↑\+25\.0%/)).not.toBeInTheDocument());
    expect(locationSearch).toContain("compare=0");
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1);
  });

  it("无利润权限：不渲染毛利卡", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true, data_reconciliation: true, data_customer: true, data_profit: false,
    }));
    renderPage();
    expect(await screen.findByText("新客 1 · 老客 2")).toBeInTheDocument();
    expect(screen.queryByText(/毛利率/)).not.toBeInTheDocument();
  });
});

describe("分组排行页签", () => {
  it("切页签落 URL；按维度请求；列头点击切升序", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: "分组排行" }));
    await waitFor(() => expect(locationSearch).toContain("tab=groups"));
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalled());
    expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      group_by: "customer", sort: "amount_ex_tax", order: "desc", top: 20, compare: true,
      date_from: TODAY, date_to: TODAY,
    });
    expect(await screen.findByText(/集中度：第 1 名占 70\.0%/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("columnheader", { name: "单数" }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      sort: "orders", order: "desc",
    }));
    fireEvent.click(screen.getByRole("columnheader", { name: "单数" }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      sort: "orders", order: "asc",
    }));
    expect(locationSearch).toContain("gorder=asc");
  });

  it("单头维度没有「明细行」列——不会发出后端 422 的 sort=lines（契约 §6）", async () => {
    renderPage("/sales/analytics?tab=groups");
    await screen.findByRole("columnheader", { name: /客户/ });
    expect(screen.queryByRole("columnheader", { name: "明细行" })).toBeNull();
  });

  it("行级维度新排序键（契约 §6 返工）：明细行 / 单均 / 较上月同期列双向排序", async () => {
    // 行级维度（商品大类）：line_level=true，明细行列随之出现
    fetchSalesAnalyticsGroups.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: { ...GROUPS, group_by: q.group_by, line_level: true,
        sort: q.sort ?? "amount_ex_tax", order: q.order ?? "desc" } }));
    renderPage("/sales/analytics?tab=groups&group_by=category");
    expect(await screen.findByRole("columnheader", { name: "明细行" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("columnheader", { name: "明细行" }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      group_by: "category", sort: "lines", order: "desc",
    }));
    expect(locationSearch).toContain("gsort=lines");
    // 单均：与展示同口径（amount_basis），不受成本遮蔽影响入口
    fireEvent.click(screen.getByRole("columnheader", { name: "单均" }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      sort: "avg_order", order: "desc",
    }));
    // 较上月同期（compare 列可见时）：真实同期变化率排序，与金额排序不等价
    fireEvent.click(screen.getByRole("columnheader", { name: /较上月同期/ }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      sort: "basis_change", order: "desc",
    }));
    expect(locationSearch).toContain("gsort=basis_change");
  });

  it("lines 排序残留归一（契约 §6）：明细行排序后切单头维度 → 请求按单数，不发非法 sort=lines", async () => {
    fetchSalesAnalyticsGroups.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: { ...GROUPS, group_by: q.group_by, line_level: true,
        sort: q.sort ?? "amount_ex_tax", order: q.order ?? "desc" } }));
    renderPage("/sales/analytics?tab=groups&group_by=category");
    expect(await screen.findByRole("columnheader", { name: "明细行" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("columnheader", { name: "明细行" }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      group_by: "category", sort: "lines", order: "desc",
    }));

    // 切到单头维度「客户」：URL 的 gsort=lines 保留用户意图，但请求前归一为
    // 单数（单头 + sort=lines 是组合校验 422，契约 §6）
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "分组维度" }));
    const option = (await screen.findAllByText("客户"))
      .map((node) => node.closest(".ant-select-item-option"))
      .find((node) => node && !node.closest(".ant-select-dropdown-hidden"));
    fireEvent.click(option!);
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({
      group_by: "customer", sort: "orders",
    }));
    // 全程没有发过 customer+lines 的非法组合
    const customerCalls = fetchSalesAnalyticsGroups.mock.calls
      .map(([q]) => q as Record<string, unknown>)
      .filter((q) => q.group_by === "customer");
    expect(customerCalls.length).toBeGreaterThan(0);
    expect(customerCalls.every((q) => q.sort !== "lines")).toBe(true);
    // URL 仍保留 gsort=lines（意图不丢），回显与 URL 不一致 → 归一提示出现
    expect(locationSearch).toContain("gsort=lines");
    expect(await screen.findByText(/已按单数排序（以响应回显为准）/)).toBeInTheDocument();
  });

  it("点客户名下钻（A-AC13 收窄）：多选 A/B 点 A 只留 A；未填组可点且走 __empty__ 哨兵（契约 §0）", async () => {
    // 已选 A公司 + B公司：点 A公司 → 收窄为仅 A公司（不是追加）
    renderPage("/sales/analytics?tab=groups&customer_name=A%E5%85%AC%E5%8F%B8,B%E5%85%AC%E5%8F%B8");
    const link = await screen.findByRole("button", { name: "A公司" });
    fireEvent.click(link);
    await waitFor(() => expect(locationSearch).toContain("customer_name=A%E5%85%AC%E5%8F%B8"));
    expect(decodeURIComponent(locationSearch)).not.toContain("B公司");
    expect(locationSearch).not.toContain("tab=");
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      customer_name: ["A公司"],
    }));
  });

  it("未填组下钻（A-AC06）：空值组转哨兵可筛，不与真实客户名碰撞", async () => {
    renderPage("/sales/analytics?tab=groups");
    // GROUPS 夹具含 key="未填"/is_empty 组：按契约渲染为可下钻按钮「（未填）」
    const emptyLink = await screen.findByRole("button", { name: "（未填）" });
    fireEvent.click(emptyLink);
    await waitFor(() => expect(locationSearch).toContain("customer_name=__empty__"));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      customer_name: ["__empty__"],
    }));
  });

  it("趋势桶下钻（A-AC12）：月桶与原区间取交集——只收窄不扩大，两侧越界都夹回", async () => {
    fetchSalesAnalyticsTrend.mockResolvedValue({ data: {
      ...TREND, granularity: "month", date_from: "2026-10-01", date_to: "2026-10-31",
      buckets: [{ bucket: "2026-10", orders: 5, customers: 3, amount_ex_tax: 1000,
        amount_inc_tax: 1130, revenue: 800, gross_profit: 210, gross_margin: 0.3, avg_order: 200 }],
    } });
    // 原筛选 10-10 ~ 10-20（月桶 10-01 ~ 10-31 比它宽）：交集应仍是 10-10 ~ 10-20
    renderPage("/sales/analytics?tab=trend&preset=custom&from=2026-10-10&to=2026-10-20&gran=month");
    const trigger = await screen.findByRole("button", { name: /模拟点击销售趋势/ });
    fireEvent.click(trigger);
    await waitFor(() => expect(locationSearch).toContain("from=2026-10-10"));
    expect(locationSearch).toContain("to=2026-10-20");
    // 下钻后切到订单明细（tab 默认不落 URL）
    expect(locationSearch).not.toContain("tab=trend");
  });

  it("较上月同期列：按口径金额算增减", async () => {
    renderPage("/sales/analytics?tab=groups");
    expect(await screen.findByText("↑+40.0%")).toBeInTheDocument();   // 700 vs 500
    expect(screen.getByText("上月无")).toBeInTheDocument();
  });

  it("无客户权限：客户类属维度禁选，URL 指向客户维度时退到销售人员", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true, data_reconciliation: true, data_customer: false, data_profit: true,
    }));
    renderPage("/sales/analytics?tab=groups&group_by=customer_type");
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalled());
    expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({ group_by: "salesperson" });
  });
});

describe("趋势页签", () => {
  it("今天预设按日；手动切按月落 URL", async () => {
    renderPage("/sales/analytics?tab=trend");
    await waitFor(() => expect(fetchSalesAnalyticsTrend).toHaveBeenCalled());
    expect(fetchSalesAnalyticsTrend.mock.lastCall?.slice(1, -1)).toEqual(["day", true]);
    fireEvent.click(screen.getByRole("radio", { name: "按月" }));
    await waitFor(() => expect(fetchSalesAnalyticsTrend.mock.lastCall?.[1]).toBe("month"));
    expect(locationSearch).toContain("gran=month");
  });

  it("「全部」预设按月推荐", async () => {
    renderPage("/sales/analytics?tab=trend&preset=all");
    await waitFor(() => expect(fetchSalesAnalyticsTrend).toHaveBeenCalled());
    expect(fetchSalesAnalyticsTrend.mock.lastCall?.[1]).toBe("month");
  });
});

describe("概览页签", () => {
  it("迷你趋势 + 客户 / 销售 / 业务类型三个 Top 5", async () => {
    renderPage("/sales/analytics?tab=overview");
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalledTimes(3));
    const dims = fetchSalesAnalyticsGroups.mock.calls.map((c) => (c[0] as { group_by: string }).group_by).sort();
    expect(dims).toEqual(["business_type", "customer", "salesperson"]);
    expect(fetchSalesAnalyticsGroups.mock.calls.every((c) => (c[0] as { top: number }).top === 5)).toBe(true);
    expect(fetchSalesAnalyticsTrend).toHaveBeenCalled();
    // DL-025 稳态返工回归：TopList 序号与占比原 text-3 在嵌套卡 inset 底
    // （rgb(246,243,238)）实测 4.32:1，改 --mb-text-2（inset 5.15 / 白 5.69）；
    // jsdom 不解析 var()，断言内联样式真的携带该 token（删样式即红）
    const styled = [...document.querySelectorAll('.ant-list span[style*="--mb-text-2"]')];
    const texts = styled.map((s) => s.textContent ?? "");
    expect(texts).toContain("1");          // 序号（A公司 Top1）
    expect(texts).toContain("70.0%");      // 占比（share 0.7 → pct 一位小数）
  });
});

describe("订单明细：导出与金额区间", () => {
  it("导出按当前筛选与排序请求 CSV", async () => {
    downloadSalesOrdersCsv.mockResolvedValue({ data: new Blob(["a"]) });
    const createObjectURL = vi.fn(() => "blob:x");
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    // jsdom 不实现导航：拦下 a.click，避免 "Not implemented: navigation" 噪声
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderPage("/sales/analytics?sort=order_date&order=asc");
    fireEvent.click(await screen.findByRole("button", { name: /导\s*出/ }));
    await waitFor(() => expect(downloadSalesOrdersCsv).toHaveBeenCalled());
    expect(downloadSalesOrdersCsv.mock.lastCall?.slice(1, -1)).toEqual(["order_date", "asc"]);
    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it("金额区间口径被遮蔽：URL 里的区间不进请求，拖动条禁用", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_reconciliation: true, data_customer: true, data_profit: true,
    }));
    renderPage("/sales/analytics?amin=1000&amax=5000");
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalled());
    expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).not.toHaveProperty("amount_min");
    expect(screen.getByRole("spinbutton", { name: "金额下限" })).toBeDisabled();
  });

  it("金额区间输入：失焦提交并按 1k 取整进请求", async () => {
    renderPage();
    const input = await screen.findByRole("spinbutton", { name: "金额下限" });
    fireEvent.change(input, { target: { value: "2499" } });
    fireEvent.blur(input);
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      amount_min: 2000,
    }));
    expect(locationSearch).toContain("amin=2000");
  });
});

describe("多维紧凑图页签", () => {
  it("九维并列各发一次 /groups（sort=指标、top=N），切指标写 URL，点击可下钻条目收窄筛选", async () => {
    renderPage("/sales/analytics?tab=compact");
    expect(await screen.findByTestId("sales-compact")).toBeInTheDocument();
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalledTimes(9));
    const dims = fetchSalesAnalyticsGroups.mock.calls.map(([q]) => (q as { group_by: string }).group_by);
    expect(new Set(dims)).toEqual(new Set([
      "customer", "salesperson", "category", "brand", "pn",
      "business_type", "warehouse", "customer_type", "customer_city",
    ]));
    for (const [q] of fetchSalesAnalyticsGroups.mock.calls) {
      expect(q).toMatchObject({ sort: "amount_ex_tax", order: "desc", top: 10 });
    }
    const customer = await screen.findByTestId("compact-customer");
    // 指标 = 金额区间口径（默认不含税）→ 显示占比
    await waitFor(() => expect(customer).toHaveTextContent("70.0%"));

    const compact = screen.getByTestId("sales-compact");
    fireEvent.click(within(compact).getByText("毛利"));
    await waitFor(() => expect(locationSearch).toContain("cmetric=gross_profit"));
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalledWith(
      expect.objectContaining({ group_by: "customer", sort: "gross_profit" }), CAPTURED_REQUEST));
    // 指标 ≠ 区间口径 → 不显示占比
    await waitFor(() => expect(screen.getByTestId("compact-customer")).not.toHaveTextContent("70.0%"));

    fireEvent.click(await within(screen.getByTestId("compact-customer")).findByRole("button", { name: "A公司" }));
    // 订单明细是默认页签（不落 URL）：离开 tab=compact 即跳转成功
    await waitFor(() => expect(locationSearch).not.toContain("tab=compact"));
    expect(decodeURIComponent(locationSearch)).toContain("customer_name=A公司");
  });

  it("无客户 / 成本 / 毛利权限：客户类维度不出、受限指标不出", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_reconciliation: true, data_customer: false, data_profit: false,
    }));
    renderPage("/sales/analytics?tab=compact&cmetric=amount_ex_tax");
    expect(await screen.findByTestId("sales-compact")).toBeInTheDocument();
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalledTimes(6));
    const dims = fetchSalesAnalyticsGroups.mock.calls.map(([q]) => (q as { group_by: string }).group_by);
    expect(dims).not.toContain("customer");
    expect(dims).not.toContain("customer_type");
    expect(dims).not.toContain("customer_city");
    // URL 残值不含税受限 → 回退首个可用指标（营收）
    for (const [q] of fetchSalesAnalyticsGroups.mock.calls) {
      expect(q).toMatchObject({ sort: "revenue" });
    }
    const compact = screen.getByTestId("sales-compact");
    expect(within(compact).queryByText("不含税金额")).not.toBeInTheDocument();
    expect(within(compact).queryByText("毛利")).not.toBeInTheDocument();
    expect(within(compact).getByText("营收")).toBeInTheDocument();
  });
});
