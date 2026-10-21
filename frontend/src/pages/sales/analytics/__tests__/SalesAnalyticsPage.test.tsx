// 销售分析页集成（D-41 / D-40 PR-4b）：URL 单一真值 —— 默认今天零参数、直开链接恢复
// 筛选与含退货、切换金额口径不重新请求、金额/客户/对账脱敏账号的控件门控与「—」/
// 「无对账数据」占位、排序回退如实提示（sort 回显 ≠ 请求值）、错误与空态、详情抽屉。
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { MemoryRouter, useLocation, useNavigationType, useNavigate } from "react-router-dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchSalesAnalyticsOptions = vi.fn();
const fetchSalesAnalyticsSummary = vi.fn();
const fetchSalesAnalyticsOrders = vi.fn();
const fetchSalesAnalyticsOrderDetail = vi.fn();
const fetchSalesAmountHistogram = vi.fn();
// DL-010 返工用例：导出 / 四视图取数也要可断言（默认无实现——被所属会话守卫拒发
// 时不应被调用；守卫失效会让用例先在「未调用」断言上红，不会真发请求）
const downloadSalesOrdersCsv = vi.fn();
const fetchSalesAnalyticsGroups = vi.fn();
const fetchSalesAnalyticsTrend = vi.fn();

vi.mock("../../../../api/salesAnalytics", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../../api/salesAnalytics",
  );
  return {
    ...actual,
    fetchSalesAnalyticsOptions: (...a: unknown[]) => fetchSalesAnalyticsOptions(...a),
    fetchSalesAnalyticsSummary: (...a: unknown[]) => fetchSalesAnalyticsSummary(...a),
    fetchSalesAnalyticsOrders: (...a: unknown[]) => fetchSalesAnalyticsOrders(...a),
    fetchSalesAnalyticsOrderDetail: (...a: unknown[]) => fetchSalesAnalyticsOrderDetail(...a),
    fetchSalesAmountHistogram: (...a: unknown[]) => fetchSalesAmountHistogram(...a),
    downloadSalesOrdersCsv: (...a: unknown[]) => downloadSalesOrdersCsv(...a),
    fetchSalesAnalyticsGroups: (...a: unknown[]) => fetchSalesAnalyticsGroups(...a),
    fetchSalesAnalyticsTrend: (...a: unknown[]) => fetchSalesAnalyticsTrend(...a),
  };
});
// jsdom 无 canvas：mock 图表壳（金额分布条 / 看板图表），只保留空态文案
vi.mock("../../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText, ariaLabel }: { empty?: boolean; emptyText?: string; ariaLabel?: string }) =>
    empty ? <div>{emptyText ?? "空"}</div> : <div data-testid="chart" aria-label={ariaLabel} />,
}));

import SalesAnalyticsPage from "../SalesAnalyticsPage";

// ---------------------------------------------------------------- 假数据（对齐 PR-3b 响应形态）

const TODAY = "2026-10-17";
// 顶层 as_of 与后端一致是 date-only（today.isoformat()）；对账块/历史里的
// reconciled_as_of / created_at 才是 datetime（后端存 UTC、带偏移输出；页面按 Asia/Shanghai 展示）
const AS_OF = "2026-10-17";

const OPTIONS = {
  business_types: [
    { value: "备件销售", orders: 9 },
    { value: "服务", orders: 5 },
    { value: null, orders: 2 },
  ],
};

// 金额分布默认样本（A-AC09：bins 右端闭 + 负/空单列，sum 守恒）；边界行为
// （8000/8001/8501）由直方图组件与 option 纯函数测试覆盖，这里只保证页面可挂载。
const HISTOGRAM = {
  amount_basis: "amount_ex_tax",
  max: 8000,
  bins: [
    { lo: 0, hi: 2000, orders: 1 },
    { lo: 2000, hi: 8000, orders: 1 },
  ],
  negative_orders: 0,
  empty_orders: 0,
  total: 2,
};

const SUMMARY = {
  window: { date_from: TODAY, date_to: TODAY },
  filters_echo: { business_type: [], customer: null, salesperson: null },
  as_of: AS_OF,
  kpi: {
    orders: 5,
    amount_ex_tax_total: 1000,
    amount_inc_tax_total: 1130,
    amount_inc_tax_missing: 1,
    revenue_total: 800,
  },
};

const RECON = {
  has_data: true,
  reconciled_as_of: "2026-10-15T08:00:00+00:00",
  order_amount_at_snapshot: 226,
  reconciled_amount: 100,
  pending_amount: 126,
  return_reconciled_amount: 10,
  reconciled_display: 100, // 不含退货（默认）
};

const ORDER_ROW = {
  order_id: 201, order_no: "XS-2026-201", order_date: "2026-10-16", is_future: false,
  salesperson: "王五", customer: "客户甲", business_type: "备件销售", data_status: "已生效",
  amount_ex_tax: 200, amount_inc_tax: 226, revenue: 150,
  reconciliation: RECON,
};

/** 订单列表响应。sort 回显 = 实际生效排序（金额键被遮蔽时后端回退 revenue）。 */
const ordersResp = (over: Record<string, unknown> = {}) => ({
  total: 1, page: 1, page_size: 50, as_of: AS_OF,
  sort: "amount_ex_tax", include_returns: false,
  items: [ORDER_ROW],
  ...over,
});

const ORDER_DETAIL = {
  order_id: 201, order_no: "XS-2026-201", order_date: "2026-10-16", is_future: false,
  salesperson: "王五", customer: "客户甲", business_type: "备件销售", data_status: "已生效",
  amount_ex_tax: 200, amount_inc_tax: 226, revenue: 150,
  as_of: AS_OF,
  reconciliation: { ...RECON, reconciled_display: 110 }, // 含退货 = 已对账 100 + 退货 10
  reconciliation_history: [
    {
      import_batch_id: 7, source: "import", created_at: "2026-10-01T08:00:00+00:00",
      reconciled_as_of: "2026-09-30T08:00:00+00:00", order_amount_at_snapshot: 226,
      reconciled_amount: 50, pending_amount: 176, return_reconciled_amount: 5,
    },
    {
      import_batch_id: 9, source: "backfill", created_at: "2026-10-15T08:00:00+00:00",
      reconciled_as_of: "2026-10-15T08:00:00+00:00", order_amount_at_snapshot: 226,
      reconciled_amount: 100, pending_amount: 126, return_reconciled_amount: 10,
    },
  ],
};

// ---------------------------------------------------------------- 渲染骨架

let locationSearch = "";
let navigationType = "";
function LocationProbe() {
  const loc = useLocation();
  locationSearch = loc.search;
  navigationType = useNavigationType();
  const navigate = useNavigate();
  return <button onClick={() => navigate(-1)}>浏览器后退</button>;
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
  localStorage.setItem("role", "boss");
  localStorage.setItem("permissions", JSON.stringify({
    data_purchase_cost: true, data_reconciliation: true, data_customer: true,
  }));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // UTC ISO 钉死时刻（= 上海 2026-10-17 10:00），不随机器时区漂移
  vi.setSystemTime(new Date("2026-10-17T02:00:00Z"));
  fetchSalesAnalyticsOptions.mockResolvedValue({ data: OPTIONS });
  fetchSalesAnalyticsSummary.mockResolvedValue({ data: SUMMARY });
  // 金额分布在订单页签常驻挂载（有金额区间权限时），给默认样本防 vi.fn() 返回 undefined
  fetchSalesAmountHistogram.mockResolvedValue({ data: HISTOGRAM });
  // 回显契约：sort / page / include_returns 原样回显请求值（金额被遮蔽的场景单独覆写）。
  // 数据集只有 1 单：越过末页的请求返回空 items + total=1（对齐后端不按 total
  // 钳制分页的真实行为——过期分享链的越界页码由页组件自行收敛）
  fetchSalesAnalyticsOrders.mockImplementation((q: Record<string, unknown>) => {
    const page = (q.page as number) ?? 1;
    return Promise.resolve({ data: ordersResp({
      sort: q.sort ?? "amount_ex_tax",
      page,
      include_returns: q.include_returns === true,
      ...(page > 1 ? { items: [] } : {}),
    }) });
  });
  fetchSalesAnalyticsOrderDetail.mockResolvedValue({ data: ORDER_DETAIL });
});

afterEach(() => {
  // antd message 渲染在 React 树之外的 body 上，cleanup 卸不掉；假时钟下自动关闭
  // 定时器也不会走——不主动 destroy 会把上一用例的 toast 漏给下一用例的缺席断言

  vi.useRealTimers();
  cleanup();
});

// ---------------------------------------------------------------- 用例

describe("销售分析页：URL 单一真值", () => {
  it("默认渲染：零参数 URL，默认今天（date_from = date_to = 今天），三金额并排 + 不含税金额倒序", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledWith({ date_from: TODAY, date_to: TODAY });
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toEqual({
      date_from: TODAY, date_to: TODAY,
      sort: "amount_ex_tax", order: "desc", page: 1, page_size: 50, include_returns: false,
    });
    expect(locationSearch).toBe("");
    // KPI：销售单数 + 三金额合计并排（默认全选）
    expect(screen.getByText("销售单数")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText(/1,?000\.00/)).toBeInTheDocument();
    expect(screen.getByText(/1,?130\.00/)).toBeInTheDocument();
    expect(screen.getByText(/800\.00/)).toBeInTheDocument();
    // 含税缺失单数的如实披露（不以推算值冒充）
    expect(screen.getByText(/1 单缺失/)).toBeInTheDocument();
    // 列表三金额列 + 对账块列都在
    for (const t of ["不含税金额", "含税订单金额", "营收", "已对账金额", "待对账金额", "退货已对账金额", "数据截至时间"]) {
      expect(screen.getByRole("columnheader", { name: t }), t).toBeInTheDocument();
    }
  });

  it("「全部」预设不传任何日期参数（后端缺省 = 全部历史）", async () => {
    renderPage("/sales/analytics?preset=all");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledWith({});
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({
      sort: "amount_ex_tax", page: 1,
    });
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).not.toHaveProperty("date_from");
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).not.toHaveProperty("date_to");
  });

  it("直开完整筛选链接：多选/客户/排序/翻页/含退货全量恢复进请求与 URL", async () => {
    renderPage(
      "/sales/analytics?preset=all"
      + "&business_type=%E5%A4%87%E4%BB%B6%E9%94%80%E5%94%AE,%E6%9C%8D%E5%8A%A1"
      + "&customer=%E5%AE%A2%E6%88%B7%E7%94%B2&sort=amount_inc_tax&page=2&include_returns=1",
    );
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledWith({
      business_type: ["备件销售", "服务"], customer: "客户甲",
    });
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({
      business_type: ["备件销售", "服务"],
      customer: "客户甲",
      sort: "amount_inc_tax",
      page: 2,
      include_returns: true,
    });
    expect(locationSearch).toContain("include_returns=1");
  });

  it("切换金额口径是纯展示切换：不触发汇总/订单重新请求，只写 URL", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    await waitFor(() => expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(1));

    // 多选去掉「含税订单金额」：下拉里点已选项 = 取消选择（jsdom 下比点标签 × 稳）
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "金额口径" }));
    fireEvent.click(
      await screen.findByText("含税订单金额", { selector: ".ant-select-item-option-content" }),
    );
    await waitFor(() => expect(new URLSearchParams(locationSearch).getAll("basis")).toEqual(["ex", "revenue"]));
    expect(navigationType).toBe("REPLACE");
    expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(1);
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1);
    // KPI 卡与列一并消失（含税 KPI 卡的缺失披露文案随之消失，不许恒显三卡）
    expect(screen.queryByRole("columnheader", { name: "含税订单金额" })).toBeNull();
    expect(screen.queryByText(/1 单缺失/)).toBeNull();
    expect(screen.getByRole("columnheader", { name: "不含税金额" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "营收" })).toBeInTheDocument();
  });

  it("含退货开关只影响列表/详情（reconciled_display）：重拉订单不重拉 KPI，状态落 URL", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    await waitFor(() => expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("switch", { name: "含已退货" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    expect(fetchSalesAnalyticsOrders.mock.calls[1][0]).toMatchObject({ include_returns: true });
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1); // KPI 与退货口径无关
    expect(locationSearch).toContain("include_returns=1");
    // 列头随口径改标：此时列值 = 已对账 + 退货已对账（D-40 §5），不得仍叫「已对账金额」
    expect(await screen.findByRole("columnheader", { name: "已对账金额（含退货）" }))
      .toBeInTheDocument();

    // 关回去：请求恢复默认（不落键），URL 摘掉
    fireEvent.click(screen.getByRole("switch", { name: "含已退货" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(3));
    expect(fetchSalesAnalyticsOrders.mock.calls[2][0]).toMatchObject({ include_returns: false });
    expect(locationSearch).not.toContain("include_returns");
  });

  it("金额不可见账号（data_purchase_cost=false）：行级「—」、金额排序入口禁用、"
    + "sort 回退如实提示；KPI 合计与口径控件仍公开（后端有意设计）", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_reconciliation: true, data_customer: true,
    }));
    // 后端真实形态（契约 §10.3 / Q-A-04）：行级金额遮蔽为 null，sort 静默回退
    // order_date 并按生效值回显
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({
      sort: "order_date",
      items: [{ ...ORDER_ROW, amount_ex_tax: null, amount_inc_tax: null, revenue: 150 }],
    }) });
    renderPage();

    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    // 回退提示以响应回显为准（请求 amount_ex_tax ≠ 回显 order_date）
    expect(screen.getByText(/所选排序字段不可见：已改按订单日期排序/)).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /XS-2026-201/ });
    expect(row.textContent).toContain("—");
    expect(row.textContent).not.toContain("200.00");
    // 金额列排序入口预先禁用（不发注定被回退的请求）；营收（恒公开）仍可排
    expect(screen.getByRole("columnheader", { name: "不含税金额" }).closest("th")?.className)
      .not.toContain("ant-table-column-has-sorters");
    expect(screen.getByRole("columnheader", { name: "营收" }).closest("th")?.className)
      .toContain("ant-table-column-has-sorters");
    fireEvent.click(screen.getByRole("columnheader", { name: "不含税金额" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(1));
    // KPI 合计不随行级遮蔽（PR-3b 有意公开），口径控件保留
    expect(screen.getByRole("combobox", { name: "金额口径" })).toBeInTheDocument();
    expect(screen.getByText(/1,?130\.00/)).toBeInTheDocument();
    expect(screen.getByText(/800\.00/)).toBeInTheDocument();
    // 金额分布（直方图）整体不挂载（A-AC03）：分布本身就是金额信息，三口径一视
    // 同仁、营收口径也无例外；不发注定 403 的直方图请求
    expect(screen.queryByLabelText("订单金额分布")).toBeNull();
    expect(fetchSalesAmountHistogram).not.toHaveBeenCalled();
  });

  it("客户不可见账号（data_customer=false）：客户搜索禁用，URL 残值不进请求（不发 403 请求）", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true, data_reconciliation: true, data_customer: false,
    }));
    // 分享链接来自权限完整账号：带着 customer 残值
    renderPage("/sales/analytics?customer=%E5%AE%A2%E6%88%B7%E7%94%B2");

    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    // 客户与客户类型两个多选都禁用（后端对客户类属条件 fail-closed 403）
    expect(screen.getByRole("combobox", { name: "客户" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "客户类型" })).toBeDisabled();
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledWith({ date_from: TODAY, date_to: TODAY });
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).not.toHaveProperty("customer");
  });

  it("对账不可见账号（data_reconciliation=false）：开关不渲染，对账列一律「无对账数据」而非 0", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true, data_reconciliation: false, data_customer: true,
    }));
    renderPage();

    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "含已退货" })).toBeNull();
    const row = screen.getByRole("row", { name: /XS-2026-201/ });
    expect(row.textContent).toContain("无对账数据");
    expect(row.textContent).not.toContain("100.00");
  });

  it("无对账快照（has_data=false）：同样按「无对账数据」展示（与权限不可见同文案，不显示 0）", async () => {
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({
      items: [{
        ...ORDER_ROW,
        reconciliation: {
          has_data: false, reconciled_as_of: null, order_amount_at_snapshot: null,
          reconciled_amount: null, pending_amount: null, return_reconciled_amount: null,
          reconciled_display: null,
        },
      }],
    }) });
    renderPage();

    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /XS-2026-201/ });
    expect(row.textContent).toContain("无对账数据");
    expect(screen.getAllByText("无对账数据").length).toBeGreaterThan(0);
  });

  it("有快照但金额与时间全为 null（后端遮蔽/快照全空）：列表与抽屉都按「无对账数据」展示", async () => {
    const EMPTY_RECON = {
      has_data: true, reconciled_as_of: null, order_amount_at_snapshot: null,
      reconciled_amount: null, pending_amount: null, return_reconciled_amount: null,
      reconciled_display: null,
    };
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({
      items: [{ ...ORDER_ROW, reconciliation: EMPTY_RECON }],
    }) });
    fetchSalesAnalyticsOrderDetail.mockResolvedValue({
      data: { ...ORDER_DETAIL, reconciliation: EMPTY_RECON, reconciliation_history: [] },
    });
    renderPage();
    const row = await screen.findByRole("row", { name: "查看销售单 XS-2026-201 详情" });
    // 本地权限快照为 true（beforeEach），仍按响应内容判无数据
    expect(row.textContent).toContain("无对账数据");
    fireEvent.click(row);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(dialog).getByText("无对账数据（对账金额与时间均为空或不可见）")).toBeInTheDocument());
    expect(within(dialog).queryByText("对账快照（最新）")).toBeNull();
  });

  it("列头点「含税订单金额」：切换排序键并回第 1 页，状态落 URL；再点切升序（D-44 §4）", async () => {
    // 页码 2 合法（120 单/50 每页）：本例验排序切换回第 1 页，越界收敛另有专测
    fetchSalesAnalyticsOrders.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: ordersResp({ total: 120, sort: q.sort, order: q.order ?? "desc" }) }));
    renderPage("/sales/analytics?page=2");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(1));

    // 注意用全名匹配：/含税金额/ 会连「不含税金额」一起匹配上
    fireEvent.click(screen.getByRole("columnheader", { name: "含税订单金额" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    expect(fetchSalesAnalyticsOrders.mock.calls[1][0]).toMatchObject({
      sort: "amount_inc_tax", page: 1,
    });
    expect(locationSearch).toContain("sort=amount_inc_tax");
    expect(navigationType).toBe("REPLACE");
    expect(locationSearch).not.toContain("page=");
    // 再点同一列：升序
    fireEvent.click(screen.getByRole("columnheader", { name: "含税订单金额" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      sort: "amount_inc_tax", order: "asc",
    }));
    expect(locationSearch).toContain("order=asc");
  });

  it("退货已对账金额列双向排序（契约 §3 返工第四对账列）：sort=return_reconciled_amount 进请求与 URL", async () => {
    fetchSalesAnalyticsOrders.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: ordersResp({ sort: q.sort, order: q.order ?? "desc" }) }));
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("columnheader", { name: "退货已对账金额" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      sort: "return_reconciled_amount", order: "desc",
    }));
    expect(locationSearch).toContain("sort=return_reconciled_amount");
    // 再点切升序（A-AC07 双向）
    fireEvent.click(screen.getByRole("columnheader", { name: "退货已对账金额" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      sort: "return_reconciled_amount", order: "asc",
    }));
  });

  it("订单行点击打开详情抽屉：三金额 + 最新对账块 + 对账历史（每批次一条，旧→新）", async () => {
    renderPage();
    const row = await screen.findByRole("row", { name: "查看销售单 XS-2026-201 详情" });
    fireEvent.click(row);

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/XS-2026-201/)).toBeInTheDocument());
    expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledWith(201, false);
    // 最新对账块
    expect(within(dialog).getByText("对账快照（最新）")).toBeInTheDocument();
    expect(within(dialog).getByText(/110\.00/)).toBeInTheDocument(); // 含退货口径值
    // 数据截至时间出现两处 = 最新块与历史末条一致（D-40：末条即最新快照）；
    // 后端 UTC 08:00+00:00 → 北京时间 16:00
    expect(within(dialog).getAllByText("2026-10-15 16:00").length).toBe(2);
    // 历史：两个批次（来源 导入/回填），导入批次号 7 在列
    expect(within(dialog).getByText("回填")).toBeInTheDocument();
    expect(within(dialog).getByText("7")).toBeInTheDocument();
    expect(within(dialog).getByText("9")).toBeInTheDocument();
  });

  it("含退货开时打开抽屉：include_returns 透传，已对账金额标签带「含退货」", async () => {
    renderPage("/sales/analytics?include_returns=1");
    const row = await screen.findByRole("row", { name: "查看销售单 XS-2026-201 详情" });
    fireEvent.click(row);

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledWith(201, true));
    expect(within(dialog).getByText("已对账金额（含退货）")).toBeInTheDocument();
  });

  it("业务类型多选：选项带全时间计数、「未填写」组走 __empty__ 哨兵可筛（契约 §0 / A-AC06）", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByRole("combobox", { name: "业务类型" }));
    // null 取值组不再禁选：选中后按后端哨兵 __empty__ 下发（不与真实「未填」名称碰撞）
    const nullOpt = (await screen.findByText(/（未填写）（2）/, { selector: ".ant-select-item-option-content" }))
      .closest(".ant-select-item-option");
    expect(nullOpt).not.toHaveClass("ant-select-item-option-disabled");
    fireEvent.click(await screen.findByText(/（未填写）（2）/, { selector: ".ant-select-item-option-content" }));

    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    expect(fetchSalesAnalyticsOrders.mock.calls[1][0]).toMatchObject({
      business_type: ["__empty__"],
    });
    expect(fetchSalesAnalyticsSummary).toHaveBeenLastCalledWith(
      expect.objectContaining({ business_type: ["__empty__"], date_from: TODAY, date_to: TODAY }),
    );
    // URL 落哨兵值，刷新/分享后语义不变
    expect(locationSearch).toContain("business_type=__empty__");
  });

  it("旧链接客户模糊条件：以可关闭标签展示并进请求；关闭标签即移除（D-44 改为多选，不再提供输入）", async () => {
    renderPage("/sales/analytics?customer=%E5%AE%A2%E6%88%B7%E7%94%B2");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({ customer: "客户甲" });
    const tag = screen.getByText("客户含「客户甲」");
    fireEvent.click(within(tag.closest(".ant-tag") as HTMLElement).getByRole("img", { name: /close/i }));
    await waitFor(() => expect(locationSearch).not.toContain("customer="));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).not.toHaveProperty("customer"));
  });

  it("未来单打标：订单日期晚于今天显示「未来」标签（本页不排除未来单）", async () => {
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({
      items: [{ ...ORDER_ROW, order_date: "2026-10-30", is_future: true }],
    }) });
    renderPage();
    const row = await screen.findByRole("row", { name: /XS-2026-201/ });
    expect(within(row).getByText("未来")).toBeInTheDocument();
  });

  it("日期预设切换（页面级交互）：切「全部」不传日期并回第 1 页（resetPage 接线）", async () => {
    // 数据集撑得起 5 页（260 单/50 每页），页码本身合法——本例只验预设切换的
    // resetPage 接线，越界页码收敛另有专测
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({ total: 260 }) });
    renderPage("/sales/analytics?page=5");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(1));
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({ page: 5, date_from: TODAY });

    fireEvent.mouseDown(screen.getByRole("combobox", { name: "日期预设" }));
    fireEvent.click(await screen.findByText("全部", { selector: ".ant-select-item-option-content" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    const q = fetchSalesAnalyticsOrders.mock.calls[1][0] as Record<string, unknown>;
    expect(q).toMatchObject({ page: 1 });
    expect(q).not.toHaveProperty("date_from");
    expect(q).not.toHaveProperty("date_to");
    expect(fetchSalesAnalyticsSummary).toHaveBeenLastCalledWith({});
    expect(locationSearch).toContain("preset=all");
    expect(locationSearch).not.toContain("page=");
  });

  it("自定义区间直入：闭区间日期原样进请求", async () => {
    renderPage("/sales/analytics?preset=custom&from=2026-09-01&to=2026-10-15");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledWith({
      date_from: "2026-09-01", date_to: "2026-10-15",
    });
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({
      date_from: "2026-09-01", date_to: "2026-10-15",
    });
  });

  it("排序键口径未显示时收敛（D-41 只按已显示金额倒序）：basis 缺 inc 则 sort=amount_inc_tax 收敛为 amount_ex_tax", async () => {
    renderPage("/sales/analytics?sort=amount_inc_tax&basis=ex,revenue");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    // 请求按首个已显示口径发出；URL 排序键原值保留（分享态不被改写）
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({ sort: "amount_ex_tax" });
    expect(locationSearch).toContain("sort=amount_inc_tax");
    // 回显与请求一致 → 不是权限回退，无回退提示
    expect(screen.queryByText(/已按营收/)).toBeNull();

    // 把该口径加回显示集：收敛解除，按 URL 排序键换键重拉
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "金额口径" }));
    fireEvent.click(await screen.findByText("含税订单金额", { selector: ".ant-select-item-option-content" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    expect(fetchSalesAnalyticsOrders.mock.calls[1][0]).toMatchObject({ sort: "amount_inc_tax" });
  });

  it("销售人员多选（D-44）：候选来自 options，选中后精确匹配进请求与 URL；重置清空", async () => {
    fetchSalesAnalyticsOptions.mockResolvedValue({ data: {
      ...OPTIONS, salespeople: [{ value: "王五", orders: 3 }, { value: null, orders: 1 }],
    } });
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    const select = screen.getByRole("combobox", { name: "销售人员" });
    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByText("王五（3）"));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      salesperson_name: ["王五"],
    }));
    expect(fetchSalesAnalyticsSummary).toHaveBeenLastCalledWith(
      expect.objectContaining({ salesperson_name: ["王五"], date_from: TODAY, date_to: TODAY }),
    );
    expect(locationSearch).toContain("salesperson_name=");
    fireEvent.click(screen.getByRole("button", { name: /重\s*置/ }));
    await waitFor(() => expect(locationSearch).toBe(""));
  });

  it("深链直开抽屉 + 金额口径子集：未选口径的金额行不渲染", async () => {
    renderPage("/sales/analytics?basis=revenue&order_id=201");
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/XS-2026-201/)).toBeInTheDocument());
    expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledWith(201, false);
    expect(within(dialog).getByText(/150\.00/)).toBeInTheDocument(); // 营收行
    expect(within(dialog).queryByText(/不含税金额/)).toBeNull();
    expect(within(dialog).queryByText(/含税订单金额/)).toBeNull();
  });
});

describe("销售分析页：健壮性", () => {
  it("汇总 403（detail 透传）：错误 Alert 展示后端 detail，重试成功后恢复", async () => {
    fetchSalesAnalyticsSummary.mockRejectedValue({
      response: { status: 403, data: { detail: "仅管理员与老板可访问销售分析" } },
    });
    renderPage();

    expect(await screen.findByText("汇总加载失败")).toBeInTheDocument();
    expect(screen.getByText("仅管理员与老板可访问销售分析")).toBeInTheDocument();

    fetchSalesAnalyticsSummary.mockResolvedValue({ data: SUMMARY });
    fireEvent.click(screen.getByText("重试"));
    expect(await screen.findByText(/1,?130\.00/)).toBeInTheDocument();
    expect(screen.queryByText("汇总加载失败")).toBeNull();
  });

  it("订单列表 422（FastAPI 数组 detail）：回退文案提示参数非法（不吞错）", async () => {
    fetchSalesAnalyticsOrders.mockRejectedValue({
      response: { status: 422, data: { detail: [{ loc: ["query", "sort"], msg: "Input should be 'amount_ex_tax' ..." }] } },
    });
    renderPage();
    expect(await screen.findByText("销售单列表加载失败")).toBeInTheDocument();
    expect(screen.getByText("请求参数非法（422）")).toBeInTheDocument();
  });

  it("空数据：列表给出空态文案（不是报错），KPI 计数为 0、金额合计「—」", async () => {
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({ total: 0, items: [] }) });
    fetchSalesAnalyticsSummary.mockResolvedValue({ data: {
      ...SUMMARY,
      kpi: { orders: 0, amount_ex_tax_total: null, amount_inc_tax_total: null, amount_inc_tax_missing: 0, revenue_total: null },
    } });
    renderPage();
    expect(await screen.findByText("当前筛选下没有销售单")).toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("订单详情 404：提示「订单不存在（或非已生效单）」并自动关抽屉、URL 摘键", async () => {
    fetchSalesAnalyticsOrderDetail.mockRejectedValue({ response: { status: 404 } });
    renderPage("/sales/analytics?order_id=201");
    await waitFor(() => expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledWith(201, false));
    expect(await screen.findByText("订单不存在（或非已生效单）")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(locationSearch).not.toContain("order_id=");
  });

  it("请求在途时手动关抽屉：迟到的失败不再弹全局提示（关闭即作废在途请求）", async () => {
    let rejectDetail: ((e: unknown) => void) | undefined;
    fetchSalesAnalyticsOrderDetail.mockImplementation(
      () => new Promise((_res, reject) => { rejectDetail = reject; }),
    );
    renderPage("/sales/analytics?order_id=201");
    await waitFor(() => expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledTimes(1));

    // 用户在响应返回前关掉抽屉（antd Drawer 右上角关闭按钮）
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(locationSearch).not.toContain("order_id=");

    // 迟到的 404 到达：用户已离开该上下文，不得再弹「订单不存在」
    rejectDetail?.({ response: { status: 404 } });
    await waitFor(() => expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("订单不存在（或非已生效单）")).toBeNull();
    expect(screen.queryByText("订单详情加载失败")).toBeNull();
  });

  it("DL-010 会话守卫：汇总在途时 localStorage token 变更，迟到响应不落地", async () => {
    localStorage.setItem("token", "account-a");
    let resolveSummary!: (v: { data: typeof SUMMARY }) => void;
    fetchSalesAnalyticsSummary.mockImplementationOnce(
      () => new Promise((res) => { resolveSummary = res; }),
    );
    renderPage();
    await waitFor(() => expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1));
    // 在途：KPI 值为「—」（不是 0，也不是另一会话的数据）
    const salesCard = () => (screen.getByText("销售单数").parentElement as HTMLElement).textContent ?? "";
    expect(salesCard()).toContain("—");

    // 账号切换 / 登出：会话真值已换（直接读 localStorage，不经 React 状态）
    localStorage.setItem("token", "account-b");
    await act(async () => { resolveSummary({ data: SUMMARY }); });

    // 迟到响应到达后不落地（DL-010 落地矩阵）：另一会话的 KPI（5）不渲染，
    // 且检测到过期的分支会通知页级清旧上下文——数据面让位给会话失效提示
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("销售单数")).toBeNull();
    expect(await screen.findByText("登录会话已变更（已切换账号或登出），请刷新页面后继续")).toBeInTheDocument();
    expect(screen.queryByText("汇总加载失败")).toBeNull();
  });

  it("分页点击：第 2 页进请求并落 URL", async () => {
    fetchSalesAnalyticsOrders.mockResolvedValue({ data: ordersResp({ total: 120 }) });
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    // rc-pagination 的点击处理器挂在 li 内层 button 上，点 li 本身不触发
    const page2 = screen.getByTitle("2");
    fireEvent.click(page2.querySelector("button") ?? page2);
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    expect(fetchSalesAnalyticsOrders.mock.calls[1][0]).toMatchObject({ page: 2, page_size: 50 });
    expect(locationSearch).toContain("page=2");
  });

  it("过期分享链页码越过末页：按 total 收敛回末页重拉并改写 URL，不残留误导空态", async () => {
    // 120 单 / 50 每页 = 3 页；分享链里的 page=7 已越界（后端只校验 page≥1，
    // 会照发空 items + total=120——页组件负责收敛，antd 分页高亮才能与请求一致）
    fetchSalesAnalyticsOrders.mockImplementation((q: Record<string, unknown>) => {
      const page = (q.page as number) ?? 1;
      return Promise.resolve({ data: ordersResp({
        total: 120,
        page,
        sort: q.sort ?? "amount_ex_tax",
        include_returns: q.include_returns === true,
        ...(page > 3 ? { items: [] } : {}),
      }) });
    });
    renderPage("/sales/analytics?page=7");
    // 首请求按 URL 原样发出 page=7
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({ page: 7 }));
    // 响应证明越界 → 自动按末页（第 3 页）重拉，URL 页码对齐
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(2));
    expect(fetchSalesAnalyticsOrders.mock.calls[1][0]).toMatchObject({ page: 3 });
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    expect(locationSearch).toContain("page=3");
    // 其他页有数据：不得展示「没有销售单」的误导空态
    expect(screen.queryByText("当前筛选下没有销售单")).toBeNull();
  });

  it("admin 恒放行：本地 data_* 快照全 false 也不做任何门控/收敛", async () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_reconciliation: false, data_customer: false,
    }));
    renderPage("/sales/analytics?customer=%E5%AE%A2%E6%88%B7%E7%94%B2");
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    // 客户筛选照发（admin 不受限），对账/金额控件照常展示
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({ customer: "客户甲" });
    expect(screen.getByRole("switch", { name: "含已退货" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "已对账金额" })).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /XS-2026-201/ });
    expect(row.textContent).not.toContain("无对账数据");
  });
});

describe("DL-010 所属会话守卫（返工：静默换 token 后请求前拒发）", () => {
  // 返工要求：owner 是组件挂载时的 token；静默换 token（无重渲染、无 storage 事件）
  // 后，旧会话的导出 / 下钻 / 取数一律拒发并清旧上下文。每个用例都先走同会话正向
  // 路径，证明守卫不是「全部禁用」的伪通过。
  const SESSION_DEAD_TEXT = "登录会话已变更（已切换账号或登出），请刷新页面后继续";

  it("同会话导出正常发起并收尾；静默换 token 后点导出拒发、旧上下文让位", async () => {
    localStorage.setItem("token", "account-a");
    // jsdom 无 createObjectURL：补静态桩让正向路径完整走完（下载触发本身也有守卫）
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    });
    downloadSalesOrdersCsv.mockResolvedValue({ data: new Blob(["csv"], { type: "text/csv" }) });
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();

    // 正向：同会话点击导出 → 请求发出、finally 解除 loading（按钮可再次点击）
    fireEvent.click(screen.getByRole("button", { name: /导出/ }));
    await waitFor(() => expect(downloadSalesOrdersCsv).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: /导出/ })).toBeEnabled();

    // 静默换 token 后旧会话的导出按钮不再发请求（A 换 B 再点 A 时代的按钮）
    localStorage.setItem("token", "account-b");
    fireEvent.click(screen.getByRole("button", { name: /导出/ }));
    expect(downloadSalesOrdersCsv).toHaveBeenCalledTimes(1);
    // 旧会话上下文已清：汇总 / 列表让位给会话失效提示，不冒充当前会话结果
    expect(screen.getByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /XS-2026-201/ })).toBeNull();
  });

  it("同会话点列排序即重拉；静默换 token 后排序 / 下钻拒发新订单请求", async () => {
    localStorage.setItem("token", "account-a");
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    const baseline = fetchSalesAnalyticsOrders.mock.calls.length;

    // 正向：同会话点「含税订单金额」列头 → 立即发出新排序请求
    fireEvent.click(screen.getByRole("columnheader", { name: "含税订单金额" }));
    await waitFor(() => expect(fetchSalesAnalyticsOrders.mock.calls.length).toBe(baseline + 1));
    expect(fetchSalesAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({ sort: "amount_inc_tax" });

    // 静默换 token：再点「营收」列头 → 拒发（无新请求），旧列表清空并提示
    localStorage.setItem("token", "account-b");
    fireEvent.click(screen.getByRole("columnheader", { name: "营收" }));
    expect(fetchSalesAnalyticsOrders.mock.calls.length).toBe(baseline + 1);
    expect(screen.getByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /XS-2026-201/ })).toBeNull();
  });

  it("同会话可开订单详情抽屉；静默换 token 后重开抽屉拒发", async () => {
    localStorage.setItem("token", "account-a");
    renderPage();
    const row = await screen.findByRole("row", { name: /XS-2026-201/ });
    // 正向：同会话点单号 → 详情请求发出
    fireEvent.click(within(row).getByText("XS-2026-201"));
    await waitFor(() => expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledTimes(1));

    // 关抽屉 → 静默换 token → 重开：旧会话的抽屉不以当前账号取数
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    localStorage.setItem("token", "account-b");
    const rowAgain = screen.getByRole("row", { name: /XS-2026-201/ });
    fireEvent.click(within(rowAgain).getByText("XS-2026-201"));
    expect(fetchSalesAnalyticsOrderDetail).toHaveBeenCalledTimes(1);
    // 抽屉内就地提示会话失效（失败/未知/无权/会话失效分开），不显示旧会话详情；
    // 行点击入口同时通知页级清旧上下文——主页面列表也让位给会话提示（两处文案并存）
    expect((await screen.findAllByText(SESSION_DEAD_TEXT)).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("采购单")).toBeNull();
    expect(screen.queryByRole("row", { name: /XS-2026-201/ })).toBeNull();
  });

  it("静默换 token 后切页签：四视图取数（useAsyncData）同样拒发", async () => {
    localStorage.setItem("token", "account-a");
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    localStorage.setItem("token", "account-b");
    fireEvent.click(screen.getByRole("tab", { name: "概览" }));
    expect(await screen.findByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(fetchSalesAnalyticsGroups).not.toHaveBeenCalled();
    expect(fetchSalesAnalyticsTrend).not.toHaveBeenCalled();
  });

  // ---------------- 落地矩阵（DL-010 本轮返工）：迟到响应不只「静默不落地」，
  // 检测到过期的落地分支必须通知页级清旧上下文；全程不得发出新（B 会话）请求 ----------------

  it("落地矩阵：候选/汇总/列表/直方图全在途换 token，迟到落地通知页级清空，无新请求", async () => {
    localStorage.setItem("token", "account-a");
    const resolvers: Record<string, (v: unknown) => void> = {};
    const defer = (k: string) => () => new Promise((res) => { resolvers[k] = res; });
    fetchSalesAnalyticsOptions.mockImplementation(defer("options"));
    fetchSalesAnalyticsSummary.mockImplementation(defer("summary"));
    fetchSalesAnalyticsOrders.mockImplementation(defer("orders"));
    fetchSalesAmountHistogram.mockImplementation(defer("histogram"));
    renderPage();
    await waitFor(() => expect(resolvers.summary).toBeDefined());

    localStorage.setItem("token", "account-b");
    await act(async () => {
      resolvers.options({ data: OPTIONS });
      resolvers.summary({ data: SUMMARY });
      resolvers.orders({ data: ordersResp() });
      resolvers.histogram({ data: HISTOGRAM });
      await Promise.resolve();
    });

    // 四路迟到响应全部不落地，且各自通知页级：数据面让位给会话失效提示
    expect(await screen.findByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /XS-2026-201/ })).toBeNull();
    expect(screen.queryByText("销售单数")).toBeNull();
    expect(fetchSalesAnalyticsOptions).toHaveBeenCalledTimes(1);
    expect(fetchSalesAnalyticsSummary).toHaveBeenCalledTimes(1);
    expect(fetchSalesAnalyticsOrders).toHaveBeenCalledTimes(1);
    expect(fetchSalesAmountHistogram).toHaveBeenCalledTimes(1);
  });

  it("落地矩阵：趋势/分组视图在途换 token，迟到落地只通知页级，不落地不重发", async () => {
    localStorage.setItem("token", "account-a");
    const resolvers: Record<string, (v: unknown) => void> = {};
    fetchSalesAnalyticsTrend.mockImplementation(() => new Promise((res) => { resolvers.trend = res; }));
    fetchSalesAnalyticsGroups.mockImplementation(() => new Promise((res) => { resolvers.groups = res; }));
    renderPage("/sales/analytics?tab=trend");
    await waitFor(() => expect(fetchSalesAnalyticsTrend).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: "分组排行" }));
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalledTimes(1));

    localStorage.setItem("token", "account-b");
    await act(async () => {
      resolvers.trend({ data: { granularity: "day", buckets: [], weekday: [], compare: null, compare_window: null, compare_available: true } });
      resolvers.groups({ data: { group_by: "customer", groups: [], total_groups: 0, other: null } });
      await Promise.resolve();
    });

    expect(await screen.findByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(fetchSalesAnalyticsTrend).toHaveBeenCalledTimes(1);
    expect(fetchSalesAnalyticsGroups).toHaveBeenCalledTimes(1);
  });

  // ---------------- 客户搜索（owner-search/owner-debounce-switch）：本组件自有
  // 300ms debounce timer 与 found 状态，守卫单独接 owner —— 三态各自可回归 ----------------

  it("客户搜索：同会话 300ms 防抖后发请求，候选正常落地（正向路径）", async () => {
    localStorage.setItem("token", "account-a");
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    fetchSalesAnalyticsOptions.mockResolvedValue({ data: { customers: [{ value: "客户乙", orders: 7 }] } });
    const box = screen.getByRole("combobox", { name: "客户" });
    fireEvent.mouseDown(box);
    fireEvent.change(box, { target: { value: "客" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    // 防抖后精确以搜索词发起（挂载那次不带搜索词，不算）
    await waitFor(() => expect(fetchSalesAnalyticsOptions).toHaveBeenLastCalledWith({}, "客"));
    expect(await screen.findByText("客户乙（7）")).toBeInTheDocument();
  });

  it("客户搜索：防抖窗口内换 token → 旧 timer 到期拒发、清旧候选并通知页级", async () => {
    localStorage.setItem("token", "account-a");
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    const box = screen.getByRole("combobox", { name: "客户" });
    fireEvent.mouseDown(box);
    fireEvent.change(box, { target: { value: "客" } });
    // 防抖窗口内静默换 token：timer 属于旧会话，到期时不得以新身份发请求
    localStorage.setItem("token", "account-b");
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(fetchSalesAnalyticsOptions).toHaveBeenCalledTimes(1); // 仅挂载候选那一次
    expect(await screen.findByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
  });

  it("客户搜索：请求在途换 token → 迟到候选不落地并通知页级", async () => {
    localStorage.setItem("token", "account-a");
    let resolveSearch!: (v: { data: { customers: { value: string; orders: number }[] } }) => void;
    fetchSalesAnalyticsOptions.mockImplementationOnce(
      () => new Promise((res) => { resolveSearch = res; }),
    );
    renderPage();
    expect(await screen.findByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    const box = screen.getByRole("combobox", { name: "客户" });
    fireEvent.mouseDown(box);
    fireEvent.change(box, { target: { value: "客" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    await waitFor(() => expect(fetchSalesAnalyticsOptions).toHaveBeenCalledTimes(2));

    localStorage.setItem("token", "account-b");
    await act(async () => {
      resolveSearch({ data: { customers: [{ value: "客户乙", orders: 7 }] } });
      await Promise.resolve();
    });
    expect(await screen.findByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(screen.queryByText("客户乙（7）")).toBeNull();
  });
});

describe("URL 导航与每页条数", () => {
  it("打开抽屉创建历史，浏览器后退恢复原筛选与每页条数", async () => {
    renderPage("/sales/analytics?page_size=100");
    const row = await screen.findByRole("row", { name: "查看销售单 XS-2026-201 详情" });
    expect(fetchSalesAnalyticsOrders.mock.calls[0][0]).toMatchObject({ page_size: 100 });
    fireEvent.click(row);
    await waitFor(() => expect(new URLSearchParams(locationSearch).get("order_id")).toBe("201"));
    expect(navigationType).toBe("PUSH");
    fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() => expect(new URLSearchParams(locationSearch).has("order_id")).toBe(false));
    expect(new URLSearchParams(locationSearch).get("page_size")).toBe("100");
    expect(navigationType).toBe("POP");
  });
});
