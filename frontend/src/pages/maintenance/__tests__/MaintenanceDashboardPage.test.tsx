/** 维保数据看板页（D-0022/R33）：四页签、金额遮蔽（不渲染而非 ¥0）、
 * 默认近 7 天 URL 状态、T4 当前累计口径、导出与失败重试。 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const fetchDashboardOptions = vi.fn();
const fetchDashboardSummary = vi.fn();
const fetchDashboardProjects = vi.fn();
const fetchDashboardParts = vi.fn();
const fetchDashboardValuation = vi.fn();
const fetchDashboardQuantityChecks = vi.fn();
const downloadDashboardExport = vi.fn();
vi.mock("../../../api/maintenanceDashboard", () => ({
  fetchDashboardOptions: (...a: unknown[]) => fetchDashboardOptions(...a),
  fetchDashboardSummary: (...a: unknown[]) => fetchDashboardSummary(...a),
  fetchDashboardProjects: (...a: unknown[]) => fetchDashboardProjects(...a),
  fetchDashboardParts: (...a: unknown[]) => fetchDashboardParts(...a),
  fetchDashboardValuation: (...a: unknown[]) => fetchDashboardValuation(...a),
  fetchDashboardQuantityChecks: (...a: unknown[]) => fetchDashboardQuantityChecks(...a),
  downloadDashboardExport: (...a: unknown[]) => downloadDashboardExport(...a),
}));
// jsdom 无 canvas：mock 图表壳。
vi.mock("../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText }: { empty?: boolean; emptyText?: string }) =>
    empty ? <div>{emptyText ?? "空"}</div> : <div data-testid="chart" />,
}));

import MaintenanceDashboardPage from "../MaintenanceDashboardPage";
import type { DashboardOptions, SummaryResponse } from "../../../api/maintenanceDashboard";

const POLICY = {
  policy_version: "mdv-2026-10-21-r33",
  effective_from: "2026-10-21",
  condition_factors: { 坏件: "0.20", 坏品: "0.20", 故障: "0.20", 废品: "0.05", 成品: "1.00" },
  unknown_condition_rule: "件况未知不估价",
  low_sample_order_count: 2,
  price_priority: "purchase_preferred_sales_fallback",
  notes: "PN 交易参考×假设折算；非变卖收入",
};

const optionsWithAmounts: DashboardOptions = {
  range_presets: ["7d", "15d", "30d", "half_year", "year", "all", "custom"],
  default_range: "7d",
  default_date_rule: "近 7 天含今天（上海日历日）",
  sorts: {
    projects: ["bad_receipt_qty_desc", "receipt_qty_desc", "issued_qty_desc", "bad_value_desc", "project_code_asc"],
    parts: ["bad_receipt_qty_desc", "receipt_qty_desc", "bad_value_desc"],
    receipts: ["occurred_desc", "qty_desc", "pn_asc"],
    quantity_checks: ["difference_desc", "over_received_desc", "demand_qty_desc"],
  },
  projects: [
    { project_id: "p1", project_code: "MDD-P1", display_name: "项目一", business_type: "spare" },
    { project_id: "p2", project_code: "MDD-P2", display_name: "项目二", business_type: null },
  ],
  amounts_visible: true,
  max_export_rows: 50000,
  valuation_policy: POLICY,
};

const optionsQtyOnly: DashboardOptions = {
  ...optionsWithAmounts,
  amounts_visible: false,
  sorts: {
    ...optionsWithAmounts.sorts,
    projects: optionsWithAmounts.sorts.projects.filter((key) => key !== "bad_value_desc"),
    parts: optionsWithAmounts.sorts.parts.filter((key) => key !== "bad_value_desc"),
  },
};
// valuation_policy 键在无金额权的 options 响应里不存在（而非空对象）。
delete (optionsQtyOnly as { valuation_policy?: unknown }).valuation_policy;

function summaryFixture(withAmounts: boolean): SummaryResponse {
  const cards = {
    receipt_qty: { total: "7.000", part: "7.000", machine: "0.000" },
    bad_receipt_qty: { total: "6.000", scrap: "2.000" },
    issued_qty: { total: "10.000", note: "实际领用量（未采集件况）" },
    return_rate_pct: 70,
    rate_numerator: "7.000",
    rate_denominator: "10.000",
    project_count: 2,
  } as SummaryResponse["cards"];
  if (withAmounts) {
    cards.valuation = {
      state: "ready",
      bad_estimated_value: { inc_tax: "101.70", ex_tax: "90.00" },
      finished_estimated_value: { inc_tax: "0.00", ex_tax: "0.00" },
      estimated_bad_qty: "5.000",
      pending_bad_qty: "0.000",
      coverage_pct: 100,
      policy_version: POLICY.policy_version,
      note: POLICY.notes,
    };
  }
  return {
    range: { start: "2026-10-15", end: "2026-10-21" },
    cards,
    trend: [{ date: "2026-10-18", receipt_qty: "6.000", issued_qty: "10.000" }],
    condition_structure: [
      { test_result: "坏品", qty: "4.000" },
      { test_result: "废品", qty: "2.000" },
      { test_result: "未填", qty: "1.000" },
    ],
    top_projects: [{
      project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
      receipt_qty: "6.000", bad_receipt_qty: "6.000", issued_qty: "10.000",
    }],
    notes: ["另有 3.000 件无业务日期实收（当前累计，未计入期间）"],
  };
}

const PROJECT_ROW = {
  project_id: "p1", project_code: "MDD-P1", display_name: "项目一", business_type: "spare",
  receipt_qty: "6.000", bad_receipt_qty: "6.000", scrap_receipt_qty: "2.000",
  issued_qty: "10.000", exempt_issued_qty: "7.000", pending_requirement_qty: "0.000",
  unlinked_receipt_qty: "5.000", return_rate_pct: 60,
  rate_numerator: "6.000", rate_denominator: "10.000", no_return_note: null,
  valuation: {
    bad_estimated_value_inc_tax: "90.40", bad_estimated_value_ex_tax: "80.00",
    pending_bad_qty: "0.000", coverage_pct: 100,
  },
};

const projectsFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [PROJECT_ROW],
  pagination: { page: 1, page_size: 20, total: 1 },
};

const PART_ROW_QTY = {
  part_id: 1, pn: "MDDA-PN-1", description: "硬盘", unit: "件",
  receipt_qty: "6.000", bad_receipt_qty: "6.000", issued_qty: "10.000",
  project_count: 1, return_rate_pct: 60, rate_numerator: "6.000", rate_denominator: "10.000",
};

const partsQtyFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [PART_ROW_QTY],
  pagination: { page: 1, page_size: 20, total: 1 },
};

const valuationFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  policy: POLICY,
  note: POLICY.notes,
  cards: {
    bad_estimated_value: { inc_tax: "101.70", ex_tax: "90.00" },
    estimated_bad_qty: "5.000", pending_bad_qty: "0.000", coverage_pct: 100,
  },
  rows: [{
    ...PART_ROW_QTY,
    valuation: {
      bad_estimated_value_inc_tax: "101.70", bad_estimated_value_ex_tax: "90.00",
      estimated_bad_qty: "5.000", pending_bad_qty: "0.000",
      pending_reasons: {}, coverage_pct: 100,
    },
    references: {
      purchase: {
        available: true, inc_tax: "113.00", ex_tax: "100.00", sample_count: 2,
        order_count: 1, latest_order_date: "2026-10-18", oldest_order_date: "2026-10-18",
        low_confidence: false,
      },
      sales: { available: false, unavailable_reason: "no_samples" },
      chosen: "purchase", chosen_reason: null,
    },
  }],
  pagination: { page: 1, page_size: 20, total: 1 },
};

const checksFixture = {
  cards: {
    checked_demand_qty: "13.000", received_qty: "9.000",
    known_difference_qty: "6.000", over_received_qty: "2.000",
    pending_check_order_count: 0, inactive_order_count: 0,
    unassigned_receipt_qty: "3.000",
    note: "当前累计（整单跨全部 PN/来源/件况）；不发布返还状态",
  },
  rows: [{
    source_order_id: "o1", order_no: "DD001", project_id: "p1", project_code: "MDD-P1",
    display_name: "项目一", order_date: "2026-10-16", demand_type: "repair",
    warehouse: "广州仓", demand_qty: "10.000", received_qty: "4.000",
    difference_qty: "6.000", over_received_qty: "0.000", basis_state: "complete",
    reason_codes: [], undated_receipt_qty: null, no_return_note: null,
  }],
  unassigned_receipts: [{
    project_id: "p2", project_code: "MDD-P2", display_name: "项目二", received_qty: "3.000",
  }],
  pagination: { page: 1, page_size: 20, total: 1 },
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function renderPage(initialPath = "/maintenance/dashboard") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <MaintenanceDashboardPage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function query() {
  return new URLSearchParams(screen.getByTestId("location").textContent ?? "");
}

async function selectOption(ariaLabel: string, optionTitle: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: ariaLabel }));
  const option = await waitFor(() => {
    const found = document.querySelector(`.ant-select-item-option[title="${optionTitle}"]`);
    expect(found).not.toBeNull();
    return found!;
  });
  fireEvent.click(option);
}

beforeAll(() => {
  // jsdom 无 blob URL：导出下载只验证调用与文件名解析，不落盘。
  Object.defineProperty(URL, "createObjectURL", {
    value: vi.fn(() => "blob:mock-dashboard"), configurable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
});

afterAll(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("维保数据看板页", () => {
  beforeEach(() => {
    fetchDashboardOptions.mockReset();
    fetchDashboardSummary.mockReset();
    fetchDashboardProjects.mockReset();
    fetchDashboardParts.mockReset();
    fetchDashboardValuation.mockReset();
    fetchDashboardQuantityChecks.mockReset();
    downloadDashboardExport.mockReset();
    fetchDashboardOptions.mockResolvedValue(optionsWithAmounts);
    fetchDashboardSummary.mockResolvedValue(summaryFixture(true));
    fetchDashboardProjects.mockResolvedValue(projectsFixture);
    fetchDashboardParts.mockResolvedValue(partsQtyFixture);
    fetchDashboardValuation.mockResolvedValue(valuationFixture);
    fetchDashboardQuantityChecks.mockResolvedValue(checksFixture);
    downloadDashboardExport.mockResolvedValue({
      blob: new Blob(["csv"], { type: "text/csv" }),
      filename: "maintenance-dashboard-projects.csv",
    });
  });

  it("默认进入总览页签，URL 不写默认值且默认按近 7 天（含今天）取数", async () => {
    renderPage();
    const card = await screen.findByTestId("kpi-receipt");
    expect(card.textContent).toContain("7");
    // 四个页签齐全
    for (const label of ["回收与领用总览", "项目对比", "PN 排名与估价", "需求单核对"]) {
      expect(screen.getByRole("tab", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("tab", { name: "回收与领用总览" })).toHaveAttribute("aria-selected", "true");
    // 默认值不入 URL（useUrlState 契约），请求侧显式带 range=7d
    expect(query().get("range")).toBeNull();
    expect(query().get("tab")).toBeNull();
    expect(fetchDashboardSummary).toHaveBeenCalledWith({ range: "7d" });
    // 无业务日期实收以提示条单独说明（partial-missing 状态）
    expect(screen.getByTestId("overview-notes")).toHaveTextContent("无业务日期");
    // 金额卡与估算口径说明可见（本账号有权）
    expect(screen.getByTestId("kpi-valuation")).toHaveTextContent("101.7");
    expect(screen.getByTestId("kpi-valuation")).toHaveTextContent(POLICY.policy_version);
    expect(screen.getAllByText(/PN 交易参考×假设折算；非变卖收入/).length).toBeGreaterThan(0);
  });

  it("无金额权：金额卡/列/图与政策说明整块不渲染（不是 ¥0），估价端点不调用", async () => {
    fetchDashboardOptions.mockResolvedValue(optionsQtyOnly);
    fetchDashboardSummary.mockResolvedValue(summaryFixture(false));
    renderPage();
    await screen.findByTestId("kpi-receipt");
    expect(screen.queryByTestId("kpi-valuation")).not.toBeInTheDocument();
    expect(screen.queryByText(/坏件估算价值/)).not.toBeInTheDocument();
    expect(screen.queryByText(/PN 交易参考×假设折算/)).not.toBeInTheDocument();
    expect(screen.getByTestId("amounts-restricted-banner")).toBeInTheDocument();

    // T2 项目对比：无金额列
    fireEvent.click(screen.getByRole("tab", { name: "项目对比" }));
    await screen.findByText("项目一");
    expect(screen.queryByRole("columnheader", { name: /坏件估算价值/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /覆盖率/ })).not.toBeInTheDocument();
    expect(fetchDashboardValuation).not.toHaveBeenCalled();

    // T3 PN 排名：走数量端点，估价/参考列不存在
    fireEvent.click(screen.getByRole("tab", { name: "PN 排名与估价" }));
    await screen.findByText("MDDA-PN-1");
    expect(fetchDashboardParts).toHaveBeenCalledWith(
      expect.objectContaining({ range: "7d", sort: "bad_receipt_qty_desc", page: 1, page_size: 20 }),
    );
    expect(fetchDashboardValuation).not.toHaveBeenCalled();
    expect(screen.queryByRole("columnheader", { name: /坏件估算价值/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /交易参考/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("amounts-hidden-note")).toBeInTheDocument();
  });

  it("有金额权：T3 走估价端点，展示政策版本、系数、双域参考与选用结果", async () => {
    renderPage("/maintenance/dashboard?tab=parts");
    await screen.findByText("MDDA-PN-1");
    expect(fetchDashboardValuation).toHaveBeenCalledWith(
      expect.objectContaining({ range: "7d", sort: "bad_receipt_qty_desc", page: 1, page_size: 20 }),
    );
    expect(fetchDashboardSummary).not.toHaveBeenCalled(); // 按需挂载：未激活的页签不取数
    const note = screen.getByTestId("valuation-policy-note");
    expect(note).toHaveTextContent("PN 交易参考×假设折算；非变卖收入");
    expect(note).toHaveTextContent("坏件×0.20");
    expect(note).toHaveTextContent(POLICY.policy_version);
    expect(screen.getByRole("columnheader", { name: /坏件估算价值/ })).toBeInTheDocument();
    expect(screen.getByText("采购 ¥113.00（1 单）")).toBeInTheDocument();
    expect(screen.getByText(/选用：采购/)).toBeInTheDocument();
  });

  it("T4 当前累计：N/R/差额/超收齐备，不出现返还状态字段，q 不下发给该端点", async () => {
    renderPage("/maintenance/dashboard?tab=checks&q=ST");
    await screen.findByText("DD001");
    expect(screen.getByTestId("checks-note")).toHaveTextContent("当前累计");
    expect(screen.getByTestId("checks-note")).toHaveTextContent("不发布返还状态");
    expect(screen.getByText("需求量 N 合计")).toBeInTheDocument();
    expect(screen.getByText("未关联需求单的实收（当前累计）")).toBeInTheDocument();
    expect(screen.getByText(/项目二（MDD-P2）/)).toBeInTheDocument();
    for (const forbidden of ["欠返", "已返", "逾期", "返还状态："]) {
      expect(screen.queryByText(new RegExp(forbidden))).not.toBeInTheDocument();
    }
    expect(fetchDashboardQuantityChecks).toHaveBeenCalledWith(
      expect.not.objectContaining({ q: expect.anything() }),
    );
    // 需求单核对页签不渲染关键词输入框
    expect(screen.queryByPlaceholderText("搜 PN / 描述")).not.toBeInTheDocument();
  });

  it("切换时间范围写入 URL 并按新范围重新取数", async () => {
    renderPage();
    await screen.findByTestId("kpi-receipt");
    await selectOption("时间范围", "近 30 天");
    await waitFor(() => expect(query().get("range")).toBe("30d"));
    await waitFor(() => expect(fetchDashboardSummary).toHaveBeenLastCalledWith({ range: "30d" }));
  });

  it("T2 导出 CSV：以当前筛选集调用导出端点并触发浏览器下载", async () => {
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "projects", format: "csv", range: "7d" }),
    ));
    await waitFor(() => expect(screen.getByText(/已导出 maintenance-dashboard-projects\.csv/)).toBeInTheDocument());
  });

  it("总览加载失败显示错误与重试，重试后恢复", async () => {
    fetchDashboardSummary.mockRejectedValueOnce({
      response: { data: { detail: { message: "数据库暂时不可用" } } },
    });
    renderPage();
    const alert = await screen.findByText("数据库暂时不可用");
    expect(alert.closest(".ant-alert")).not.toBeNull();
    fireEvent.click(within(alert.closest(".ant-alert")!).getByRole("button", { name: /重\s*试/ }));
    await screen.findByTestId("kpi-receipt");
    expect(fetchDashboardSummary).toHaveBeenCalledTimes(2);
  });
});
