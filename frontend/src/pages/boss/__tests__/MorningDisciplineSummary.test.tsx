import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Grid } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const dashboardPriceDisciplineSummary = vi.fn();
const fetchDashboardOrderDetail = vi.fn();

vi.mock("../../../api", () => ({
  dashboardPriceDisciplineSummary: (...args: unknown[]) => dashboardPriceDisciplineSummary(...args),
}));

vi.mock("../../../api/poolAnalysis", () => ({
  fetchDashboardOrderDetail: (...args: unknown[]) => fetchDashboardOrderDetail(...args),
  fetchPoolAnalysisOrderDetail: vi.fn(),
}));

import MorningDisciplineSummary from "../MorningDisciplineSummary";

const breakpoint = vi.spyOn(Grid, "useBreakpoint");

const SUMMARY = {
  window: { range: "custom", date_from: "2026-07-01", date_to: "2026-07-15", as_of: "2026-07-15" },
  basis: "ex_tax", restricted: false,
  purchase: { violation_line_count: 3, order_count: 2, pool_count: 2, total_gap: 660 },
  sales: { violation_line_count: 2, order_count: 2, pool_count: 1, total_gap: 360 },
  most_severe_pool: {
    pool_group_id: 7, pool_name: "硬盘池", purchase_total_gap: 600,
    sales_total_gap: 300, total_gap: 900, violation_line_count: 4, dominant_side: "purchase",
  },
  handler_summary: {
    purchase: [{ person: "张三", violation_line_count: 2, order_count: 1, total_gap: 600 }],
    sales: [{ person: "李四", violation_line_count: 2, order_count: 2, total_gap: 360 }],
  },
  recent_violations: [
    { side: "purchase", line_id: 1001, order_id: 101, order_no: "DUP-001", order_date: "2026-07-14",
      part_id: 11, pn_std: "PN-A", pool_group_id: 7, pool_name: "硬盘池", person: "张三",
      quantity: 2, actual_unit_ex_tax: 430, manual_limit_ex_tax: 400, unit_gap: 30, total_gap: 60 },
    { side: "sales", line_id: 2002, order_id: 202, order_no: "DUP-001", order_date: "2026-07-13",
      part_id: 12, pn_std: "PN-B", pool_group_id: 8, pool_name: "内存池", person: "李四",
      quantity: 3, actual_unit_ex_tax: 180, manual_limit_ex_tax: 200, unit_gap: 20, total_gap: 60 },
  ],
  missing_constraints: { active_pool_count: 12, purchase_ceiling_unset_count: 4,
    sales_floor_unset_count: 5, both_unset_count: 3 },
};

const ORDER_DETAIL = {
  side: "sales", price_restricted: false, supplier_restricted: false, customer_restricted: false,
  order: { order_id: 202, order_no: "DUP-001", order_date: "2026-07-13",
    salesperson: "李四", customer: "客户甲", business_type: "备件销售", data_status: "已生效",
    sale_order_amount_ex_tax: 540 },
  items: [{ line_id: 22, part_id: 12, pn_std: "PN-B", description: "内存", brand: null,
    quantity: 3, unit: "块", sale_unit_price_ex_tax: 180, sale_line_value_ex_tax: 540,
    anomaly_flags: [], pool_group_id: 8, pool_name: "内存池" }],
};

function renderSummary(overrides: Record<string, unknown> = {}) {
  const props = {
    dateRange: { date_from: "2026-07-01", date_to: "2026-07-15" },
    localGovernanceRestricted: false,
    ...overrides,
  };
  return render(
    <MemoryRouter initialEntries={["/boss"]}>
      <Routes>
        <Route path="/boss" element={<MorningDisciplineSummary {...props} />} />
        <Route path="/pool-analysis/:groupId" element={<div>池详情页</div>} />
        <Route path="/parts" element={<div>型号全景页</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  breakpoint.mockReturnValue({ xs: false, sm: true, md: true, lg: true, xl: true, xxl: true });
  dashboardPriceDisciplineSummary.mockResolvedValue({ data: SUMMARY });
  fetchDashboardOrderDetail.mockResolvedValue(ORDER_DETAIL);
});

afterEach(cleanup);

describe("早会价格纪律摘要", () => {
  it("展示四张事实卡、最近越线与只读边界，金额差和订单数口径分离", async () => {
    renderSummary();
    expect(await screen.findByText("早会价格纪律摘要")).toBeInTheDocument();
    expect(screen.getByText("高于采购上限")).toBeInTheDocument();
    expect(screen.getByText("低于销售下限")).toBeInTheDocument();
    expect(screen.getByText("差额最大池")).toBeInTheDocument();
    expect(screen.getByText("涉及经办人")).toBeInTheDocument();
    expect(screen.getByText(/历史分析，只记录展示，不拦截订单/)).toBeInTheDocument();
    expect(screen.getByText(/次数和差额不等于员工评价/)).toBeInTheDocument();
    expect(screen.getByText("3 行 · 2 单 · 2 个池")).toBeInTheDocument();
    expect(screen.getByText("价差主要来自采购")).toBeInTheDocument();
    expect(screen.getByText("最近 10 条越线记录")).toBeInTheDocument();
    expect(dashboardPriceDisciplineSummary).toHaveBeenCalledWith({
      date_from: "2026-07-01", date_to: "2026-07-15",
    });
  });

  it("经办人超过三人时可展开查看全部，不静默截断", async () => {
    dashboardPriceDisciplineSummary.mockResolvedValue({ data: {
      ...SUMMARY,
      handler_summary: { ...SUMMARY.handler_summary, purchase: [
        ...SUMMARY.handler_summary.purchase,
        { person: "采购乙", violation_line_count: 1, order_count: 1, total_gap: 80 },
        { person: "采购丙", violation_line_count: 1, order_count: 1, total_gap: 70 },
        { person: "采购丁", violation_line_count: 1, order_count: 1, total_gap: 60 },
      ] },
    } });
    renderSummary();
    const summary = await screen.findByText("查看其余 1 人");
    expect(screen.getByText(/采购丁 1行\/1单/)).not.toBeVisible();
    fireEvent.click(summary);
    expect(screen.getByText(/采购丁 1行\/1单/)).toBeVisible();
  });

  it("restricted=true 时不渲染任何次数、金额、人员或排行侧信道", async () => {
    dashboardPriceDisciplineSummary.mockResolvedValue({ data: { ...SUMMARY, restricted: true } });
    renderSummary();
    expect(await screen.findByText(/无池价格纪律查看权限/)).toBeInTheDocument();
    expect(screen.queryByText("张三")).toBeNull();
    expect(screen.queryByText("硬盘池")).toBeNull();
    expect(screen.queryByText("3 行 · 2 单 · 2 个池")).toBeNull();
    expect(screen.queryByText(/¥660/)).toBeNull();
  });

  it("本地权限已收紧时首屏直接显示无权限且不发请求", async () => {
    renderSummary({ localGovernanceRestricted: true });
    expect(await screen.findByText(/无池价格纪律查看权限/)).toBeInTheDocument();
    expect(dashboardPriceDisciplineSummary).not.toHaveBeenCalled();
  });

  it("未设约束与零越线严格分离，不把未设置写成零越线", async () => {
    dashboardPriceDisciplineSummary.mockResolvedValue({ data: {
      ...SUMMARY,
      purchase: { violation_line_count: 0, order_count: 0, pool_count: 0, total_gap: 0 },
      sales: { violation_line_count: 0, order_count: 0, pool_count: 0, total_gap: 0 },
      most_severe_pool: null,
      handler_summary: { purchase: [], sales: [] },
      recent_violations: [],
      missing_constraints: { active_pool_count: 12, purchase_ceiling_unset_count: 4,
        sales_floor_unset_count: 5, both_unset_count: 3 },
    } });
    renderSummary();
    expect(await screen.findByText(/另有 4 个池未设采购上限/)).toBeInTheDocument();
    expect(screen.getByText(/5 个池未设销售下限/)).toBeInTheDocument();
    expect(screen.getByText(/其中 3 个池两侧都未设置/)).toBeInTheDocument();
    expect(screen.getByText(/未设置不等于零越线/)).toBeInTheDocument();
    expect(screen.getByText(/当前范围内未发现越线记录/)).toBeInTheDocument();
  });

  it("真正空数据显示明确空态，且不伪造未设约束提示", async () => {
    dashboardPriceDisciplineSummary.mockResolvedValue({ data: {
      ...SUMMARY,
      purchase: { violation_line_count: 0, order_count: 0, pool_count: 0, total_gap: 0 },
      sales: { violation_line_count: 0, order_count: 0, pool_count: 0, total_gap: 0 },
      most_severe_pool: null, handler_summary: { purchase: [], sales: [] }, recent_violations: [],
      missing_constraints: { active_pool_count: 12, purchase_ceiling_unset_count: 0,
        sales_floor_unset_count: 0, both_unset_count: 0 },
    } });
    renderSummary();
    expect(await screen.findByText(/当前范围内未发现越线记录/)).toBeInTheDocument();
    expect(screen.queryByText(/未设置不等于零越线/)).toBeNull();
  });

  it("失败时清空旧数据并提供重试，恢复后重新展示", async () => {
    const view = renderSummary();
    expect((await screen.findAllByText("张三")).length).toBeGreaterThan(0);

    dashboardPriceDisciplineSummary.mockRejectedValueOnce(new Error("offline"));
    view.rerender(
      <MemoryRouter initialEntries={["/boss"]}>
        <MorningDisciplineSummary dateRange={{ date_from: "2026-07-15", date_to: "2026-07-15" }}
          localGovernanceRestricted={false} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/价格纪律摘要加载失败/)).toBeInTheDocument();
    expect(screen.queryByText("张三")).toBeNull();
    dashboardPriceDisciplineSummary.mockResolvedValueOnce({ data: SUMMARY });
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    expect(await screen.findByText("张三")).toBeInTheDocument();
  });

  it("快速切日期时，最后返回的旧响应不能覆盖新窗口", async () => {
    let resolveOld!: (value: unknown) => void;
    let resolveNew!: (value: unknown) => void;
    dashboardPriceDisciplineSummary
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve; }));
    const view = renderSummary();
    view.rerender(
      <MemoryRouter initialEntries={["/boss"]}>
        <MorningDisciplineSummary dateRange={{ date_from: "2026-07-15", date_to: "2026-07-15" }}
          localGovernanceRestricted={false} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(dashboardPriceDisciplineSummary).toHaveBeenCalledTimes(2));
    resolveNew({ data: { ...SUMMARY,
      purchase: { violation_line_count: 9, order_count: 8, pool_count: 7, total_gap: 999 },
    } });
    expect(await screen.findByText("9 行 · 8 单 · 7 个池")).toBeInTheDocument();
    resolveOld({ data: SUMMARY });
    await waitFor(() => expect(screen.getByText("9 行 · 8 单 · 7 个池")).toBeInTheDocument());
    expect(screen.queryByText("3 行 · 2 单 · 2 个池")).toBeNull();
  });

  it("池与 PN 保留可达深链；重复单号仍按 side + order_id 打开正确订单", async () => {
    renderSummary();
    const poolLinks = await screen.findAllByRole("link", { name: "进入池「硬盘池」分析详情" });
    expect(poolLinks[0]).toHaveAttribute("href", "/pool-analysis/7?from=2026-07-01&to=2026-07-15");
    expect(screen.getByRole("link", { name: "查看型号 PN-A 全景" }))
      .toHaveAttribute("href", "/parts?part_id=11");

    const duplicateButtons = screen.getAllByRole("button", { name: /查看销售订单 DUP-001/ });
    fireEvent.click(duplicateButtons[0]);
    await waitFor(() => expect(fetchDashboardOrderDetail).toHaveBeenCalledWith("sales", 202));
    expect(await screen.findByText("客户甲")).toBeInTheDocument();
  });

  it("窄屏使用卡片列表而不是宽表格，交互元素仍是原生按钮/链接", async () => {
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    renderSummary();
    const list = await screen.findByTestId("discipline-mobile-list");
    expect(within(list).getAllByRole("button", { name: /查看.*订单/ })).toHaveLength(2);
    expect(screen.queryByTestId("discipline-desktop-table")).toBeNull();
  });
});
