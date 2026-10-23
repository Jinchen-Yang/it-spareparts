// A3 独立 Chromium 对比度失败的返工回归（DL-025）：受限账号（data_purchase_cost /
// data_customer / data_profit 全 false，对齐独立证据 restricted profile）下——
// 1) 金额区间禁用口径「不含税」与 InputNumber「不限」值/占位、2) 分组维度弹层里
// 禁用的客户类选项、3) FilterBar 禁用客户控件与各 Select 占位——均经看板局部
// 主题（dashboardColors.analyticsControlTheme）取 --mb-text-2（含 fallback），
// 不再继承全站 colorTextQuaternary（rgb(182,176,166)，真实 disabled 底 1.97:1 /
// 白底 2.15:1）。
//
// jsdom 不能解析 var() 的 computed 值，这里断言的是「控件实际命中的 cssinjs 样式
// 块（按元素携带的 hashId 对应）包含覆盖色」，即规则真的生成在该 token 集下；
// 弹层（portal 渲染进 document.body）与触发控件同 hashId，同一断言覆盖。真实
// 浏览器 computed 复验由 Codex 在新冻结 SHA 执行，本文件不冒称真实 computed 通过。
// 本文件不写十六进制色值（tests/colorGuard 只减不增约束）。
import { cleanup, fireEvent, render, screen, waitFor } from "../../../../test/antdRender";
import { parseColor } from "../../../../utils/contrast";
import { MemoryRouter } from "react-router-dom";
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
// jsdom 无 canvas：图表壳 mock（不影响 Select / 输入框 / 弹层真实性）
vi.mock("../../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText, ariaLabel }: { empty?: boolean; emptyText?: string; ariaLabel?: string }) =>
    empty ? <div>{emptyText ?? "空"}</div> : <div data-testid="chart" aria-label={ariaLabel} />,
}));

vi.mock("../../../../authz/AuthzProvider", () => ({
  useAuthz: () => ({ status: "ready", snapshot: { version: "legacy", can_manage: false,
    sales_analysis: { enabled: false, read: false, export: false, scope: "none", customer_count: 0, reason: "legacy", field_permissions: [] } } }),
  AuthzUnavailable: () => <div>权限尚未确认</div>,
}));

import SalesAnalyticsPage from "../SalesAnalyticsPage";

const TODAY = "2026-10-17";
const KPI = {
  orders: 5, amount_ex_tax_total: 1000, amount_inc_tax_total: 1130, amount_inc_tax_missing: 0,
  revenue_total: 800, customers: 3, salespeople: 2, revenue_costed: 700, gross_profit: 210,
  gross_margin: 0.3, cost_coverage: 0.875, new_customers: 1, returning_customers: 2,
};
const GROUPS = {
  group_by: "customer", line_level: false, sort: "amount_ex_tax", order: "desc",
  amount_basis: "amount_ex_tax", top: 20, total_groups: 2,
  groups: [
    { key: "A公司", is_empty: false, orders: 3, customers: 1, amount_ex_tax: 700, amount_inc_tax: 791,
      revenue: 600, gross_profit: 180, gross_margin: 0.3, avg_order: 233.33, share: 0.7, cumulative_share: 0.7 },
  ],
  other: null,
  concentration: { top1_share: 0.7, top5_share: 1, top10_share: 1 },
};

function renderPage(initial = "/sales/analytics") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <SalesAnalyticsPage />
    </MemoryRouter>,
  );
}

/** 元素携带的 cssinjs hashId（dev/test 环境 css-dev-only-do-not-override-*；生产 css-*）。 */
const hashOf = (el: Element | null): string | null =>
  el?.className.match(/\bcss-(?:dev-only-do-not-override-)?[a-z0-9]+\b/)?.[0] ?? null;

/** 该 hashId 命中的全部生成样式（cssinjs 按组件分块，同一 token 集多块）。 */
const themedCss = (hash: string): string =>
  [...document.querySelectorAll("style")]
    .map((s) => s.textContent ?? "")
    .filter((t) => t.includes(hash))
    .join("\n");

const VAR = "var(--mb-text-2, rgb(107, 102, 94))";
const HOVER_VAR = "var(--mb-accent-strong, rgb(44, 86, 174))";
// colorPrimary（seed token）覆盖用字面量：var() 会被 antd 默认算法调色板派生
// 污染成纯黑，字面量与 --mb-accent-strong token / fallback 同色
const HOVER_LITERAL = "rgb(44, 86, 174)";

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "boss");
  // 独立证据 restricted profile：三项数据权限全 false（金额区间整组禁用 +
  // 客户类控件禁用 + 分组维度弹层客户类选项禁用）
  localStorage.setItem("permissions", JSON.stringify({
    data_purchase_cost: false, data_reconciliation: true, data_customer: false, data_profit: false,
  }));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-10-17T02:00:00Z"));
  fetchSalesAnalyticsOptions.mockResolvedValue({ data: {
    business_types: [{ value: "备件销售", orders: 9 }],
    amount_range: { amount_ex_tax_max: 8000, amount_inc_tax_max: 9040, revenue_max: 7000 },
  } });
  fetchSalesAnalyticsSummary.mockResolvedValue({ data: {
    window: { date_from: TODAY, date_to: TODAY },
    filters_echo: { business_type: [], customer: null, salesperson: null },
    as_of: TODAY, kpi: KPI, line_mode: false, compare_available: false,
  } });
  fetchSalesAnalyticsOrders.mockResolvedValue({ data: {
    total: 0, page: 1, page_size: 50, as_of: TODAY, sort: "amount_ex_tax", order: "desc",
    include_returns: false, items: [],
  } });
  fetchSalesAnalyticsGroups.mockImplementation((q: Record<string, unknown>) =>
    Promise.resolve({ data: { ...GROUPS, group_by: q.group_by, sort: q.sort ?? "amount_ex_tax",
      order: q.order ?? "desc" } }));
  fetchSalesAnalyticsTrend.mockResolvedValue({ data: {
    granularity: "day", date_from: TODAY, date_to: TODAY,
    buckets: [{ bucket: TODAY, orders: 5, customers: 3, amount_ex_tax: 1000, amount_inc_tax: 1130,
      revenue: 800, gross_profit: 210, gross_margin: 0.3, avg_order: 200 }],
    compare: null, compare_window: null, compare_available: false, weekday: [],
  } });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("A3 看板控件局部主题：禁用 / 占位文字取 --mb-text-2（DL-025 返工）", () => {
  it("FilterBar + 金额区间：禁用控件与占位的样式块内是覆盖色，真实 disabled 底不变", async () => {
    renderPage();
    expect(await screen.findByText(/金额区间/)).toBeInTheDocument();

    // 禁用客户类型 Select（权限 disabled 语义保留：仍处于禁用态）
    const customerTypeSelect = screen.getByRole("combobox", { name: "客户类型" })
      .closest(".ant-select");
    expect(customerTypeSelect?.classList.contains("ant-select-disabled")).toBe(true);
    const hash = hashOf(customerTypeSelect);
    expect(hash).toBeTruthy();
    const css = themedCss(hash!);
    // 局部主题真的作用于该控件（删掉局部主题本断言即红）
    expect(css).toContain(VAR);
    // 禁用选择器：同一规则里保留真实 disabled 底 rgba(0,0,0,0.04)（独立探针合成
    // rgb(244.8) 的来源），只换文字色
    expect(css).toContain(`.ant-select-outlined.ant-select-disabled:not(.ant-select-customize-input) .ant-select-selector{background:rgba(0,0,0,0.04);color:${VAR}`);
    // Select 占位（含受限账号的「客户信息不可见」占位文案）
    expect(css).toContain(`.ant-select .ant-select-selection-placeholder`);
    expect(css).toContain(`.ant-select-selection-placeholder{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;flex:1;color:${VAR}`);

    // 金额区间禁用组：口径 Select 禁用 + InputNumber 值/占位「不限」
    const basisSelect = screen.getByRole("combobox", { name: "金额区间口径" }).closest(".ant-select");
    expect(basisSelect?.classList.contains("ant-select-disabled")).toBe(true);
    expect(hashOf(basisSelect)).toBe(hash);
    const minInput = screen.getByRole("spinbutton", { name: "金额下限" });
    expect(minInput).toBeDisabled();
    expect(minInput.getAttribute("placeholder")).toBe("不限");
    const numWrap = minInput.closest(".ant-input-number");
    expect(hashOf(numWrap)).toBe(hash);
    // 禁用数字输入与占位规则都换色（占位覆盖正常态与禁用态同一 token）。禁用
    // 输入规则按 size 生成 sm 变体，按「选择器含 disabled + 规则体是覆盖色」匹配
    expect(css).toContain(`.ant-input-number .ant-input-number-input::placeholder{color:${VAR}`);
    expect(css).toMatch(/ant-input-number-disabled[^{]*\{[^}]*color:var\(--mb-text-2, rgb\(107, 102, 94\)\)/);
  });

  it("分组维度弹层（portal）：禁用客户类选项与触发控件同 hash 且规则换色；行为不变", async () => {
    renderPage("/sales/analytics?tab=groups");
    // 受限替换：group_by=customer → salesperson（权限语义保留）
    await waitFor(() => expect(fetchSalesAnalyticsGroups).toHaveBeenCalled());
    expect(fetchSalesAnalyticsGroups.mock.calls[0][0]).toMatchObject({ group_by: "salesperson" });

    // 打开弹层（真实 Select，portal 渲染进 document.body，不在组件 DOM 子树内）
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "分组维度" }));
    const dropdown = await waitFor(() => {
      const el = document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
      expect(el).toBeTruthy();
      return el as Element;
    });
    expect(dropdown.closest(".ant-select")).toBeNull(); // 确认在 portal，不在控件子树

    // 客户类四项禁选（disabled 语义保留），销售人员可用
    for (const name of ["客户", "客户类型", "客户城市", "客户来源"]) {
      const option = document.querySelector(`.ant-select-item-option[title="${name}"]`);
      expect(option, name).toBeTruthy();
      expect(option!.classList.contains("ant-select-item-option-disabled"), name).toBe(true);
    }
    const salesperson = document.querySelector('.ant-select-item-option[title="销售人员"]');
    expect(salesperson?.classList.contains("ant-select-item-option-disabled")).toBe(false);

    // 弹层与触发控件同一 hashId（React 上下文取同一 token 集），其禁用选项规则已换色
    const triggerHash = hashOf(screen.getByRole("combobox", { name: "分组维度" }).closest(".ant-select"));
    const dropdownHash = hashOf(dropdown);
    expect(dropdownHash).toBe(triggerHash);
    const css = themedCss(dropdownHash!);
    expect(css).toContain(VAR);
    expect(css).toContain(`.ant-select-dropdown .ant-select-item-option-disabled{color:${VAR}`);

    // 行为不变：禁用项点不动（不发起 group_by=customer 请求），可用项正常切换
    const before = fetchSalesAnalyticsGroups.mock.calls.length;
    fireEvent.click(document.querySelector('.ant-select-item-option[title="客户"]')!);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "分组维度" }).getAttribute("aria-expanded")).toBe("true"));
    fireEvent.click(document.querySelector('.ant-select-item-option[title="仓库"]')!);
    await waitFor(() => expect(fetchSalesAnalyticsGroups.mock.calls.length).toBe(before + 1));
    expect(fetchSalesAnalyticsGroups.mock.lastCall?.[0]).toMatchObject({ group_by: "warehouse" });
  });

  it("稳态 hover 文字（DL-025 矩阵追加）：radio / 默认按钮 / 链接 hover 取 accent-strong，不再继承淡化主色", async () => {
    // 趋势页签有粒度 / 指标 radio（独立矩阵失败项：radio hover 淡化主色白底 3.12:1）
    renderPage("/sales/analytics?tab=trend");
    await waitFor(() => expect(fetchSalesAnalyticsTrend).toHaveBeenCalled());
    const wrapper = screen.getByRole("radio", { name: "按日" }).closest(".ant-radio-button-wrapper");
    const hash = hashOf(wrapper);
    expect(hash).toBeTruthy();
    const css = themedCss(hash!);
    // 未选中 radio hover：antd 5.21 直接取 colorPrimary（非 colorPrimaryHover 派生），
    // 因此局部主题必须连 colorPrimary 一起覆盖。colorPrimary 是 seed token，var()
    // 覆盖会被默认算法调色板派生污染成纯黑（probe4 实测），只能用与 token 同值
    // 的字面量（见 dashboardColors 注释）。字面量经派生后 cssinjs 按 FastColor 的
    // hex 小写序列化输出（通道与 rgb(44, 86, 174) 相同）——断言按通道相等比较，
    // 对序列化格式不敏感，且天然拒绝污染色（纯黑）回归
    const hoverRule = css.match(/ant-radio-button-wrapper:hover\{([^}]*)\}/);
    expect(hoverRule, "未选中 radio hover 规则存在").toBeTruthy();
    const hoverColor = hoverRule![1].match(/color:([^;]+);/);
    expect(hoverColor, "hover 规则含文字色").toBeTruthy();
    expect(parseColor(hoverColor![1].trim())).toEqual(parseColor(HOVER_LITERAL));
    // 选中 radio hover（colorPrimaryHover，map token 覆盖 var() 原样生效）与
    // 重置 / 导出默认按钮 hover 同源
    expect(css).toContain(`.ant-radio-button-wrapper-checked:not(.ant-radio-button-wrapper-disabled):hover{color:${HOVER_VAR}`);
    expect(css).toMatch(/ant-btn-variant-outlined[^{]*:hover[^{]*\{[^}]*color:var\(--mb-accent-strong, rgb\(44, 86, 174\)\)/);
    // 链接 hover（colorLinkHover）：概览「详细趋势 / 更多」与各链接按钮（白 / inset 底 6.22+）
    expect(css).toContain(`a:where(.${hash}):hover{color:${HOVER_VAR}`);
    expect(css).toContain(`.ant-btn-color-link.ant-btn-variant-link:not(:disabled):not(.ant-btn-disabled):hover{color:${HOVER_VAR}`);
  });

  it("导出按钮 loading 态（DL-025 loading 追加）：opacityLoading 覆写为 1，loading 态文字保持全 alpha", async () => {
    // 销售页导出按钮 loading={exporting}：antd 默认 opacityLoading=0.65 会把整个
    // 按钮置 0.65（button/style/index.js `&-loading{opacity:opacityLoading}`）。
    // 本仓 --mb-text 较深，0.65 合成白底仍有 4.73:1（root 注明静态依赖而非实测
    // 失败，但属踩线余量依赖）；局部主题 token=1 后规则生成 opacity:1，文字保持
    // 全 alpha；loading 语义（旋转图标 / 禁点）不动——只断透明度覆写，不改行为。
    // 导出按钮随订单明细页签渲染（state.tab === "orders"，默认页签）
    renderPage();
    await waitFor(() => expect(fetchSalesAnalyticsOrders).toHaveBeenCalled());
    const exportBtn = screen.getByRole("button", { name: /导\s*出/ }); // antd 两字中文按钮自动插空格
    expect(hashOf(exportBtn)).toBeTruthy();
    const css = themedCss(hashOf(exportBtn)!);
    expect(css).toMatch(/\.ant-btn[^{]*-loading\{[^}]*opacity:1(?!\.)/);
    expect(css).not.toMatch(/-loading\{[^}]*opacity:0\.65/);
  });
});
