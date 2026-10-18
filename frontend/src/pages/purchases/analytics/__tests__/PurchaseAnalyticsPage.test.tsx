// 采购分析页集成（D-39 PR-4a）：URL 单一真值 —— 默认今天零参数、直开链接恢复筛选与
// 下钻层、切换口径不重新请求、脱敏账号（金额/客户）控件门控与「—」占位、
// 排序保护回显（ranking_restricted 禁用金额排序）、错误与空态。
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchPurchaseAnalyticsOptions = vi.fn();
const fetchPurchaseAnalyticsSummary = vi.fn();
const fetchPurchaseAnalyticsOrders = vi.fn();
const fetchPurchaseAnalyticsOrderDetail = vi.fn();

vi.mock("../../../../api/purchaseAnalytics", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../../api/purchaseAnalytics",
  );
  return {
    ...actual,
    fetchPurchaseAnalyticsOptions: (...a: unknown[]) => fetchPurchaseAnalyticsOptions(...a),
    fetchPurchaseAnalyticsSummary: (...a: unknown[]) => fetchPurchaseAnalyticsSummary(...a),
    fetchPurchaseAnalyticsOrders: (...a: unknown[]) => fetchPurchaseAnalyticsOrders(...a),
    fetchPurchaseAnalyticsOrderDetail: (...a: unknown[]) => fetchPurchaseAnalyticsOrderDetail(...a),
  };
});

import PurchaseAnalyticsPage from "../PurchaseAnalyticsPage";

// ---------------------------------------------------------------- 假数据（对齐 PR-3a 响应形态）

const TODAY = "2026-10-17";

const OPTIONS = {
  source_types: ["备件采购", "批量采购", "杂项采购"],
  purchasers: ["张三", "李四"],
  salespersons: ["王五"],
  categories: ["硬盘", "内存", "未分类"],
  uncategorized_label: "未分类",
  no_linked_label: "（无关联销售单）",
};

const echo = (over: Record<string, unknown> = {}) => ({
  date_from: TODAY, date_to: TODAY,
  source_type: [], purchaser: [], salesperson: [], customer_id: [], category: [],
  group_by: "source_type", group_key: null, ...over,
});

const SUMMARY = {
  basis_mode: "order",
  filters_echo: echo(),
  as_of: "2026-10-17 09:00",
  kpi: {
    orders: 5, lines: 12,
    amount_inc: 1130, amount_ex: 1000, amount_paid: 1050,
    nominal_inc_share: 0.2,
  },
  groups: [
    { key: "备件采购", label: "备件采购", orders: 3, lines: 8, amount_inc: 678, amount_ex: 600, amount_paid: 630 },
    { key: "批量采购", label: "批量采购", orders: 2, lines: 4, amount_inc: 452, amount_ex: 400, amount_paid: 420 },
  ],
  groups_ranking_restricted: false,
};

/** 金额被 data_purchase_cost 遮蔽时的响应形态：金额键保留、值置 null。 */
const SUMMARY_MASKED = {
  ...SUMMARY,
  kpi: {
    orders: 5, lines: 12,
    amount_inc: null, amount_ex: null, amount_paid: null, nominal_inc_share: null,
  },
  groups: SUMMARY.groups.map((g) => ({
    ...g, amount_inc: null, amount_ex: null, amount_paid: null,
  })),
  groups_ranking_restricted: true,
};

/** 客户分组（取值反查用）：key=客户 id，label=null，客户名在 customer 键上。 */
const SUMMARY_BY_CUSTOMER = {
  ...SUMMARY,
  filters_echo: echo({ group_by: "customer" }),
  groups: [
    { key: "12", label: null, customer: "客户甲", orders: 3, lines: 8, amount_inc: 678, amount_ex: 600, amount_paid: 630 },
    { key: "30", label: null, customer: "客户乙", orders: 2, lines: 4, amount_inc: 452, amount_ex: 400, amount_paid: 420 },
    { key: OPTIONS.no_linked_label, label: OPTIONS.no_linked_label, orders: 0, lines: 0, amount_inc: null, amount_ex: null, amount_paid: null },
  ],
};

const ORDER_ROW = {
  order_id: 101, order_no: "CG-2026-101", order_date: "2026-10-16",
  source_type: "备件采购", purchaser: "张三", salesperson: "王五",
  customer_id: 12, customer: "客户甲", lines: 2,
  amount_inc: 226, amount_ex: 200, amount_paid: 210,
};

const ordersResp = (over: Record<string, unknown> = {}) => ({
  total: 1, page: 1, page_size: 50, sort: "order_date", order: "desc",
  effective_sort: "order_date", ranking_restricted: false, basis_mode: "order",
  filters_echo: echo({ group_key: "备件采购" }),
  as_of: "2026-10-17 09:00",
  items: [ORDER_ROW],
  ...over,
});

const ORDER_DETAIL = {
  order_id: 101, order_no: "CG-2026-101", order_date: "2026-10-16",
  source_type: "备件采购", purchaser: "张三", data_status: "已生效",
  is_tax_inclusive: true, tax_rate: 0.13, tax_rate_effective: 0.13,
  amount_ex: 200, amount_inc: 226, amount_paid: 210,
  linked_sales_order_no: null, salesperson: "王五", customer_id: 12, customer: "客户甲",
  lines_count: 2,
  lines: [
    {
      line_id: 1, line_no: 1, part_id: 9, pn_std: "ST1000DM003", description: "硬盘 1T",
      brand: "希捷", qty: 2, unit_price: 100, line_amount: 200, category: "硬盘",
      line_amount_inc: 226, line_amount_ex: 200,
    },
    {
      line_id: 2, line_no: 2, part_id: 10, pn_std: "KSM8G", description: "内存 8G",
      brand: "金士顿", qty: 1, unit_price: 100, line_amount: 100, category: "内存",
      line_amount_inc: 113, line_amount_ex: 100,
    },
  ],
};

// ---------------------------------------------------------------- 渲染骨架

let locationSearch = "";
function LocationProbe() {
  const loc = useLocation();
  locationSearch = loc.search;
  return null;
}

function renderPage(initial = "/purchases/analytics") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LocationProbe />
      <PurchaseAnalyticsPage />
    </MemoryRouter>,
  );
}

/** 视口汇总调用（排除客户取值反查的 group_by=customer 探针）。 */
const viewSummaryCalls = () =>
  fetchPurchaseAnalyticsSummary.mock.calls
    .map(([q]) => q as Record<string, unknown>)
    .filter((q) => q.group_by !== "customer");

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "boss");
  localStorage.setItem("permissions", JSON.stringify({
    data_purchase_cost: true, data_customer: true,
  }));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // UTC ISO 钉死时刻（= 上海 2026-10-17 10:00），不随机器时区漂移
  vi.setSystemTime(new Date("2026-10-17T02:00:00Z"));
  fetchPurchaseAnalyticsOptions.mockResolvedValue({ data: OPTIONS });
  fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
    Promise.resolve({ data: q.group_by === "customer" ? SUMMARY_BY_CUSTOMER : SUMMARY }));
  fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp() });
  fetchPurchaseAnalyticsOrderDetail.mockResolvedValue({ data: ORDER_DETAIL });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

// ---------------------------------------------------------------- 用例

describe("采购分析页：URL 单一真值", () => {
  it("默认渲染：零参数 URL，默认今天（date_from = date_to = 今天），采购类型分组", async () => {
    renderPage();
    // 分组表出现 = 汇总已回填
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    expect(viewSummaryCalls()).toEqual([
      { date_from: TODAY, date_to: TODAY, group_by: "source_type" },
    ]);
    expect(locationSearch).toBe("");
    // KPI：单数/行数/三口径并排（默认全选）——KPI 卡与分组列同名列并存，用 role 区分
    expect(screen.getByText("采购单数")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    for (const t of ["含税金额", "不含税金额", "实付金额"]) {
      expect(screen.getAllByText(t).length, t).toBe(2); // KPI 卡 + 分组列各一
    }
    // 名义含税角标
    expect(screen.getByText(/名义含税占比/)).toBeInTheDocument();
  });

  it("「全部」预设不传任何日期参数（后端缺省 = 全部历史）", async () => {
    renderPage("/purchases/analytics?preset=all");
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    const q = viewSummaryCalls()[0];
    expect(q).toEqual({ group_by: "source_type" });
    expect(q).not.toHaveProperty("date_from");
    expect(q).not.toHaveProperty("date_to");
  });

  it("直开完整筛选链接：多选全量传递 + 下钻层带着筛选请求订单列表", async () => {
    renderPage(
      "/purchases/analytics?preset=all"
      + "&source_type=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD,%E6%89%B9%E9%87%8F%E9%87%87%E8%B4%AD"
      + "&customer=12&group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD&sort=amount_paid&dir=asc&page=2",
    );
    // 下钻层直接渲染（URL 即状态）
    expect(await screen.findByRole("button", { name: "← 返回分组" })).toBeInTheDocument();
    await waitFor(() => expect(fetchPurchaseAnalyticsOrders).toHaveBeenCalled());
    expect(fetchPurchaseAnalyticsOrders.mock.calls[0][0]).toMatchObject({
      source_type: ["备件采购", "批量采购"],
      customer_id: [12],
      group_by: "source_type",
      group_key: "备件采购",
      sort: "amount_paid",
      order: "asc",
      page: 2,
    });
    expect(await screen.findByRole("row", { name: /CG-2026-101/ })).toBeInTheDocument();
    // 汇总仍在后台拉取（返回分组时无需重新请求筛选下的汇总）
    const q = viewSummaryCalls()[0];
    expect(q).toMatchObject({ source_type: ["备件采购", "批量采购"], customer_id: [12] });

    // 返回分组：group_key 出栈，回到分组表
    fireEvent.click(screen.getByRole("button", { name: "← 返回分组" }));
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    expect(locationSearch).not.toContain("group_key");
  });

  it("切换口径是纯展示切换：不触发汇总/订单重新请求，只写 URL", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    await waitFor(() => expect(viewSummaryCalls()).toHaveLength(1));

    // 多选去掉「含税」：下拉里点已选项 = 取消选择（jsdom 下比点标签 × 稳）
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "金额口径" }));
    fireEvent.click(
      await screen.findByText("含税", { selector: ".ant-select-item-option-content" }),
    );
    await waitFor(() => expect(locationSearch).toContain("basis=ex%2Cpaid"));
    expect(fetchPurchaseAnalyticsOrders).not.toHaveBeenCalled();
    // 等一轮事件循环确认没有异步补发
    await waitFor(() => expect(viewSummaryCalls()).toHaveLength(1));
    expect(screen.queryAllByText("含税金额")).toHaveLength(0); // KPI 卡与分组列一并消失
    expect(screen.getAllByText("不含税金额").length).toBe(2);
  });

  it("金额不可见账号（data_purchase_cost=false）：不渲染口径控件，金额一律「—」", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_customer: true,
    }));
    fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: q.group_by === "customer" ? SUMMARY_BY_CUSTOMER : SUMMARY_MASKED }));
    renderPage();

    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "金额口径" })).toBeNull();
    // 被遮蔽金额显示「—」而不是 0（KPI 卡 = 含「含税金额」标签且带值区的卡）
    const kpiCards = screen.getAllByText("含税金额").map((el) => el.parentElement!);
    expect(kpiCards.some((card) => card.textContent?.includes("—"))).toBe(true);
    const row = screen.getByRole("row", { name: /备件采购/ });
    expect(row.textContent).toContain("—");
    expect(row.textContent).not.toContain("0.00");
    // 分组排序保护回显：按订单数排序的提示如实展示
    expect(screen.getByText(/分组按订单数排序/)).toBeInTheDocument();
  });

  it("金额不可见账号下钻：金额排序入口预先禁用（不等后端 ranking_restricted 回显）", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_customer: true,
    }));
    // 后端默认 sort=order_date 时 ranking_restricted=false——只有本地预判才能禁住入口；
    // 金额也按脱敏形态置 null（与真实响应一致）
    fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp({
      items: [{ ...ORDER_ROW, amount_inc: null, amount_ex: null, amount_paid: null }],
    }) });
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");

    expect(await screen.findByRole("row", { name: /CG-2026-101/ })).toBeInTheDocument();
    expect(screen.getByText(/已按采购日期排序/)).toBeInTheDocument();
    const thAmount = screen.getByRole("columnheader", { name: "含税金额" }).closest("th");
    const thDate = screen.getByRole("columnheader", { name: "采购日期" }).closest("th");
    expect(thAmount?.className).not.toContain("ant-table-column-has-sorters");
    expect(thDate?.className).toContain("ant-table-column-has-sorters");
    // 点金额表头：不发出注定被回退的请求
    fireEvent.click(screen.getByRole("columnheader", { name: "含税金额" }));
    await waitFor(() => expect(fetchPurchaseAnalyticsOrders).toHaveBeenCalledTimes(1));
  });

  it("客户不可见账号（data_customer=false）：客户筛选禁用、不做取值反查、URL 里的客户维度整体收敛（含下钻层）", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: true, data_customer: false,
    }));
    // 分享链接来自权限完整账号的「客户分组 → 下钻客户 12」：受限账号打开后
    // 客户维度连同其 group_key/order_id 一并收敛，不能出现「采购类型：12 · 共 0 单」
    renderPage("/purchases/analytics?group_by=customer&customer=12&group_key=12&order_id=101");

    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    const customerBox = screen.getByRole("combobox", { name: "客户" });
    expect(customerBox).toBeDisabled();
    // 客户下拉不显示残留选中态，按占位文案提示不可见
    expect(customerBox.closest(".ant-select")?.textContent).not.toContain("12");
    // 没有任何 group_by=customer 请求（取值反查被跳过；视图被收敛回 source_type）
    expect(
      fetchPurchaseAnalyticsSummary.mock.calls.some(([q]) => (q as Record<string, unknown>).group_by === "customer"),
    ).toBe(false);
    // 客户筛选从请求里剔除（带 customer=12 的链接不会打出 403 循环）
    expect(viewSummaryCalls()[0]).toEqual({
      date_from: TODAY, date_to: TODAY, group_by: "source_type",
    });
    // 下钻层连同抽屉一并收敛：不打订单/明细请求，直接回分组视图
    expect(fetchPurchaseAnalyticsOrders).not.toHaveBeenCalled();
    expect(fetchPurchaseAnalyticsOrderDetail).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "← 返回分组" })).toBeNull();
    // 分组维度控件显示收敛后的维度（与表格同源，不自相矛盾）
    expect(screen.getByRole("combobox", { name: "分组维度" }).closest(".ant-select")?.textContent)
      .toContain("采购类型");
    // 分组维度下拉里「客户」不可选
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "分组维度" }));
    const option = await screen.findByText("客户", { selector: ".ant-select-item-option-content" });
    expect(option.closest(".ant-select-item-option")).toHaveClass("ant-select-item-option-disabled");
  });

  it("金额可见账号：客户筛选取值经客户分组反查（排除「无关联销售单」哨兵）", async () => {
    renderPage();
    await screen.findByRole("row", { name: /备件采购/ });
    await waitFor(() =>
      expect(fetchPurchaseAnalyticsSummary.mock.calls.some(([q]) => (q as Record<string, unknown>).group_by === "customer"))
        .toBe(true));
    // 取值反查不带视图筛选（全历史客户清单）
    const probe = fetchPurchaseAnalyticsSummary.mock.calls
      .map(([q]) => q as Record<string, unknown>)
      .find((q) => q.group_by === "customer");
    expect(probe).toEqual({ group_by: "customer" });
  });

  it("排序保护（ranking_restricted）：金额列不可排、如实提示已按日期排序，点金额表头不重新请求", async () => {
    fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp({ ranking_restricted: true }) });
    // 初始 sort=amount_inc&dir=asc：后端回退按日期排（effective_sort），URL 状态原样保留
    renderPage(
      "/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD&sort=amount_inc&dir=asc",
    );
    expect(await screen.findByText(/已按采购日期排序/)).toBeInTheDocument();

    // 注意用全名匹配：/含税金额/ 会连「不含税金额」一起匹配上
    const thAmount = screen.getByRole("columnheader", { name: "含税金额" }).closest("th");
    const thDate = screen.getByRole("columnheader", { name: "采购日期" }).closest("th");
    expect(thDate?.className).toContain("ant-table-column-has-sorters");
    expect(thAmount?.className).not.toContain("ant-table-column-has-sorters");

    await waitFor(() => expect(fetchPurchaseAnalyticsOrders).toHaveBeenCalledTimes(1));
    // 点金额表头：无排序器，不触发请求
    fireEvent.click(screen.getByRole("columnheader", { name: "含税金额" }));
    await waitFor(() => expect(fetchPurchaseAnalyticsOrders).toHaveBeenCalledTimes(1));
    // 日期排序仍可用（当前非日期列受控 → 点击进入 ascend，状态与 URL 同步）
    fireEvent.click(screen.getByRole("columnheader", { name: /采购日期/ }));
    await waitFor(() => expect(fetchPurchaseAnalyticsOrders).toHaveBeenCalledTimes(2));
    // order_date 是默认排序键 → 不落 URL；asc 非默认 → 落 URL
    expect(locationSearch).not.toContain("sort=");
    expect(locationSearch).toContain("dir=asc");
    expect(fetchPurchaseAnalyticsOrders.mock.calls[1][0]).toMatchObject({
      sort: "order_date", order: "asc",
    });
  });

  it("订单行点击打开明细抽屉：无分类时 category 为空，抽屉展示单头口径与行级金额", async () => {
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    fireEvent.click(row);

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/CG-2026-101/)).toBeInTheDocument());
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledWith(101, []);
    // 单头三口径 + 单头批次行说明
    expect(within(dialog).getByText(/只计单头所属批次/)).toBeInTheDocument();
    expect(within(dialog).getAllByText(/ST1000DM003/).length).toBeGreaterThan(0);
    expect(within(dialog).getByRole("columnheader", { name: "行金额（实付）" })).toBeInTheDocument();
    // 分类未筛：不出现「匹配分类行合计」
    expect(within(dialog).queryByText(/匹配分类行合计/)).toBeNull();
  });

  it("分类筛选活跃时明细抽屉带 category，展示匹配分类行合计", async () => {
    fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp({ basis_mode: "line" }) });
    fetchPurchaseAnalyticsOrderDetail.mockResolvedValue({
      data: {
        ...ORDER_DETAIL,
        category_matched: { lines: 1, amount_inc: 226, amount_ex: 200, amount_paid: 200 },
      },
    });
    renderPage("/purchases/analytics?group_key=%E7%A1%AC%E7%9B%98&category=%E7%A1%AC%E7%9B%98");
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    fireEvent.click(row);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/匹配分类行合计/)).toBeInTheDocument());
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledWith(101, ["硬盘"]);
    // 分类口径（行级）说明
    expect(screen.getByText(/分类口径：订单已拆到明细行/)).toBeInTheDocument();
  });

  it("分类分组下钻、未筛分类：抽屉按所在行分类过滤（category=[group_key]）", async () => {
    fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp({ basis_mode: "line" }) });
    fetchPurchaseAnalyticsOrderDetail.mockResolvedValue({
      data: {
        ...ORDER_DETAIL,
        category_matched: { lines: 1, amount_inc: 226, amount_ex: 200, amount_paid: 200 },
      },
    });
    renderPage("/purchases/analytics?group_by=category&group_key=%E7%A1%AC%E7%9B%98");
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    fireEvent.click(row);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/匹配分类行合计（硬盘）/)).toBeInTheDocument());
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledWith(101, ["硬盘"]);
  });

  it("分类分组下钻、多选分类筛选：抽屉只带所在行分类，不带整组多选", async () => {
    fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp({ basis_mode: "line" }) });
    renderPage(
      "/purchases/analytics?group_by=category&group_key=%E7%A1%AC%E7%9B%98"
      + "&category=%E7%A1%AC%E7%9B%98,%E5%86%85%E5%AD%98",
    );
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    fireEvent.click(row);
    await screen.findByRole("dialog");
    await waitFor(() => expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalled());
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenLastCalledWith(101, ["硬盘"]);
    expect(fetchPurchaseAnalyticsOrderDetail).not.toHaveBeenCalledWith(101, ["硬盘", "内存"]);
  });

  it("非分类分组下钻 + 多选分类筛选：抽屉沿用筛选多选", async () => {
    fetchPurchaseAnalyticsOrders.mockResolvedValue({ data: ordersResp({ basis_mode: "line" }) });
    renderPage(
      "/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD"
      + "&category=%E7%A1%AC%E7%9B%98,%E5%86%85%E5%AD%98",
    );
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    fireEvent.click(row);
    await screen.findByRole("dialog");
    await waitFor(() => expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalled());
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenLastCalledWith(101, ["硬盘", "内存"]);
  });
});

describe("采购分析页：健壮性", () => {
  it("汇总 403（detail 透传）：错误 Alert 展示后端 detail，重试成功后恢复", async () => {
    fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
      q.group_by === "customer"
        ? Promise.resolve({ data: SUMMARY_BY_CUSTOMER })
        : Promise.reject({ response: { status: 403, data: { detail: "仅老板与管理员可访问" } } }));
    renderPage();

    expect(await screen.findByText("汇总加载失败")).toBeInTheDocument();
    expect(screen.getByText("仅老板与管理员可访问")).toBeInTheDocument();

    // 重试：恢复成正常实现
    fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: q.group_by === "customer" ? SUMMARY_BY_CUSTOMER : SUMMARY }));
    fireEvent.click(screen.getByText("重试"));
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    expect(screen.queryByText("汇总加载失败")).toBeNull();
  });

  it("订单列表 422：回退文案提示参数非法（不吞错）", async () => {
    fetchPurchaseAnalyticsOrders.mockRejectedValue({ response: { status: 422 } });
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    expect(await screen.findByText("订单列表加载失败")).toBeInTheDocument();
    expect(screen.getByText("请求参数非法（422）")).toBeInTheDocument();
  });

  it("空数据：分组表给出空态文案（不是报错）", async () => {
    fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({
        data: q.group_by === "customer"
          ? SUMMARY_BY_CUSTOMER
          : { ...SUMMARY, groups: [], kpi: { orders: 0, lines: 0, amount_inc: null, amount_ex: null, amount_paid: null, nominal_inc_share: null } },
      }));
    renderPage("/purchases/analytics?preset=all");
    expect(await screen.findByText("当前筛选下没有采购数据")).toBeInTheDocument();
    // KPI 空态金额为「—」，计数为 0（不把 null 当 0 金额）
    const kpiCards = screen.getAllByText("含税金额").map((el) => el.parentElement!);
    expect(kpiCards.some((card) => card.textContent?.includes("—"))).toBe(true);
  });
});
