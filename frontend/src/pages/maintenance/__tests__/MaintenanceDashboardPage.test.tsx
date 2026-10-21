/** 维保数据看板页（R33/D-0022 + R34/D-0024）：五页签、金额遮蔽（不渲染而非 ¥0）、
 * 试算比例（默认 50% / 显式 0 / 自定义）、三域并列不相加、期间下单与每日消耗
 * （M01/M03）、返件明细下钻（M-AC09）、会话守卫（DL-010）、T4 当前累计口径、
 * 导出与失败重试。 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const fetchDashboardOptions = vi.fn();
const fetchDashboardSummary = vi.fn();
const fetchDashboardProjects = vi.fn();
const fetchDashboardParts = vi.fn();
const fetchDashboardValuation = vi.fn();
const fetchDashboardQuantityChecks = vi.fn();
const fetchDashboardOrders = vi.fn();
const fetchDashboardConsumption = vi.fn();
const fetchDashboardConsumptionIssues = vi.fn();
const fetchDashboardReceipts = vi.fn();
const downloadDashboardExport = vi.fn();
vi.mock("../../../api/maintenanceDashboard", () => ({
  fetchDashboardOptions: (...a: unknown[]) => fetchDashboardOptions(...a),
  fetchDashboardSummary: (...a: unknown[]) => fetchDashboardSummary(...a),
  fetchDashboardProjects: (...a: unknown[]) => fetchDashboardProjects(...a),
  fetchDashboardParts: (...a: unknown[]) => fetchDashboardParts(...a),
  fetchDashboardValuation: (...a: unknown[]) => fetchDashboardValuation(...a),
  fetchDashboardQuantityChecks: (...a: unknown[]) => fetchDashboardQuantityChecks(...a),
  fetchDashboardOrders: (...a: unknown[]) => fetchDashboardOrders(...a),
  fetchDashboardConsumption: (...a: unknown[]) => fetchDashboardConsumption(...a),
  fetchDashboardConsumptionIssues: (...a: unknown[]) => fetchDashboardConsumptionIssues(...a),
  fetchDashboardReceipts: (...a: unknown[]) => fetchDashboardReceipts(...a),
  downloadDashboardExport: (...a: unknown[]) => downloadDashboardExport(...a),
}));
// jsdom 无 canvas：mock 图表壳；暴露 onEvents.click 触发按钮（趋势图日期下钻）。
vi.mock("../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText, onEvents, testId }: {
    empty?: boolean; emptyText?: string; testId?: string;
    onEvents?: Record<string, (params: unknown) => void>;
  }) => (empty
    ? <div>{emptyText ?? "空"}</div>
    : (
      <div data-testid={testId ?? "chart"}>
        {onEvents?.click ? (
          <button type="button" onClick={() => onEvents.click({ dataIndex: 0 })}>
            触发图表点击
          </button>
        ) : null}
      </div>
    )),
}));

import MaintenanceDashboardPage from "../MaintenanceDashboardPage";
import type {
  DashboardOptions,
  OrdersResponse,
  SummaryResponse,
} from "../../../api/maintenanceDashboard";

/** R34 政策（D-0024 + Sunny 01:20 裁定）：默认 50% 可调，20/30 仅快捷选项。 */
const POLICY = {
  policy_version: "mdv-2026-10-22-r34",
  retention_ratio: null,
  retention_ratio_required: true,
  quick_options: ["0.5", "0.3", "0.2"],
  retention_rule: "价值保留比例 = 参考价×比例；本次试算参数，缺省按 50% 试算",
  window_days: 7,
  window_rule: "含统计结束日的 7 个上海日历日",
  half_life_days: "7",
  weighting: "数量×时间衰减（半衰期 7 天），双税分别计算",
  three_domain_rule: "采购/销售/领用已计成本并列，不相加",
  unknown_condition_rule: "件况未知（未填/其他/任意文本）不估价",
  finished_goods_rule: "成品按参考全值另列，不混入坏件主卡",
  low_sample_order_count: 2,
  price_priority: "purchase_first_sales_fallback_cost_parallel",
  notes: "估算／试算，非变卖收入；系数变化只改变试算，不改变任何业务成本、库存或已收货数量",
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
  export_kinds: [
    "projects", "parts", "receipts", "quantity_checks",
    "orders", "consumption_daily", "consumption_issues",
  ],
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

/** M-AC01 场景数字：期间下单 10 / 实际领用 6 / 实收 3（各自事实日期入期）。 */
function summaryFixture(withAmounts: boolean, ratio: string | null): SummaryResponse {
  const cards = {
    receipt_qty: { total: "3.000", part: "3.000", machine: "0.000" },
    bad_receipt_qty: { total: "2.000", scrap: "1.000" },
    issued_qty: { total: "6.000", note: "实际领用量（未采集件况）" },
    return_rate_pct: 50,
    rate_numerator: "3.000",
    rate_denominator: "6.000",
    project_count: 2,
  } as SummaryResponse["cards"];
  if (withAmounts) {
    const valuation = {
      state: ratio === null ? "awaiting_ratio" : "ready",
      estimated_bad_qty: "1.000",
      pending_bad_qty: "1.000",
      awaiting_ratio_bad_qty: ratio === null ? "1.000" : "0.000",
      coverage_pct: ratio === null ? null : 50,
      retention_ratio: ratio,
      domains: {
        purchase: { value_inc_tax: "56.50", value_ex_tax: "50.00" },
        cost: { value_inc_tax: "45.20", value_ex_tax: "40.00" },
      },
      policy_version: POLICY.policy_version,
      note: POLICY.notes,
    } as SummaryResponse["cards"]["valuation"];
    if (ratio !== null) {
      valuation!.bad_estimated_value = { inc_tax: "56.50", ex_tax: "50.00" };
      valuation!.finished_estimated_value = { inc_tax: "0.00", ex_tax: "0.00" };
    }
    cards.valuation = valuation;
  }
  return {
    range: { start: "2026-10-15", end: "2026-10-21" },
    cards,
    trend: [{ date: "2026-10-18", receipt_qty: "3.000", issued_qty: "6.000" }],
    condition_structure: [
      { test_result: "坏品", qty: "1.000" },
      { test_result: "废品", qty: "1.000" },
      { test_result: "未填", qty: "1.000" },
    ],
    top_projects: [{
      project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
      receipt_qty: "3.000", bad_receipt_qty: "2.000", issued_qty: "6.000",
    }],
    notes: ["另有 1.000 件无业务日期实收（当前累计，未计入期间）"],
  };
}

const ordersFixture: OrdersResponse = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  cards: { order_qty: "10.000", order_count: 2 },
  rows: [
    { project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
      business_type: "spare", order_qty: "8.000", order_count: 1 },
    { project_id: "p2", project_code: "MDD-P2", display_name: "项目二",
      business_type: null, order_qty: "2.000", order_count: 1 },
  ],
};

const PROJECT_ROW = {
  project_id: "p1", project_code: "MDD-P1", display_name: "项目一", business_type: "spare",
  receipt_qty: "3.000", bad_receipt_qty: "2.000", scrap_receipt_qty: "1.000",
  issued_qty: "6.000", exempt_issued_qty: "1.000", pending_requirement_qty: "0.000",
  unlinked_receipt_qty: "1.000", return_rate_pct: 50,
  rate_numerator: "3.000", rate_denominator: "6.000", no_return_note: null,
  valuation: {
    state: "awaiting_ratio",
    pending_bad_qty: "1.000",
    awaiting_ratio_bad_qty: "1.000",
    coverage_pct: null,
  },
};

const projectsFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [PROJECT_ROW],
  pagination: { page: 1, page_size: 20, total: 1 },
};

const PART_ROW_QTY = {
  part_id: 1, pn: "MDDA-PN-1", description: "硬盘", unit: "件",
  receipt_qty: "3.000", bad_receipt_qty: "2.000", issued_qty: "6.000",
  project_count: 1, return_rate_pct: 50, rate_numerator: "3.000", rate_denominator: "6.000",
};

const partsQtyFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [PART_ROW_QTY],
  pagination: { page: 1, page_size: 20, total: 1 },
};

/** R34 三域参考：采购可用、销售回退不可用、领用已计成本独立可用。 */
const referencesFixture = {
  purchase: {
    available: true, inc_tax: "113.00", ex_tax: "100.00", sample_count: 2,
    order_count: 1, latest_order_date: "2026-10-18", oldest_order_date: "2026-10-18",
    low_confidence: false,
  },
  sales: { available: false, unavailable_reason: "no_samples" },
  cost: {
    available: true, unit_cost_inc_tax: "22.60", unit_cost_ex_tax: "20.00",
    sample_line_count: 3, issue_qty: "4.000", costed_qty: "4.000", uncosted_qty: "0.000",
    coverage_pct: 100, sources: ["需求成本"], latest_issue_date: "2026-10-20",
    oldest_issue_date: "2026-10-16",
    note: "领用已计成本参考（可能含估算）；原成本证据可能晚于锚点，不再税转换",
  },
  chosen: "purchase", chosen_reason: null,
};

function valuationFixture(ratio: string | null) {
  const policy = { ...POLICY, retention_ratio: ratio };
  const partValuation: Record<string, unknown> = {
    state: ratio === null ? "awaiting_ratio" : "ready",
    estimated_bad_qty: ratio === null ? "0.000" : "1.000",
    pending_bad_qty: "1.000",
    awaiting_ratio_bad_qty: ratio === null ? "1.000" : "0.000",
    pending_reasons: { unknown_condition: "1.000" },
    coverage_pct: ratio === null ? null : 50,
  };
  if (ratio !== null) {
    partValuation.bad_estimated_value_inc_tax = "56.50";
    partValuation.bad_estimated_value_ex_tax = "50.00";
    partValuation.domains = {
      purchase: { value_inc_tax: "56.50", value_ex_tax: "50.00" },
      cost: { value_inc_tax: "45.20", value_ex_tax: "40.00" },
    };
  }
  const cards: Record<string, unknown> = {
    estimated_bad_qty: ratio === null ? "0.000" : "1.000",
    pending_bad_qty: "1.000",
    awaiting_ratio_bad_qty: ratio === null ? "1.000" : "0.000",
    coverage_pct: ratio === null ? null : 50,
  };
  if (ratio !== null) {
    cards.bad_estimated_value = { inc_tax: "56.50", ex_tax: "50.00" };
  }
  return {
    range: { start: "2026-10-15", end: "2026-10-21" },
    policy,
    note: POLICY.notes,
    cards,
    rows: [{
      ...PART_ROW_QTY,
      valuation: partValuation,
      references: referencesFixture,
    }],
    pagination: { page: 1, page_size: 20, total: 1 },
  };
}

function consumptionFixture(withAmounts: boolean) {
  return {
    range: { start: "2026-10-15", end: "2026-10-21" },
    cards: withAmounts ? {
      issued_qty: "6.000",
      cost_amount_ex_tax: "80.00", cost_amount_inc_tax: "90.40",
      uncosted_qty: "2.000", coverage_pct: 66.7,
      // 真实 DB 枚举码 direct/window + 一个未收录码（验证未知码保留原码标注）。
      sources: ["direct", "window", "future_algo"],
      note: "只读既有已计成本（可能含估算，原取价证据可能晚于领用日）；不回算、不减返件估值、不覆盖成本链",
    } : { issued_qty: "6.000" },
    rows: [
      {
        issue_date: "2026-10-20", project_id: "p1", project_code: "MDD-P1",
        display_name: "项目一", issued_qty: "6.000", line_count: 2,
        ...(withAmounts ? {
          cost_amount_ex_tax: "80.00", cost_amount_inc_tax: "90.40",
          costed_qty: "4.000", uncosted_qty: "2.000", coverage_pct: 66.7,
          sources: ["direct", "window"],
        } : {}),
      },
    ],
    pagination: { page: 1, page_size: 20, total: 1 },
  };
}

/** M-AC09 返件明细 Drawer 行（R33 GET /receipts）：一条已试算 + 一条未关联/未填件况。 */
const receiptsFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [
    {
      rkd_line_id: "r1", occurred_at: "2026-10-18 10:00:00", project_id: "p1",
      project_code: "MDD-P1", source_order_id: "o1", source_order_no: "DD001",
      part_id: 1, pn: "MDDA-PN-1", description: "硬盘", receipt_kind: "备件",
      unit: "件", test_result: "坏品", qty: "2.000", source: "维修返还",
      review_required: false,
      valuation: {
        state: "ready", classification: "bad", factor: "0.5",
        value_inc_tax: "56.50", value_ex_tax: "50.00",
      },
    },
    {
      rkd_line_id: "r2", occurred_at: "2026-10-19 09:00:00", project_id: "p1",
      project_code: "MDD-P1", source_order_id: null, source_order_no: null,
      part_id: 2, pn: "MDDA-PN-2", description: "内存", receipt_kind: "备件",
      unit: null, test_result: null, qty: "1.000", source: "其他",
      review_required: true,
    },
  ],
  pagination: { page: 1, page_size: 20, total: 2 },
};

/** M-AC02：同日同项目两条领用，仅一条有已计成本；数量均计，金额只报已知。
 * cost_source 用后端 DB 枚举真实码（direct；契约示例 direct_purchase 系笔误）。 */
const consumptionIssuesFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [
    {
      issue_line_id: 11, issue_id: 1, issue_no: "LY-001", issue_date: "2026-10-20",
      project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
      part_id: 1, pn: "MDDA-PN-1", qty: "4.000",
      cost_source: "direct", algorithm_version: "mc-v3", reference_side: "purchase",
      unit_cost_ex_tax: "20.00", unit_cost_inc_tax: "22.60",
      cost_amount_ex_tax: "80.00", cost_amount_inc_tax: "90.40",
    },
    {
      issue_line_id: 12, issue_id: 1, issue_no: "LY-001", issue_date: "2026-10-20",
      project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
      part_id: 2, pn: "MDDA-PN-2", qty: "2.000",
    },
  ],
  pagination: { page: 1, page_size: 50, total: 2 },
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
    fetchDashboardOrders.mockReset();
    fetchDashboardConsumption.mockReset();
    fetchDashboardConsumptionIssues.mockReset();
    fetchDashboardReceipts.mockReset();
    downloadDashboardExport.mockReset();
    fetchDashboardOptions.mockResolvedValue(optionsWithAmounts);
    // 默认场景（Sunny 01:20 裁定）：URL 无 ratio → 前端默认 0.5 试算，响应回显 0.5。
    fetchDashboardSummary.mockResolvedValue(summaryFixture(true, "0.5"));
    fetchDashboardProjects.mockResolvedValue(projectsFixture);
    fetchDashboardParts.mockResolvedValue(partsQtyFixture);
    fetchDashboardValuation.mockResolvedValue(valuationFixture("0.5"));
    fetchDashboardQuantityChecks.mockResolvedValue(checksFixture);
    fetchDashboardOrders.mockResolvedValue(ordersFixture);
    fetchDashboardConsumption.mockResolvedValue(consumptionFixture(true));
    fetchDashboardConsumptionIssues.mockResolvedValue(consumptionIssuesFixture);
    fetchDashboardReceipts.mockResolvedValue(receiptsFixture);
    downloadDashboardExport.mockResolvedValue({
      blob: new Blob(["csv"], { type: "text/csv" }),
      filename: "maintenance-dashboard-projects.csv",
    });
  });

  it("默认进入总览页签：五个页签齐全、M01 期间下单卡与数量卡并列（M-AC01）", async () => {
    renderPage();
    // options→挂载→取数是跨 act 的级联：显式等数据落地（骨架屏阶段元素已在，不能只等存在）
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt").textContent).toContain("3"),
      { timeout: 5000 },
    );
    const card = screen.getByTestId("kpi-receipt");
    // 五个页签齐全
    for (const label of [
      "回收与领用总览", "项目对比", "PN 排名与估价", "下单与每日消耗", "需求单核对",
    ]) {
      expect(screen.getByRole("tab", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("tab", { name: "回收与领用总览" })).toHaveAttribute("aria-selected", "true");
    // M-AC01：下单 10（2 单）、实际领用 6、实收 3 三类事实各自成卡
    expect(screen.getByTestId("kpi-orders").textContent).toContain("10");
    expect(screen.getByTestId("kpi-orders").textContent).toContain("2 张需求单");
    expect(screen.getByTestId("kpi-issued").textContent).toContain("6");
    expect(screen.getByTestId("kpi-receipt").textContent).toContain("3");
    // 默认值不入 URL（useUrlState 契约），请求侧显式带 range=7d；orders 端点不带 q/ratio
    expect(query().get("range")).toBeNull();
    expect(query().get("tab")).toBeNull();
    expect(query().get("ratio")).toBeNull();
    expect(fetchDashboardSummary).toHaveBeenCalledWith(
      { range: "7d", value_retention_ratio: "0.5" },
    );
    expect(fetchDashboardOrders).toHaveBeenCalledWith({ range: "7d" });
    // 无业务日期实收以提示条单独说明（partial-missing 状态）
    expect(screen.getByTestId("overview-notes")).toHaveTextContent("无业务日期");
    // Sunny 01:20 裁定：无显式 ratio 默认按 50% 试算（不是待试算，更不是 ¥0）
    expect(screen.queryByTestId("kpi-valuation-awaiting")).not.toBeInTheDocument();
    expect(screen.getByTestId("kpi-valuation")).toHaveTextContent("比例 50%");
    expect(screen.getByTestId("kpi-valuation")).toHaveTextContent(POLICY.policy_version);
    expect(screen.getByTestId("retention-ratio-state")).toHaveTextContent("默认试算比例 50%");
    expect(screen.getByTestId("retention-ratio-state")).toHaveTextContent("可调整");
    expect(screen.getByRole("button", { name: "恢复默认（50%）" })).toBeDisabled();
  });

  it("无金额权：金额卡/列/试算参数控件整块不渲染（不是 ¥0），估价端点不调用", async () => {
    fetchDashboardOptions.mockResolvedValue(optionsQtyOnly);
    fetchDashboardSummary.mockResolvedValue(summaryFixture(false, null));
    fetchDashboardConsumption.mockResolvedValue(consumptionFixture(false));
    renderPage();
    // M01 数量卡照常（等待级联落地，而非只等元素存在）
    await waitFor(
      () => expect(screen.getByTestId("kpi-orders").textContent).toContain("10"),
      { timeout: 5000 },
    );
    expect(screen.queryByTestId("kpi-valuation")).not.toBeInTheDocument();
    expect(screen.queryByText(/坏件试算价值/)).not.toBeInTheDocument();
    // 数量账号不见任何试算参数痕迹（R34：参数控件本身不渲染）
    expect(screen.queryByTestId("retention-ratio-control")).not.toBeInTheDocument();
    expect(screen.getByTestId("amounts-restricted-banner")).toBeInTheDocument();

    // T2 项目对比：无金额列
    fireEvent.click(screen.getByRole("tab", { name: "项目对比" }));
    await screen.findByText("项目一");
    expect(screen.queryByRole("columnheader", { name: /坏件试算价值/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /覆盖率/ })).not.toBeInTheDocument();
    expect(fetchDashboardValuation).not.toHaveBeenCalled();

    // T3 PN 排名：走数量端点，估价/参考列不存在
    fireEvent.click(screen.getByRole("tab", { name: "PN 排名与估价" }));
    await screen.findByText("MDDA-PN-1");
    expect(fetchDashboardParts).toHaveBeenCalledWith(
      expect.objectContaining({ range: "7d", sort: "bad_receipt_qty_desc", page: 1, page_size: 20 }),
    );
    expect(fetchDashboardParts).toHaveBeenCalledWith(expect.not.objectContaining({
      value_retention_ratio: expect.anything(),
    }));
    expect(fetchDashboardValuation).not.toHaveBeenCalled();
    expect(screen.queryByRole("columnheader", { name: /坏件试算价值/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /三域参考/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("amounts-hidden-note")).toBeInTheDocument();

    // 下单与每日消耗：只有数量卡/列（无成本/覆盖率/来源列头）
    fireEvent.click(screen.getByRole("tab", { name: "下单与每日消耗" }));
    await screen.findByTestId("consumption-cards");
    expect(screen.getByTestId("kpi-consumption-qty").textContent).toContain("6");
    expect(screen.queryByTestId("kpi-consumption-cost")).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /已计消耗成本/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /未计成本量/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /已计成本覆盖率/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /成本来源/ })).not.toBeInTheDocument();
    expect(screen.getByTestId("consumption-qty-only-note")).toBeInTheDocument();
  });

  it("有金额权无显式 ratio：默认按 50% 试算，URL 不写 ratio、不解释为不试算", async () => {
    renderPage("/maintenance/dashboard?tab=parts");
    await screen.findByText("MDDA-PN-1");
    expect(fetchDashboardValuation).toHaveBeenCalledWith(
      expect.objectContaining({ value_retention_ratio: "0.5" }),
    );
    expect(query().get("ratio")).toBeNull();
    const note = screen.getByTestId("valuation-policy-note");
    expect(note).toHaveTextContent("当前试算比例 50%");
    expect(note).toHaveTextContent(POLICY.policy_version);
    expect(note).toHaveTextContent(POLICY.three_domain_rule);
    expect(screen.getByTestId("retention-ratio-state")).toHaveTextContent("默认试算比例 50%");
    // 默认比例下已出试算金额：不是待试算，也不是 ¥0
    expect(screen.queryByTestId("kpi-bad-value-awaiting")).not.toBeInTheDocument();
    expect(screen.queryByTestId("part-value-awaiting")).not.toBeInTheDocument();
    // 参考价照常展示（三域：成本域即使销售无样本也独立展示）
    expect(screen.getByText("采购 ¥113.00（1 单）")).toBeInTheDocument();
    expect(screen.getByText(/领用已计成本 ¥22.60/)).toBeInTheDocument();
    expect(screen.queryByText(/市场两域均无样本/)).not.toBeInTheDocument();
  });

  it("恢复默认：自定义 30% 点「恢复默认（50%）」后回归 0.5，URL 清空显式值", async () => {
    fetchDashboardValuation.mockResolvedValue(valuationFixture("0.3"));
    renderPage("/maintenance/dashboard?tab=parts&ratio=0.3");
    await screen.findByText("MDDA-PN-1");
    expect(screen.getByTestId("retention-ratio-state")).toHaveTextContent(/当前试算比例 30/);
    expect(screen.getByRole("button", { name: "恢复默认（50%）" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "恢复默认（50%）" }));
    await waitFor(() => expect(query().get("ratio")).toBeNull());
    await waitFor(() => expect(fetchDashboardValuation).toHaveBeenLastCalledWith(
      expect.objectContaining({ value_retention_ratio: "0.5" }),
    ));
    await waitFor(() => expect(screen.getByTestId("retention-ratio-state"))
      .toHaveTextContent("默认试算比例 50%"));
    expect(screen.getByRole("button", { name: "恢复默认（50%）" })).toBeDisabled();
  });

  it("快捷 30%：请求/URL/导出携带 value_retention_ratio=0.3，三域并列展示且不相加", async () => {
    fetchDashboardValuation.mockResolvedValue(valuationFixture("0.3"));
    downloadDashboardExport.mockResolvedValue({
      blob: new Blob(["csv"], { type: "text/csv" }),
      filename: "maintenance-dashboard-parts.csv",
    });
    renderPage("/maintenance/dashboard?tab=parts");
    await screen.findByText("MDDA-PN-1");
    // 默认 50% 在显（快捷按钮来自政策 quick_options：0.5/0.3/0.2，50% 高亮）
    expect(screen.getByTestId("retention-ratio-state")).toHaveTextContent("默认试算比例 50%");
    expect(screen.getByRole("button", { name: "50%" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "30%" }));
    await waitFor(() => expect(query().get("ratio")).toBe("0.3"));
    await waitFor(() => expect(fetchDashboardValuation).toHaveBeenCalledWith(
      expect.objectContaining({ value_retention_ratio: "0.3" }),
    ));
    // 政策响应回显实际比例值；默认文案退场，切到「当前试算比例」
    await waitFor(() => expect(screen.getByTestId("valuation-policy-note"))
      .toHaveTextContent(/当前试算比例 30/));
    await waitFor(() => expect(screen.getByTestId("retention-ratio-state"))
      .toHaveTextContent(/当前试算比例 30/));
    expect(screen.queryByText(/默认试算比例/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "30%" })).toHaveAttribute("aria-pressed", "true");
    // 主展示价值 + 三域分值并列；不出现任何求和口径
    expect(screen.queryByTestId("kpi-bad-value-awaiting")).not.toBeInTheDocument();
    expect(screen.getAllByText(/采购 ¥56.5/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/领用已计成本 ¥45.2/).length).toBeGreaterThan(0);
    expect(screen.getByText("三域并列，不相加")).toBeInTheDocument();
    // 导出参数随比例
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "parts", format: "csv", value_retention_ratio: "0.3" }),
    ));
  });

  it("pending 态（市场两域无样本）与待试算分开呈现，成本域参考仍独立展示", async () => {
    // 契约 /parts pending：无 bad_estimated_value_*；references 市场两域均不可用
    const pendingValuation = {
      ...valuationFixture("0.5"),
      rows: [{
        ...PART_ROW_QTY,
        valuation: {
          state: "pending",
          estimated_bad_qty: "0.000",
          pending_bad_qty: "2.000",
          awaiting_ratio_bad_qty: "0.000",
          pending_reasons: { no_samples: "2.000" },
          coverage_pct: null,
        },
        references: {
          purchase: { available: false, unavailable_reason: "no_samples" },
          sales: { available: false, unavailable_reason: "no_samples" },
          cost: referencesFixture.cost,
          chosen: null, chosen_reason: null,
        },
      }],
    };
    fetchDashboardValuation.mockResolvedValue(pendingValuation);
    renderPage("/maintenance/dashboard?tab=parts&ratio=0.5");
    await screen.findByText("MDDA-PN-1");
    // 主价值列与三域列都是「待估（无可用样本）」，不是「待试算（未设比例）」
    expect(screen.getByTestId("part-value-pending")).toHaveTextContent("待估");
    expect(screen.getByTestId("part-domains-pending")).toHaveTextContent("待估");
    expect(screen.queryByTestId("part-value-awaiting")).not.toBeInTheDocument();
    expect(screen.queryByTestId("part-domains-awaiting")).not.toBeInTheDocument();
    // 市场两域均无样本 → 主展示回退说明出现；成本域参考独立展示
    expect(screen.getByText(/市场两域均无样本/)).toBeInTheDocument();
    expect(screen.getByText(/领用已计成本 ¥22.60/)).toBeInTheDocument();
    expect(screen.getAllByText(/窗口内无成交样本/).length).toBe(2);
  });

  it("显式 0 是合法比例且不被默认 50% 覆盖（0=0，不是待试算）", async () => {
    fetchDashboardSummary.mockResolvedValue(summaryFixture(true, "0"));
    fetchDashboardValuation.mockResolvedValue(valuationFixture("0"));
    renderPage("/maintenance/dashboard?tab=parts&ratio=0");
    await screen.findByText("MDDA-PN-1");
    expect(screen.getByTestId("retention-ratio-state")).toHaveTextContent("当前试算比例 0%");
    expect(fetchDashboardValuation).toHaveBeenCalledWith(
      expect.objectContaining({ value_retention_ratio: "0" }),
    );
    // 显式 0 保留在 URL（分享一致），不被默认覆盖
    expect(query().get("ratio")).toBe("0");
    expect(screen.queryByText(/默认试算比例/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("kpi-bad-value-awaiting")).not.toBeInTheDocument();
  });

  it("下单与每日消耗（M01/M03）：已知小计与未知不混，日表可下钻核对（M-AC02）", async () => {
    renderPage("/maintenance/dashboard?tab=consumption");
    await screen.findByTestId("consumption-cards");
    // M01 卡片与项目表
    expect(screen.getByTestId("kpi-order-qty").textContent).toContain("10");
    expect(screen.getByTestId("kpi-order-count").textContent).toContain("2");
    expect(fetchDashboardOrders).toHaveBeenCalledWith({ range: "7d" });
    expect(fetchDashboardConsumption).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1, page_size: 20 }),
    );
    expect(fetchDashboardConsumption).toHaveBeenCalledWith(expect.not.objectContaining({
      q: expect.anything(),
    }));
    // M03：数量全计；金额只报已知小计 + 未计成本量/覆盖率/来源（M-AC02）
    expect(screen.getByTestId("kpi-consumption-qty").textContent).toContain("6");
    expect(screen.getByTestId("kpi-consumption-cost").textContent).toContain("90.4");
    expect(screen.getByTestId("kpi-consumption-cost").textContent).toContain("已知小计");
    expect(screen.getByTestId("kpi-consumption-uncosted").textContent).toContain("2");
    expect(screen.getByTestId("kpi-consumption-coverage").textContent).toContain("66.7");
    // 来源码 → 复用面板可信中文映射（与后端枚举一一对应）；未收录码保留原码并标注
    expect(screen.getByTestId("kpi-consumption-sources").textContent).toContain("直接采购价");
    expect(screen.getByTestId("kpi-consumption-sources").textContent).toContain("7天采购窗口");
    expect(screen.getByTestId("kpi-consumption-sources").textContent).toContain("future_algo（未收录来源）");
    // 日表下钻：按 issue_date × project_id 请求行级明细
    fireEvent.click(screen.getByRole("button", { name: "领用明细" }));
    await waitFor(() => expect(fetchDashboardConsumptionIssues).toHaveBeenCalledWith(
      expect.objectContaining({
        issue_date: "2026-10-20", project_id: "p1", page: 1, page_size: 50,
      }),
    ));
    expect(await screen.findByText("MDDA-PN-2")).toBeInTheDocument();
    // 两条领用行数量均计（4 + 2）；仅一条有已计成本，另一条标「未计成本」；
    // 明细 Drawer 的成本来源同样走可信中文映射（真实 DB 枚举码 direct）
    expect(screen.getByText("MDDA-PN-1")).toBeInTheDocument();
    expect(screen.getAllByText("直接采购价").length).toBeGreaterThan(0);
    expect(screen.getAllByText("未计成本").length).toBeGreaterThan(0);
  });

  it("下单与每日消耗：三个导出走完整筛选集（orders/consumption_daily/consumption_issues）", async () => {
    renderPage("/maintenance/dashboard?tab=consumption");
    await screen.findByTestId("consumption-cards");
    const ordersCard = screen.getByTestId("orders-cards").closest(".ant-card") as HTMLElement;
    fireEvent.click(within(ordersCard).getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "orders", format: "csv", range: "7d" }),
    ));
    const consumptionCard = screen.getByTestId("consumption-cards").closest(".ant-card") as HTMLElement;
    fireEvent.click(within(consumptionCard).getAllByRole("button", { name: "导出 CSV" })[0]);
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "consumption_daily", format: "csv" }),
    ));
    fireEvent.click(within(consumptionCard).getAllByRole("button", { name: "导出 CSV" })[1]);
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "consumption_issues", format: "csv" }),
    ));
  });

  it("会话令牌在响应落地前变更：迟到响应被丢弃，不渲染旧会话数据（DL-010）", async () => {
    localStorage.setItem("token", "token-a");
    let resolveSummary!: (value: SummaryResponse) => void;
    fetchDashboardSummary.mockImplementation(
      () => new Promise<SummaryResponse>((resolve) => { resolveSummary = resolve; }),
    );
    renderPage();
    // 总览经 Promise.all 取数：summary 挂起期间两卡都还在骨架屏
    await screen.findByTestId("overview-cards");
    expect(screen.getByTestId("kpi-receipt").textContent).not.toContain("3");
    // 落地前切换账号（token 变化）：旧会话响应不得应用（数量卡也不落地）
    localStorage.setItem("token", "token-b");
    await act(async () => { resolveSummary(summaryFixture(true, "0.5")); });
    expect(screen.getByTestId("kpi-receipt").textContent).not.toContain("3");
    expect(screen.getByTestId("kpi-orders").textContent).not.toContain("10");
    expect(screen.queryByTestId("kpi-valuation")).not.toBeInTheDocument();
  });

  it("T4 当前累计：N/R/差额/超收齐备，日期只选单不截断整单 R，q 不下发给该端点", async () => {
    renderPage("/maintenance/dashboard?tab=checks&q=ST");
    await screen.findByText("DD001");
    expect(screen.getByTestId("checks-note")).toHaveTextContent("当前累计");
    expect(screen.getByTestId("checks-note")).toHaveTextContent("不发布返还状态");
    expect(screen.getByTestId("checks-cumulative-note")).toHaveTextContent("不因期间或 PN 条件截断");
    expect(screen.getByTestId("checks-cumulative-note")).toHaveTextContent("不互相抵消");
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
    await waitFor(() => expect(fetchDashboardSummary).toHaveBeenLastCalledWith(
      { range: "30d", value_retention_ratio: "0.5" },
    ));
    await waitFor(() => expect(fetchDashboardOrders).toHaveBeenLastCalledWith({ range: "30d" }));
  });

  it("T2 项目对比：期间下单列并入对比，导出 CSV 用当前筛选集", async () => {
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    // M01 并入：项目一期间下单 8（1 单）
    expect(screen.getByRole("columnheader", { name: /期间下单量/ })).toBeInTheDocument();
    expect(screen.getAllByText("8").length).toBeGreaterThan(0);
    expect(fetchDashboardOrders).toHaveBeenCalledWith({ range: "7d" });
    // 已知后端缺口（needs #1）：/projects 暂未接入比例参数 → 显式说明 + 持续下发
    expect(fetchDashboardProjects).toHaveBeenCalledWith(
      expect.objectContaining({ value_retention_ratio: "0.5" }),
    );
    expect(screen.getByTestId("projects-ratio-gap-note")).toHaveTextContent("后端缺口说明");
    expect(screen.getByTestId("projects-ratio-gap-note")).toHaveTextContent("待试算");
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "projects", format: "csv", range: "7d", value_retention_ratio: "0.5",
      }),
    ));
    await waitFor(() => expect(screen.getByText(/已导出 maintenance-dashboard-projects\.csv/)).toBeInTheDocument());
    // 行内返件明细（M-AC09）：项目维度求交下钻
    fireEvent.click(screen.getByRole("button", { name: "查看 项目一 返件明细" }));
    await waitFor(() => expect(fetchDashboardReceipts).toHaveBeenCalledWith(
      expect.objectContaining({ project: "p1", sort: "occurred_desc", page: 1, page_size: 20 }),
    ));
  });

  it("M-AC09 总览下钻：Top 项目点击开返件明细 Drawer，导出 receipts 完整交集", async () => {
    renderPage();
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt").textContent).toContain("3"),
      { timeout: 5000 },
    );
    fireEvent.click(screen.getByRole("button", { name: "查看 项目一 返件明细" }));
    await waitFor(() => expect(fetchDashboardReceipts).toHaveBeenCalledWith(
      expect.objectContaining({
        project: "p1", value_retention_ratio: "0.5",
        sort: "occurred_desc", page: 1, page_size: 20,
      }),
    ));
    expect(await screen.findByText("返件明细：项目一")).toBeInTheDocument();
    expect(await screen.findByText("MDDA-PN-2")).toBeInTheDocument();
    // 数量列（件况/需复核）与金额列（试算价值随 amountsVisible）都在
    expect(screen.getByText("坏品")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /试算价值/ })).toBeInTheDocument();
    // 未关联需求单的行明确标注，不猜来源
    expect(screen.getByText("未关联")).toBeInTheDocument();
    // 导出走该交集完整集合（kind=receipts，携带求交参数与比例）
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "receipts", format: "csv", project: "p1", value_retention_ratio: "0.5",
      }),
    ));
  });

  it("M-AC09 趋势图日期点击下钻：原筛选 ∩ 点击日（custom 单日区间）", async () => {
    renderPage();
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt").textContent).toContain("3"),
      { timeout: 5000 },
    );
    // mock 图表以 dataIndex=0 触发 click → trend[0].date = 2026-10-18
    fireEvent.click(screen.getByRole("button", { name: "触发图表点击" }));
    await waitFor(() => expect(fetchDashboardReceipts).toHaveBeenCalledWith(
      expect.objectContaining({
        range: "custom", date_from: "2026-10-18", date_to: "2026-10-18",
        value_retention_ratio: "0.5",
      }),
    ));
    expect(await screen.findByText(/返件明细：2026-10-18/)).toBeInTheDocument();
  });

  it("M-AC09 下钻是求交不是替换：多项目选集点击其一收敛为该项目（DL-022）", async () => {
    renderPage("/maintenance/dashboard?project=p1,p2");
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt").textContent).toContain("3"),
      { timeout: 5000 },
    );
    fireEvent.click(screen.getByRole("button", { name: "查看 项目一 返件明细" }));
    await waitFor(() => expect(fetchDashboardReceipts).toHaveBeenCalledWith(
      expect.objectContaining({ project: "p1" }),
    ));
    // 绝不带原选集（p1,p2）或其他项目（p2）扩大范围
    expect(fetchDashboardReceipts).not.toHaveBeenCalledWith(
      expect.objectContaining({ project: "p1,p2" }),
    );
    expect(fetchDashboardReceipts).not.toHaveBeenCalledWith(
      expect.objectContaining({ project: "p2" }),
    );
  });

  it("M-AC09 PN 下钻：parts 行「返件明细」按 q=PN 求交；关闭回原视图", async () => {
    renderPage("/maintenance/dashboard?tab=parts");
    await screen.findByText("MDDA-PN-1");
    fireEvent.click(screen.getByRole("button", { name: "查看 MDDA-PN-1 返件明细" }));
    await waitFor(() => expect(fetchDashboardReceipts).toHaveBeenCalledWith(
      expect.objectContaining({ q: "MDDA-PN-1", value_retention_ratio: "0.5" }),
    ));
    expect(await screen.findByText(/返件明细：MDDA-PN-1/)).toBeInTheDocument();
    // 关闭 Drawer 回到原页签视图（返回状态保留：URL 未被改写）
    fireEvent.click(document.querySelector(".ant-drawer-close")!);
    await waitFor(() => expect(screen.queryByText(/返件明细：MDDA-PN-1/)).not.toBeInTheDocument());
    expect(screen.getByText("MDDA-PN-1")).toBeInTheDocument();
    expect(query().get("tab")).toBe("parts");
    expect(query().get("q")).toBeNull();
  });

  it("M-AC09 返件明细 Drawer 随权限：数量账号无金额列、请求不带比例参数", async () => {
    fetchDashboardOptions.mockResolvedValue(optionsQtyOnly);
    fetchDashboardSummary.mockResolvedValue(summaryFixture(false, null));
    renderPage();
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt").textContent).toContain("3"),
      { timeout: 5000 },
    );
    fireEvent.click(screen.getByRole("button", { name: "查看 项目一 返件明细" }));
    await waitFor(() => expect(fetchDashboardReceipts).toHaveBeenCalledWith(
      expect.not.objectContaining({ value_retention_ratio: expect.anything() }),
    ));
    expect(await screen.findByText("MDDA-PN-2")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /试算价值/ })).not.toBeInTheDocument();
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
