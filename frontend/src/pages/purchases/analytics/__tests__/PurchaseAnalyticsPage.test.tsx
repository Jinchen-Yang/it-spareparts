// 采购分析页集成（D-39 PR-4a）：URL 单一真值 —— 默认今天零参数、直开链接恢复筛选与
// 下钻层、切换口径不重新请求、脱敏账号（金额/客户）控件门控与「—」占位、
// 排序保护回显（ranking_restricted 禁用金额排序）、错误与空态。
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../../test/antdRender";
import { MemoryRouter, useLocation, useNavigationType, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_EXPIRED_MESSAGE } from "../../../../utils/session";

const tagLayout = vi.hoisted(() => ({ showCustomerTags: false }));
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  const { createElement, forwardRef } = await import("react");
  return {
    ...actual,
    // jsdom没有宽度，responsive标签不会进入DOM。仅标签回归使用固定可见数，
    // 仍运行真实Select/useCache，不用DOM布局替身冒充客户名称缓存。
    Select: forwardRef<import("react").ComponentRef<typeof actual.Select>, Record<string, unknown>>((props, ref) => createElement(actual.Select, {
      ...props,
      ref,
      ...(tagLayout.showCustomerTags && props["aria-label"] === "客户" ? { maxTagCount: 1 } : {}),
    })),
  };
});

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
// jsdom 无 canvas：mock 图表壳，只保留空态文案；带 onEvents.click 的图渲染一个
// 「模拟点击」按钮（点击第 0 个类目），趋势柱 / 排行横条的下钻可在页面级测试触发。
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

import PurchaseAnalyticsPage from "../PurchaseAnalyticsPage";

// ---------------------------------------------------------------- 假数据（对齐 PR-3a 响应形态）

const TODAY = "2026-10-17";

const OPTIONS = {
  customers: [{ customer_id: 12, customer: "客户甲" }, { customer_id: 30, customer: "客户乙" }],
  customers_has_more: false,
  selected_customers: [],
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
    nominal_inc_share: 0.2, nominal_ex_share: 0.15,
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
    amount_inc: null, amount_ex: null, amount_paid: null,
    nominal_inc_share: null, nominal_ex_share: null,
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
  nominal_side: null, // 已标税率（0.13）：两侧都非名义
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
  amount_ex: 200, amount_inc: 226, amount_paid: 210, nominal_side: null,
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
let navigationType = "";
function LocationProbe() {
  const loc = useLocation();
  locationSearch = loc.search;
  navigationType = useNavigationType();
  const navigate = useNavigate();
  return <button onClick={() => navigate(-1)}>浏览器后退</button>;
}

function renderPage(initial = "/purchases/analytics") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LocationProbe />
      <PurchaseAnalyticsPage />
    </MemoryRouter>,
  );
}

/** 视口汇总调用（排除客户取值反查的 group_by=customer 探针与趋势图的
 * group_by=day/month 二次请求——Q-A-01 图表与表格同端点不同分组）。 */
const viewSummaryCalls = () =>
  fetchPurchaseAnalyticsSummary.mock.calls
    .map(([q]) => q as Record<string, unknown>)
    .filter((q) => !["customer", "day", "month"].includes(q.group_by as string));

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
  tagLayout.showCustomerTags = false;
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
    // 名义占比角标按侧分开（D-39 名义侧裁定）：含税/不含税 KPI 各自的推算占比
    expect(screen.getByText(/名义含税占比/)).toBeInTheDocument();
    expect(screen.getByText(/名义不含税占比/)).toBeInTheDocument();
  });

  it("名义侧说明按实际推算方向表述，包含历史缺口翻转", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    for (const side of ["含税", "不含税"]) {
      const note = screen.getByLabelText(`名义${side}说明`);
      fireEvent.mouseOver(note);
      expect(await screen.findByText(
        `未标税率（或税率为 0）时，名义值是实际按 13% 推算出来的那一侧；名义${side}占比 = 实际推算的${side}额之和 ÷ ${side}合计。`,
      )).toBeInTheDocument();
      fireEvent.mouseLeave(note);
      const columnTitles = screen.getAllByText(`${side}金额`);
      const columnTitle = columnTitles[columnTitles.length - 1];
      fireEvent.mouseOver(columnTitle);
      expect(await screen.findByText(
        `未标税率（或税率为 0）时，只有实际按 13% 推算出来的${side}侧才是名义值。`,
      )).toBeInTheDocument();
      fireEvent.mouseLeave(columnTitle);
    }
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
    await waitFor(() => expect(new URLSearchParams(locationSearch).getAll("basis")).toEqual(["ex", "paid"]));
    expect(navigationType).toBe("REPLACE");
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
    // 名义占比随金额一起被遮蔽：null 不渲染角标（不显示 0.00%）
    expect(screen.queryByText(/名义含税占比/)).toBeNull();
    expect(screen.queryByText(/名义不含税占比/)).toBeNull();
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

  it("客户候选使用 options 并支持服务端名称搜索，不请求全历史客户金额汇总", async () => {
    renderPage();
    await screen.findByRole("row", { name: /备件采购/ });
    expect(fetchPurchaseAnalyticsOptions).toHaveBeenCalledWith("", []);
    expect(fetchPurchaseAnalyticsSummary.mock.calls.some(([q]) => q.group_by === "customer")).toBe(false);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "客户" }));
    expect(await screen.findByText("客户甲", { selector: ".ant-select-item-option-content" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "客户" }), { target: { value: "客户乙" } });
    await waitFor(() => expect(fetchPurchaseAnalyticsOptions).toHaveBeenLastCalledWith("客户乙", []));
  });

  it("分享链接的已选客户不在首批候选时通过 selected_customers 恢复名称", async () => {
    tagLayout.showCustomerTags = true;
    fetchPurchaseAnalyticsOptions.mockResolvedValue({ data: {
      ...OPTIONS, customers: [OPTIONS.customers[0]],
      selected_customers: [{ customer_id: 999, customer: "首批之外客户" }],
    } });
    renderPage("/purchases/analytics?customer=999");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "客户" }).closest(".ant-select"))
      .toHaveTextContent("首批之外客户"));
    expect(fetchPurchaseAnalyticsOptions).toHaveBeenCalledWith("", [999]);
  });

  it("已选客户在远程搜索返回其他候选后仍显示原客户名称", async () => {
    tagLayout.showCustomerTags = true;
    fetchPurchaseAnalyticsOptions.mockImplementation((query: string) => Promise.resolve({
      data: query ? { ...OPTIONS, customers: [OPTIONS.customers[1]] } : OPTIONS,
    }));
    renderPage("/purchases/analytics?customer=12");
    const input = screen.getByRole("combobox", { name: "客户" });
    const select = input.closest(".ant-select")!;
    await waitFor(() => expect(select.querySelector(".ant-select-selection-item-content"))
      .toHaveTextContent("客户甲"));
    fireEvent.mouseDown(input);
    fireEvent.change(input, { target: { value: "客户乙" } });
    await waitFor(() => expect(fetchPurchaseAnalyticsOptions).toHaveBeenLastCalledWith("客户乙", [12]));
    await waitFor(() => expect(screen.queryByText("客户甲", { selector: ".ant-select-item-option-content" })).toBeNull());
    expect(select.querySelector(".ant-select-selection-item-content")).toHaveTextContent("客户甲");
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
    expect(navigationType).toBe("REPLACE");
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
    // 已标税率（0.13）：两侧都非名义，不出现名义标注
    expect(within(dialog).queryByText(/名义（按 13% 推算）/)).toBeNull();
  });

  it("明细抽屉：未标税率单据只给名义侧金额标注（含税单→不含税侧，D-39 名义侧裁定）", async () => {
    // 含税单 + 0 税率：录入含税额真实，推算出的不含税额是名义 → 标注只在不含税金额旁
    fetchPurchaseAnalyticsOrderDetail.mockResolvedValue({
      data: { ...ORDER_DETAIL, tax_rate: 0, tax_rate_effective: 0.13, nominal_side: "ex" },
    });
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    fireEvent.click(await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/未标税率/)).toBeInTheDocument());
    const badges = within(within(dialog).getByText("不含税金额")).getAllByText(/名义（按 13% 推算）/);
    expect(badges).toHaveLength(1);
    // 标注挂在不含税金额那一项上（「不含税金额」串含「含税金额」子串，用前缀断言）
    expect(badges[0].parentElement?.textContent).toMatch(/^不含税金额/);
  });

  it("明细抽屉：非含税单 NULL 税率 → 名义标注在含税金额旁（含税标记未标注同规则）", async () => {
    fetchPurchaseAnalyticsOrderDetail.mockResolvedValue({
      data: {
        ...ORDER_DETAIL,
        is_tax_inclusive: null, tax_rate: null, tax_rate_effective: 0.13,
        nominal_side: "inc",
      },
    });
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    fireEvent.click(await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/未标税率/)).toBeInTheDocument());
    const badges = within(within(dialog).getByText("含税金额")).getAllByText(/名义（按 13% 推算）/);
    expect(badges).toHaveLength(1);
    expect(badges[0].parentElement?.textContent).toMatch(/^含税金额/);
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
          : { ...SUMMARY, groups: [], kpi: { orders: 0, lines: 0, amount_inc: null, amount_ex: null, amount_paid: null, nominal_inc_share: null, nominal_ex_share: null } },
      }));
    renderPage("/purchases/analytics?preset=all");
    expect(await screen.findByText("当前筛选下没有采购数据")).toBeInTheDocument();
    // KPI 空态金额为「—」，计数为 0（不把 null 当 0 金额）
    const kpiCards = screen.getAllByText("含税金额").map((el) => el.parentElement!);
    expect(kpiCards.some((card) => card.textContent?.includes("—"))).toBe(true);
  });
});

describe("URL 导航与每页条数", () => {
  it("打开抽屉创建历史，浏览器后退恢复原筛选与每页条数", async () => {
    renderPage("/purchases/analytics?group_key=备件采购&page_size=100");
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    expect(fetchPurchaseAnalyticsOrders.mock.calls[0][0]).toMatchObject({ page_size: 100 });
    fireEvent.click(row);
    await waitFor(() => expect(new URLSearchParams(locationSearch).get("order_id")).toBe("101"));
    expect(navigationType).toBe("PUSH");
    fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() => expect(new URLSearchParams(locationSearch).has("order_id")).toBe(false));
    expect(new URLSearchParams(locationSearch).get("page_size")).toBe("100");
    expect(navigationType).toBe("POP");
  });
  it("分组行下钻 push，浏览器后退回到分组", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("row", { name: "查看分组 备件采购 的订单列表" }));
    await waitFor(() => expect(new URLSearchParams(locationSearch).get("group_key")).toBe("备件采购"));
    expect(navigationType).toBe("PUSH");
    fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
    await waitFor(() => expect(new URLSearchParams(locationSearch).has("group_key")).toBe(false));
    expect(navigationType).toBe("POP");
  });
});

describe("图表层（Q-A-01 / D-39 补图表）", () => {
  /** 按日分组的趋势响应：日期键 + 「(空)」无日期桶（恒排末端）。 */
  const DAY_SUMMARY = {
    ...SUMMARY,
    filters_echo: echo({ group_by: "day" }),
    groups: [
      { key: "(空)", label: null, orders: 1, lines: 1, amount_inc: 100, amount_ex: 90, amount_paid: 95 },
      { key: TODAY, label: null, orders: 4, lines: 11, amount_inc: 1030, amount_ex: 910, amount_paid: 955 },
    ],
  };

  it("按日表格与趋势同筛选时共享在途读取，重挂载仍重新读取", async () => {
    let resolve!: (value: { data: typeof DAY_SUMMARY }) => void;
    fetchPurchaseAnalyticsSummary.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const first = renderPage("/purchases/analytics?group_by=day");
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(1);
    await act(async () => { resolve({ data: DAY_SUMMARY }); });
    expect(await screen.findByRole("button", { name: /模拟点击采购趋势/ })).toBeInTheDocument();
    expect(screen.getByText("采购单数")).toBeInTheDocument();
    first.unmount();
    renderPage("/purchases/analytics?group_by=day");
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(2);
    await act(async () => { resolve({ data: DAY_SUMMARY }); });
  });

  it("合并读取失败后重试发起一次新请求，不复用失败结果", async () => {
    fetchPurchaseAnalyticsSummary.mockRejectedValueOnce(new Error("offline"));
    renderPage("/purchases/analytics?group_by=day");
    const retry = (await screen.findAllByRole("button", { name: /重试/ }))[0];
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(1);
    fetchPurchaseAnalyticsSummary.mockResolvedValue({ data: DAY_SUMMARY });
    fireEvent.click(retry);
    expect(await screen.findByRole("button", { name: /模拟点击采购趋势/ })).toBeInTheDocument();
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(2);
  });

  it("合并的在途读取换账号后同时丢弃，不把共享结果带到新会话", async () => {
    localStorage.setItem("token", "account-a");
    let resolve!: (value: { data: typeof DAY_SUMMARY }) => void;
    fetchPurchaseAnalyticsSummary.mockImplementation(() => new Promise((done) => { resolve = done; }));
    renderPage("/purchases/analytics?group_by=day");
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(1);
    localStorage.setItem("token", "account-b");
    await act(async () => { resolve({ data: DAY_SUMMARY }); });
    expect(await screen.findByText(SESSION_EXPIRED_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /模拟点击采购趋势/ })).toBeNull();
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(1);
  });

  it("默认今天（1 天）趋势按日：同端点第二次请求 group_by=day；切按月再请求 group_by=month", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    await waitFor(() => expect(fetchPurchaseAnalyticsSummary.mock.calls.some(
      ([q]) => (q as Record<string, unknown>).group_by === "day"
        && (q as Record<string, unknown>).date_from === TODAY
        && (q as Record<string, unknown>).date_to === TODAY,
    )).toBe(true));
    // 手动切按月：趋势层按月再拉（表格主请求不受影响）
    fireEvent.click(await screen.findByRole("radio", { name: "按月" }));
    await waitFor(() => expect(fetchPurchaseAnalyticsSummary.mock.calls.some(
      ([q]) => (q as Record<string, unknown>).group_by === "month",
    )).toBe(true));
    expect(viewSummaryCalls()).toHaveLength(1);
  });

  it("趋势柱点击下钻：group_by=day + group_key 进 URL（可分享可后退），订单列表带同一 drill", async () => {
    fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: q.group_by === "customer" ? SUMMARY_BY_CUSTOMER
        : q.group_by === "day" ? DAY_SUMMARY : SUMMARY }));
    renderPage();
    // (空) 恒排末端：首桶是今天
    const trigger = await screen.findByRole("button", { name: /模拟点击采购趋势/ });
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(new URLSearchParams(locationSearch).get("group_by")).toBe("day");
      expect(new URLSearchParams(locationSearch).get("group_key")).toBe(TODAY);
    });
    expect(navigationType).toBe("PUSH");
    await waitFor(() => expect(fetchPurchaseAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      group_by: "day", group_key: TODAY,
    }));
  });

  it("排行横条点击下钻：与表格行点击同一语义（group_key=分组键）", async () => {
    renderPage();
    const trigger = await screen.findByRole("button", { name: /模拟点击采购分组排行/ });
    fireEvent.click(trigger);
    await waitFor(() => expect(new URLSearchParams(locationSearch).get("group_key")).toBe("备件采购"));
    await waitFor(() => expect(fetchPurchaseAnalyticsOrders.mock.lastCall?.[0]).toMatchObject({
      group_by: "source_type", group_key: "备件采购",
    }));
  });

  it("金额不可见账号：图表指标只剩单数，三金额选项不出现（后端金额全 null）", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      data_purchase_cost: false, data_customer: true,
    }));
    fetchPurchaseAnalyticsSummary.mockImplementation((q: Record<string, unknown>) =>
      Promise.resolve({ data: q.group_by === "customer" ? SUMMARY_BY_CUSTOMER : SUMMARY_MASKED }));
    renderPage();
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    // AntD 按钮式 Radio 的文案在包裹 label 上（input 的 textContent 恒空）
    const radios = screen.getAllByRole("radio").map((r) => r.closest("label")?.textContent ?? "");
    expect(radios).toContain("单数");
    expect(radios).not.toContain("含税");
    expect(radios).not.toContain("实付");
  });
});

describe("DL-010 所属会话守卫（返工：静默换 token 后请求前拒发）", () => {
  // 与销售页同款返工：owner 是挂载时 token；静默换 token（无重渲染、无 storage 事件）
  // 后，旧会话的趋势粒度切换 / 抽屉取数一律拒发并清旧上下文。每个用例先走同会话
  // 正向路径，证明守卫不是「全部禁用」的伪通过。
  const SESSION_DEAD_TEXT = "登录会话已变更（已切换账号或登出），请刷新页面后继续";

  it("同会话切趋势粒度即重拉；静默换 token 后再切粒度拒发、旧上下文让位", async () => {
    localStorage.setItem("token", "account-a");
    renderPage();
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    // 首帧趋势已按日拉过；正向：同会话切按月 → 趋势层按月再拉
    fireEvent.click(await screen.findByRole("radio", { name: "按月" }));
    await waitFor(() => expect(fetchPurchaseAnalyticsSummary.mock.calls.some(
      ([q]) => (q as Record<string, unknown>).group_by === "month",
    )).toBe(true));
    const total = fetchPurchaseAnalyticsSummary.mock.calls.length;

    // 静默换 token：切回按日 → 拒发（请求数不增），旧汇总/列表让位给会话失效提示
    localStorage.setItem("token", "account-b");
    fireEvent.click(screen.getByRole("radio", { name: "按日" }));
    expect(fetchPurchaseAnalyticsSummary.mock.calls.length).toBe(total);
    expect(screen.getByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /备件采购/ })).toBeNull();
  });

  it("同会话可开采购单明细抽屉；静默换 token 后重开抽屉拒发", async () => {
    localStorage.setItem("token", "account-a");
    // 分组下钻层才有订单行（默认层是分组表）
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    // 正向：同会话点行 → 明细请求发出、抽屉渲染旧会话可见的详情
    fireEvent.click(row);
    await waitFor(() => expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("采购单 CG-2026-101")).toBeInTheDocument();

    // 关抽屉 → 静默换 token → 重开：旧会话的抽屉不以当前账号取数
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    localStorage.setItem("token", "account-b");
    fireEvent.click(screen.getByRole("row", { name: "查看采购单 CG-2026-101 明细" }));
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledTimes(1);
    // 抽屉内就地提示会话失效（失败/未知/无权/会话失效分开），不显示旧会话详情；
    // 行点击入口同时通知页级清旧上下文——主页面订单列表也让位（两处文案并存）
    expect((await screen.findAllByText(SESSION_DEAD_TEXT)).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("采购单 CG-2026-101")).toBeNull();
    expect(screen.queryByRole("row", { name: "查看采购单 CG-2026-101 明细" })).toBeNull();
  });

  // ---------------- 落地矩阵（DL-010 本轮返工）：迟到响应不只「静默不落地」，
  // 检测到过期的落地分支必须通知页级清旧上下文；全程不得发出新（B 会话）请求 ----------------

  it("落地矩阵：候选/汇总/趋势/订单全在途换 token，迟到落地通知页级清空，无新请求", async () => {
    localStorage.setItem("token", "account-a");
    const resolvers: Record<string, (v: unknown) => void> = {};
    const defer = (k: string) => () => new Promise((res) => { resolvers[k] = res; });
    fetchPurchaseAnalyticsOptions.mockImplementation(defer("options"));
    fetchPurchaseAnalyticsSummary.mockImplementation(defer("summary"));
    fetchPurchaseAnalyticsOrders.mockImplementation(defer("orders"));
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    await waitFor(() => expect(resolvers.orders).toBeDefined());

    localStorage.setItem("token", "account-b");
    await act(async () => {
      resolvers.options({ data: OPTIONS });
      resolvers.summary({ data: SUMMARY });
      resolvers.orders({ data: ordersResp() });
      await Promise.resolve();
    });

    // 迟到响应全部不落地，且各自通知页级：数据面让位给会话失效提示
    expect(await screen.findByText(SESSION_DEAD_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /备件采购/ })).toBeNull();
    expect(screen.queryByText("采购单 CG-2026-101")).toBeNull();
    expect(fetchPurchaseAnalyticsOptions).toHaveBeenCalledTimes(1);
    expect(fetchPurchaseAnalyticsOrders).toHaveBeenCalledTimes(1);
    // 汇总端点：主汇总 + 趋势（day）各一次，之后不再重发
    expect(fetchPurchaseAnalyticsSummary).toHaveBeenCalledTimes(2);
  });

  it("落地矩阵：明细抽屉在途换 token，迟到详情不落地并通知页级清主页面", async () => {
    localStorage.setItem("token", "account-a");
    let resolveDetail!: (v: { data: typeof ORDER_DETAIL }) => void;
    fetchPurchaseAnalyticsOrderDetail.mockImplementationOnce(
      () => new Promise((res) => { resolveDetail = res; }),
    );
    renderPage("/purchases/analytics?group_key=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    const row = await screen.findByRole("row", { name: "查看采购单 CG-2026-101 明细" });
    fireEvent.click(row);
    await waitFor(() => expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledTimes(1));

    localStorage.setItem("token", "account-b");
    await act(async () => {
      resolveDetail({ data: ORDER_DETAIL });
      await Promise.resolve();
    });
    // 迟到详情不进抽屉，并通知页级：主页面分组/列表同样让位
    expect(await screen.findAllByText(SESSION_DEAD_TEXT)).not.toHaveLength(0);
    expect(screen.queryByText("采购单 CG-2026-101")).toBeNull();
    expect(fetchPurchaseAnalyticsOrderDetail).toHaveBeenCalledTimes(1);
  });

  // ---------------- 图表卡几何（320/390 视口）：卡片可收缩不撑出横向滚动，
  // 趋势卡头部允许换行（标题不被 extra 单选组挤到 0 宽） ----------------

  it("图表卡：趋势/排行卡可收缩（minWidth 0 + maxWidth 100%），趋势筛选控件独立于标题行", async () => {
    renderPage();
    expect(await screen.findByRole("row", { name: /备件采购/ })).toBeInTheDocument();
    const trendCard = screen.getByText("采购趋势").closest(".ant-card") as HTMLElement;
    const rankCard = screen.getByText(/分组排行（采购类型）/).closest(".ant-card") as HTMLElement;
    // jsdom 对无单位 0 的 computed minWidth 返回 "0"（非 "0px"）
    expect(trendCard).toHaveStyle({ minWidth: "0", maxWidth: "100%" });
    expect(rankCard).toHaveStyle({ minWidth: "0", maxWidth: "100%" });
    const header = trendCard.querySelector(".ant-card-head")!;
    const body = trendCard.querySelector(".ant-card-body")!;
    expect(header).toHaveTextContent("采购趋势");
    expect(header.querySelector(".ant-card-extra")).toBeNull();
    expect(body.querySelector('[aria-label="趋势粒度"]')).not.toBeNull();
    expect(body.querySelector('[aria-label="图表指标"]')).not.toBeNull();
  });
});
