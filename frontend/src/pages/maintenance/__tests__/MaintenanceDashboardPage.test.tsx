/** 维保数据看板页（R33/D-0022 + R34/D-0024）：五页签、金额遮蔽（不渲染而非 ¥0）、
 * 试算比例（默认 50% / 显式 0 / 自定义）、三域并列不相加、期间下单与每日消耗
 * （M01/M03）、返件明细下钻（M-AC09）、会话守卫（DL-010）、T4 当前累计口径、
 * 导出与失败重试；coveragefix 轮（契约 v2）：三域返件估值覆盖（B-F07）、
 * 单位分桶不混算（B-F09）、全待估≠¥0（B-F06）、未计成本金额未知（B-F11）、
 * 无日期下单桶（B-F12）、quantity-checks q 透传整单选择（B-F10）。 */
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
// jsdom 无 canvas：mock 图表壳；暴露 onEvents.click 触发按钮（趋势图日期下钻）
// 与系列名（B-F09 按单位拆系列断言）。
vi.mock("../../../components/charts/EChartContainer", () => ({
  default: ({ empty, emptyText, onEvents, testId, option }: {
    empty?: boolean; emptyText?: string; testId?: string;
    onEvents?: Record<string, (params: unknown) => void>;
    option?: { series?: { name?: string }[] };
  }) => (empty
    ? <div>{emptyText ?? "空"}</div>
    : (
      <div data-testid={testId ?? "chart"}>
        {(option?.series ?? []).map((series, index) => (
          series.name ? <span key={index}>{series.name}</span> : null
        ))}
        {onEvents?.click ? (
          <button type="button" onClick={() => onEvents.click({ dataIndex: 0 })}>
            触发图表点击
          </button>
        ) : null}
      </div>
    )),
}));

import MaintenanceDashboardPage from "../MaintenanceDashboardPage";
import { DomainCoverageLines, ReferenceLine } from "../dashboard/dashboardShared";
import type {
  DashboardOptions,
  DomainValuationPayload,
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

/** 契约 v2 §2（B-F07）三域返件估值覆盖锚点：采购/销售 已估 3/4=75%、
 * 成本域 1/4=25%（coverage_basis=bad_receipts）；references.cost 的
 * coverage_pct=10%（issued_lines 领用已计口径）是另一个事实，不得混用。 */
const DOMAIN_COVERAGE_FIXTURE = {
  purchase: {
    value_inc_tax: "56.50", value_ex_tax: "50.00",
    bad_qty: "4.000", estimated_bad_qty: "3.000", pending_bad_qty: "1.000",
    awaiting_ratio_bad_qty: "0.000", coverage_pct: 75, pending_reasons: {},
    unit: "件",
    unit_breakdown: [{
      unit: "件", bad_qty: "4.000", estimated_bad_qty: "3.000",
      pending_bad_qty: "1.000", awaiting_ratio_bad_qty: "0.000", coverage_pct: 75,
    }],
    coverage_basis: "bad_receipts" as const,
  },
  sales: {
    value_inc_tax: "28.25", value_ex_tax: "25.00",
    bad_qty: "4.000", estimated_bad_qty: "3.000", pending_bad_qty: "1.000",
    awaiting_ratio_bad_qty: "0.000", coverage_pct: 75, pending_reasons: {},
    unit: "件",
    unit_breakdown: [{
      unit: "件", bad_qty: "4.000", estimated_bad_qty: "3.000",
      pending_bad_qty: "1.000", awaiting_ratio_bad_qty: "0.000", coverage_pct: 75,
    }],
    coverage_basis: "bad_receipts" as const,
  },
  cost: {
    value_inc_tax: "11.30", value_ex_tax: "10.00",
    bad_qty: "4.000", estimated_bad_qty: "1.000", pending_bad_qty: "3.000",
    awaiting_ratio_bad_qty: "0.000", coverage_pct: 25,
    pending_reasons: { cost_not_recorded: "3.000" },
    unit: "件",
    unit_breakdown: [{
      unit: "件", bad_qty: "4.000", estimated_bad_qty: "1.000",
      pending_bad_qty: "3.000", awaiting_ratio_bad_qty: "0.000", coverage_pct: 25,
    }],
    coverage_basis: "bad_receipts" as const,
  },
};

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
      domains: DOMAIN_COVERAGE_FIXTURE,
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
    // 契约 v2 §2：领用已计覆盖（issued_lines 口径，如 1/10 领用行）——
    // 与 domains.cost 的返件估值覆盖（bad_receipts，1/4=25%）是两个事实。
    coverage_pct: 10, coverage_basis: "issued_lines",
    sources: ["需求成本"], latest_issue_date: "2026-10-20",
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
    partValuation.domains = DOMAIN_COVERAGE_FIXTURE;
  }
  const cards: Record<string, unknown> = {
    estimated_bad_qty: ratio === null ? "0.000" : "1.000",
    pending_bad_qty: "1.000",
    awaiting_ratio_bad_qty: ratio === null ? "1.000" : "0.000",
    coverage_pct: ratio === null ? null : 50,
  };
  if (ratio !== null) {
    cards.bad_estimated_value = { inc_tax: "56.50", ex_tax: "50.00" };
    // 契约 v2 §2：/valuation 总卡也带三域覆盖（不只行级）。
    cards.domains = DOMAIN_COVERAGE_FIXTURE;
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
      part_id: 1, pn: "MDDA-PN-1", qty: "4.000", unit: "件",
      cost_source: "direct", algorithm_version: "mc-v3", reference_side: "purchase",
      unit_cost_ex_tax: "20.00", unit_cost_inc_tax: "22.60",
      cost_amount_ex_tax: "80.00", cost_amount_inc_tax: "90.40",
    },
    {
      issue_line_id: 12, issue_id: 1, issue_no: "LY-001", issue_date: "2026-10-20",
      project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
      part_id: 2, pn: "MDDA-PN-2", qty: "2.000", unit: null,
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

// ---- coveragefix 轮新夹具（契约 lane-b-backend-coverage-contract v2）----

/** B-F06：全待估项目行 state=pending、值键不存在（不是 ready + ¥0.00）。 */
const projectsPendingFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [{
    ...PROJECT_ROW,
    valuation: {
      state: "pending",
      estimated_bad_qty: "0.000",
      pending_bad_qty: "2.000",
      awaiting_ratio_bad_qty: "0.000",
      coverage_pct: 0,
    },
  }],
  pagination: { page: 1, page_size: 20, total: 1 },
};

/** B-F09/B-F07：项目行混合单位（平面 null + unit_breakdown）+ 三域覆盖键。 */
const projectsMixedFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  rows: [{
    ...PROJECT_ROW,
    unit: null,
    unit_breakdown: [
      { unit: "件", receipt_qty: "3.000", bad_receipt_qty: "2.000",
        scrap_receipt_qty: "1.000", issued_qty: "6.000", exempt_issued_qty: "1.000",
        pending_requirement_qty: "0.000", unlinked_receipt_qty: "1.000" },
      { unit: "台", receipt_qty: "2.000", bad_receipt_qty: "1.000",
        scrap_receipt_qty: "0.000", issued_qty: "2.000", exempt_issued_qty: "0.000",
        pending_requirement_qty: "0.000", unlinked_receipt_qty: "0.000" },
    ],
    receipt_qty: null, bad_receipt_qty: null, scrap_receipt_qty: null,
    issued_qty: null, exempt_issued_qty: null, pending_requirement_qty: null,
    unlinked_receipt_qty: null,
    return_rate_pct: null, rate_numerator: null, rate_denominator: null,
    valuation: {
      state: "ready",
      estimated_bad_qty: "3.000",
      pending_bad_qty: "1.000",
      awaiting_ratio_bad_qty: "0.000",
      coverage_pct: 75,
      bad_estimated_value_inc_tax: "56.50",
      bad_estimated_value_ex_tax: "50.00",
      domains: DOMAIN_COVERAGE_FIXTURE,
    },
  }],
  pagination: { page: 1, page_size: 20, total: 1 },
};

/** B-F09/B-F12：下单混合单位（平面 null + 分桶，不并成 17）+ 无日期桶单列。 */
const ordersMixedFixture: OrdersResponse = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  cards: {
    order_qty: null, order_count: 2, unit: null,
    unit_breakdown: [
      { unit: "件", order_qty: "15.000" },
      { unit: "台", order_qty: "2.000" },
    ],
    undated_orders: {
      order_qty: "7.000", order_count: 1,
      note: "无制单日期下单未计入期间（不猜日期）",
    },
  },
  rows: [{
    project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
    business_type: "spare",
    order_qty: null, unit: null,
    unit_breakdown: [
      { unit: "件", order_qty: "8.000" },
      { unit: "台", order_qty: "2.000" },
    ],
    order_count: 1, undated_order_qty: "7.000", undated_order_count: 1,
  }],
};

/** B-F09：消耗混合单位（件 2 已计 100% / 台 3 未计 0%）：平面 null，
 * 金额 22.60 是跨单位同币种小计（金额例外），数量/覆盖率绝不合并。 */
const consumptionMixedFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  cards: {
    issued_qty: null, unit: null,
    unit_breakdown: [
      { unit: "件", issued_qty: "2.000", costed_qty: "2.000", uncosted_qty: "0.000",
        coverage_pct: 100, cost_amount_ex_tax: "20.00", cost_amount_inc_tax: "22.60" },
      { unit: "台", issued_qty: "3.000", costed_qty: "0.000", uncosted_qty: "3.000",
        coverage_pct: 0 },
    ],
    cost_amount_ex_tax: "20.00", cost_amount_inc_tax: "22.60",
    costed_qty: null, uncosted_qty: null, coverage_pct: null,
    sources: ["direct"],
    note: "只读既有已计成本（可能含估算）；不回算、不减返件估值、不覆盖成本链",
  },
  rows: [{
    issue_date: "2026-10-20", project_id: "p1", project_code: "MDD-P1",
    display_name: "项目一", issued_qty: null, unit: null,
    unit_breakdown: [
      { unit: "件", issued_qty: "2.000", costed_qty: "2.000", uncosted_qty: "0.000",
        coverage_pct: 100, cost_amount_ex_tax: "20.00", cost_amount_inc_tax: "22.60" },
      { unit: "台", issued_qty: "3.000", costed_qty: "0.000", uncosted_qty: "3.000",
        coverage_pct: 0 },
    ],
    line_count: 2,
    cost_amount_ex_tax: "20.00", cost_amount_inc_tax: "22.60",
    costed_qty: null, uncosted_qty: null, coverage_pct: null,
    sources: ["direct"],
  }],
  pagination: { page: 1, page_size: 20, total: 1 },
};

/** B-F11：全部未计成本 → 金额键不存在（未知金额，不是 ¥0.00）；
 * 数量/未计成本量/覆盖 0%（真实 0）照给。 */
const consumptionUncostedFixture = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  cards: {
    issued_qty: "3.000", unit: "台",
    unit_breakdown: [{
      unit: "台", issued_qty: "3.000", costed_qty: "0.000",
      uncosted_qty: "3.000", coverage_pct: 0,
    }],
    costed_qty: "0.000", uncosted_qty: "3.000", coverage_pct: 0,
    sources: [],
    note: "只读既有已计成本；不回算、不减返件估值、不覆盖成本链",
  },
  rows: [{
    issue_date: "2026-10-21", project_id: "p1", project_code: "MDD-P1",
    display_name: "项目一", issued_qty: "3.000", unit: "台",
    unit_breakdown: [{
      unit: "台", issued_qty: "3.000", costed_qty: "0.000",
      uncosted_qty: "3.000", coverage_pct: 0,
    }],
    line_count: 1, costed_qty: "0.000", uncosted_qty: "3.000", coverage_pct: 0,
    sources: [],
  }],
  pagination: { page: 1, page_size: 20, total: 1 },
};

/** B-F09：总览混合单位——卡片平面 null + 分桶；趋势/件况按单位拆系列。 */
const summaryMixedFixture: SummaryResponse = {
  range: { start: "2026-10-15", end: "2026-10-21" },
  cards: {
    receipt_qty: {
      total: null, part: null, machine: null,
      unit_breakdown: [
        { unit: "件", total: "2.000", part: "2.000", machine: "0.000" },
        { unit: "台", total: "1.000", part: "0.000", machine: "1.000" },
      ],
    },
    bad_receipt_qty: {
      total: null, scrap: null,
      unit_breakdown: [
        { unit: "件", total: "1.000", scrap: "0.000" },
        { unit: "台", total: "1.000", scrap: "0.000" },
      ],
    },
    issued_qty: {
      total: null, note: "实际领用量（未采集件况）",
      unit_breakdown: [
        { unit: "件", total: "4.000" },
        { unit: "台", total: "2.000" },
      ],
    },
    return_rate_pct: null, rate_numerator: null, rate_denominator: null,
    project_count: 2,
    valuation: {
      state: "ready",
      estimated_bad_qty: null, pending_bad_qty: null, awaiting_ratio_bad_qty: null,
      coverage_pct: null,
      unit_breakdown: [{
        unit: "件", bad_qty: "4.000", estimated_bad_qty: "3.000",
        pending_bad_qty: "1.000", awaiting_ratio_bad_qty: "0.000", coverage_pct: 75,
      }, {
        unit: "台", bad_qty: "2.000", estimated_bad_qty: "1.000",
        pending_bad_qty: "1.000", awaiting_ratio_bad_qty: "0.000", coverage_pct: 50,
      }],
      retention_ratio: "0.5",
      domains: DOMAIN_COVERAGE_FIXTURE,
      policy_version: POLICY.policy_version,
      note: POLICY.notes,
      bad_estimated_value: { inc_tax: "56.50", ex_tax: "50.00" },
      finished_estimated_value: { inc_tax: "0.00", ex_tax: "0.00" },
    },
  },
  trend: [{
    date: "2026-10-18", receipt_qty: null, issued_qty: null,
    unit_breakdown: [
      { unit: "件", receipt_qty: "2.000", issued_qty: "4.000" },
      { unit: "台", receipt_qty: "1.000", issued_qty: "2.000" },
    ],
  }],
  condition_structure: [{
    test_result: "坏品", qty: null,
    unit_breakdown: [
      { unit: "件", qty: "1.000" },
      { unit: "台", qty: "1.000" },
    ],
  }],
  top_projects: [{
    project_id: "p1", project_code: "MDD-P1", display_name: "项目一",
    receipt_qty: null, bad_receipt_qty: null, issued_qty: null,
    unit_breakdown: [
      { unit: "件", receipt_qty: "2.000", bad_receipt_qty: "1.000", issued_qty: "4.000" },
      { unit: "台", receipt_qty: "1.000", bad_receipt_qty: "1.000", issued_qty: "2.000" },
    ],
  }],
  notes: [],
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
    // 参考价照常展示（三域：成本域即使销售无样本也独立展示）；
    // B2-F01：参考双税并列（两侧直接用后端原值，不互相推算）
    expect(screen.getByText("采购 ¥113.00（含税）／¥100.00（不含税）（1 单）")).toBeInTheDocument();
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
    // （成本域估值标签按契约 v2 §2 独立命名，与三域参考列的领用已计成本区分）
    expect(screen.queryByTestId("kpi-bad-value-awaiting")).not.toBeInTheDocument();
    expect(screen.getAllByText(/采购 ¥56.5/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/成本域 ¥11.3/).length).toBeGreaterThan(0);
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

  it("会话令牌在响应落地前变更：迟到响应被丢弃并整页失活，旧上下文清除（DL-010）", async () => {
    localStorage.setItem("token", "token-a");
    let resolveSummary!: (value: SummaryResponse) => void;
    fetchDashboardSummary.mockImplementation(
      () => new Promise<SummaryResponse>((resolve) => { resolveSummary = resolve; }),
    );
    renderPage();
    // 总览经 Promise.all 取数：summary 挂起期间两卡都还在骨架屏
    await screen.findByTestId("overview-cards");
    expect(screen.getByTestId("kpi-receipt").textContent).not.toContain("3");
    // 落地前切换账号（token 变化）：旧会话响应不得应用（数量卡也不落地），
    // 且页面失活——旧会话已渲染的金额/项目上下文随页签一并清除。
    localStorage.setItem("token", "token-b");
    await act(async () => { resolveSummary(summaryFixture(true, "0.5")); });
    await waitFor(
      () => expect(screen.getByTestId("session-stale-alert")).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(screen.queryByTestId("kpi-receipt")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kpi-orders")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kpi-valuation")).not.toBeInTheDocument();
  });

  it("T4 当前累计（B-F10）：q 透传选择含该 PN 的整张需求单；N/R 整单不截断；导出同参", async () => {
    renderPage("/maintenance/dashboard?tab=checks&q=ST");
    await screen.findByText("DD001", {}, { timeout: 5000 });
    expect(screen.getByTestId("checks-note")).toHaveTextContent("当前累计");
    expect(screen.getByTestId("checks-note")).toHaveTextContent("不发布返还状态");
    expect(screen.getByTestId("checks-cumulative-note")).toHaveTextContent("整张需求单");
    expect(screen.getByTestId("checks-cumulative-note")).toHaveTextContent("不因期间或 PN 条件截断");
    expect(screen.getByTestId("checks-cumulative-note")).toHaveTextContent("不互相抵消");
    expect(screen.getByText("需求量 N 合计")).toBeInTheDocument();
    expect(screen.getByText("未关联需求单的实收（当前累计）")).toBeInTheDocument();
    expect(screen.getByText(/项目二（MDD-P2）/)).toBeInTheDocument();
    // B-F08：未关联桶按当前授权范围返回（受限但非空范围也有），不摊入需求单行
    expect(screen.getByText(/当前授权范围内未关联需求单的实收/)).toBeInTheDocument();
    for (const forbidden of ["欠返", "已返", "逾期", "返还状态："]) {
      expect(screen.queryByText(new RegExp(forbidden))).not.toBeInTheDocument();
    }
    // B-F10：q 下发给 quantity-checks（选择整单，不是局部裁剪 N/R）；
    // 搜索框在该页签可用（不再剥离 q）
    expect(fetchDashboardQuantityChecks).toHaveBeenCalledWith(
      expect.objectContaining({ q: "ST" }),
    );
    expect(screen.getByPlaceholderText("搜 PN / 描述")).toBeInTheDocument();
    // 导出与 API 同一筛选集（含 q）
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "quantity_checks", format: "csv", q: "ST" }),
    ));
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
    // 比例参数照常下发（默认 0.5）：后端已接入该参数（独立复核实测
    // 默认 .5 / 显式 0 / 自定义 0.2 在表与 CSV/XLSX 一致），无缺口可说明
    expect(fetchDashboardProjects).toHaveBeenCalledWith(
      expect.objectContaining({ value_retention_ratio: "0.5" }),
    );
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

  // ---- 会话所有权（独立 B 首轮 B-F03/B-F04）：请求前与落地前都绑定组件所属会话 ----
  // 复现方式与独立探针一致：同 tab 静默改 localStorage（不触发 storage 事件/重渲染）。

  it("B-F03：options 迟到且已换号 → 旧 amounts 控件不得落地，整页失活且可刷新恢复", async () => {
    localStorage.setItem("token", "token-a");
    let resolveOptions!: (value: DashboardOptions) => void;
    fetchDashboardOptions.mockImplementationOnce(
      () => new Promise<DashboardOptions>((resolve) => { resolveOptions = resolve; }),
    );
    renderPage();
    // 静默换号后 A 的 options 才返回（迟到）
    localStorage.setItem("token", "token-b");
    await act(async () => { resolveOptions(optionsWithAmounts); });
    // A 会话的金额控件 / 项目上下文不得渲染，也不得触发旧会话的业务请求
    expect(screen.queryByTestId("retention-ratio-control")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kpi-receipt")).not.toBeInTheDocument();
    expect(fetchDashboardSummary).not.toHaveBeenCalled();
    expect(fetchDashboardProjects).not.toHaveBeenCalled();
    // 检测到失配：整页转入会话失活（清除旧上下文），不是静默卡骨架屏
    const stale = await waitFor(() => {
      expect(screen.getByTestId("session-stale-alert")).toBeInTheDocument();
      return screen.getByTestId("session-stale-alert");
    }, { timeout: 5000 });
    // 失活不是禁用功能：按当前会话刷新即恢复（正向对照）
    fireEvent.click(within(stale).getByRole("button", { name: /刷\s*新/ }));
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt").textContent).toContain("3"),
      { timeout: 5000 },
    );
    expect(screen.getByTestId("retention-ratio-control")).toBeInTheDocument();
    expect(screen.queryByTestId("session-stale-alert")).not.toBeInTheDocument();
  });

  it("B-F04：A 挂载的导出按钮换号后点击 → 请求前拒绝，不发旧组件请求", async () => {
    localStorage.setItem("token", "token-a");
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一"); // options + projects 全部在 A 会话正常落地
    localStorage.setItem("token", "token-b"); // 静默换号
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(
      () => expect(screen.getByTestId("session-stale-alert")).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(downloadDashboardExport).not.toHaveBeenCalled();
  });

  it("B-F04：Drawer 内导出按钮换号后点击 → 同样不发旧组件请求", async () => {
    localStorage.setItem("token", "token-a");
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    fireEvent.click(screen.getByRole("button", { name: "查看 项目一 返件明细" }));
    expect(await screen.findByText("返件明细：项目一")).toBeInTheDocument();
    localStorage.setItem("token", "token-b");
    const drawer = document.querySelector(".ant-drawer") as HTMLElement;
    fireEvent.click(within(drawer).getByRole("button", { name: "导出 CSV" }));
    await waitFor(
      () => expect(screen.getByTestId("session-stale-alert")).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(downloadDashboardExport).not.toHaveBeenCalled();
  });

  it("B-F04：换号后新开返件明细 Drawer → 行级请求同样不发（下钻也绑定所属会话）", async () => {
    localStorage.setItem("token", "token-a");
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    localStorage.setItem("token", "token-b");
    fireEvent.click(screen.getByRole("button", { name: "查看 项目一 返件明细" }));
    await waitFor(
      () => expect(screen.getByTestId("session-stale-alert")).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(fetchDashboardReceipts).not.toHaveBeenCalled();
  });

  it("下载落地再次校验：请求发出后换号 → blob 不落盘、无成功提示", async () => {
    localStorage.setItem("token", "token-a");
    let resolveExport!: (value: { blob: Blob; filename: string }) => void;
    downloadDashboardExport.mockImplementationOnce(
      () => new Promise<{ blob: Blob; filename: string }>((resolve) => {
        resolveExport = resolve;
      }),
    );
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
    await waitFor(() => expect(downloadDashboardExport).toHaveBeenCalledTimes(1));
    // 请求在途时换号：响应落地前再次校验，旧会话数据不落盘
    localStorage.setItem("token", "token-b");
    await act(async () => {
      resolveExport({ blob: new Blob(["csv"], { type: "text/csv" }), filename: "x.csv" });
    });
    expect(screen.queryByText(/已导出/)).not.toBeInTheDocument();
  });

  // ---- coveragefix（契约 lane-b-backend-coverage-contract v2：B-F06~B-F12）----

  it("B-F06：全待估项目 state=pending、无值键（待估，不是 ready + ¥0.00）", async () => {
    fetchDashboardProjects.mockResolvedValueOnce(projectsPendingFixture);
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    // 值键不存在 → 待估占位（绝不是 TaxMoney 渲染的 ¥0.00）
    expect(screen.getByTestId("project-valuation-pending")).toHaveTextContent("待估");
    expect(screen.queryByTestId("project-valuation-awaiting")).not.toBeInTheDocument();
    expect(screen.queryByText(/¥\s*0\.00/)).not.toBeInTheDocument();
    // 全待估的覆盖率是真实 0%（分母 2、已估 0），不是「无率」隐藏
    expect(screen.getByTestId("project-coverage")).toHaveTextContent("0%");
  });

  it("B-F07：三域返件估值覆盖按域显示（采购/销售 75%、成本域 25%），与领用已计覆盖 10% 两口径并存", async () => {
    renderPage("/maintenance/dashboard?tab=parts");
    await screen.findByText("MDDA-PN-1");
    // 三域试算价值列：分值 + 已估/待估/覆盖（bad_receipts 返件估值口径）
    expect(screen.getAllByText(/采购 ¥56\.5/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/已估 3 \/ 待估 1/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/覆盖 75%（返件估值口径）/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/成本域 ¥11\.3/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/已估 1 \/ 待估 3/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/覆盖 25%（返件估值口径）/).length).toBeGreaterThan(0);
    // 三域参考列：领用已计覆盖是 issued_lines 口径（10%），不得冒充返件估值覆盖
    expect(screen.getAllByText(/领用已计成本 ¥22\.60/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/领用已计覆盖 10%/).length).toBeGreaterThan(0);
    // /valuation 总卡也呈现三域覆盖（cards.domains 不只写在类型里）
    expect(screen.getAllByText(/覆盖 75%（返件估值口径）/).length).toBeGreaterThanOrEqual(2);
  });

  it("B-F07：总览金额卡副行呈现三域覆盖（采购/销售/成本域各自已估/待估/覆盖）", async () => {
    renderPage();
    await waitFor(
      () => expect(screen.getByTestId("kpi-valuation")).toHaveTextContent("比例 50%"),
      { timeout: 5000 },
    );
    const card = screen.getByTestId("kpi-valuation");
    expect(card).toHaveTextContent("覆盖 75%");
    expect(card).toHaveTextContent("成本域");
    expect(card).toHaveTextContent("覆盖 25%");
    expect(card).toHaveTextContent("返件估值口径");
  });

  it("B-F09：消耗混合单位按物理单位分桶（件 2 已计 100% / 台 3 未计 0%），不并成 5 或 40%", async () => {
    fetchDashboardConsumption.mockResolvedValueOnce(consumptionMixedFixture);
    // q=ST 只应作用于支持关键词的端点：orders/consumption 仍剥离（B-F10 只放开 checks）
    renderPage("/maintenance/dashboard?tab=consumption&q=ST");
    await screen.findByTestId("consumption-cards");
    expect(fetchDashboardOrders).toHaveBeenCalledWith(
      expect.not.objectContaining({ q: expect.anything() }),
    );
    expect(fetchDashboardConsumption).toHaveBeenCalledWith(
      expect.not.objectContaining({ q: expect.anything() }),
    );
    // 卡片：平面 issued null → 「混合单位」+ 分桶行；金额 22.60 是跨单位同币种小计
    const qtyCard = screen.getByTestId("kpi-consumption-qty");
    expect(qtyCard).toHaveTextContent("混合单位");
    expect(qtyCard).toHaveTextContent("件：2");
    expect(qtyCard).toHaveTextContent("台：3");
    expect(screen.getByTestId("kpi-consumption-cost")).toHaveTextContent("22.6");
    // 覆盖率平面 null：分桶各报（件 100% / 台 0%），绝不出现合并的 40%
    const coverageCard = screen.getByTestId("kpi-consumption-coverage");
    expect(coverageCard).toHaveTextContent("件：100%");
    expect(coverageCard).toHaveTextContent("台：0%");
    expect(screen.queryByText(/40%/)).not.toBeInTheDocument();
    // 未计成本量同样分桶（台 3）
    expect(screen.getByTestId("kpi-consumption-uncosted")).toHaveTextContent("台：3");
    // 日表/下单表都有单位列（混合单位呈现）；数量单元格分桶行
    expect(screen.getAllByRole("columnheader", { name: "单位" }).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/台：3/).length).toBeGreaterThan(1);
  });

  it("B-F11：全部未计成本的桶金额未知（—），不是 ¥0.00；数量与覆盖 0% 照给", async () => {
    fetchDashboardConsumption.mockResolvedValueOnce(consumptionUncostedFixture);
    renderPage("/maintenance/dashboard?tab=consumption");
    await screen.findByTestId("consumption-cards");
    const costCard = screen.getByTestId("kpi-consumption-cost");
    expect(costCard).toHaveTextContent("—");
    expect(costCard).toHaveTextContent("未知");
    expect(costCard).not.toHaveTextContent("0.00");
    expect(screen.queryByText(/¥\s*0\.00/)).not.toBeInTheDocument();
    expect(screen.getByTestId("kpi-consumption-uncosted")).toHaveTextContent("3");
    expect(screen.getByTestId("kpi-consumption-coverage")).toHaveTextContent("0%");
  });

  it("B-F12：无制单日期下单单独成桶（7／1 张，未计入期间），不冒充入期；混合单位不并 17", async () => {
    fetchDashboardOrders.mockResolvedValueOnce(ordersMixedFixture);
    renderPage("/maintenance/dashboard?tab=consumption");
    await screen.findByTestId("orders-cards");
    const qtyCard = screen.getByTestId("kpi-order-qty");
    expect(qtyCard).toHaveTextContent("混合单位");
    expect(qtyCard).toHaveTextContent("件：15");
    expect(qtyCard).toHaveTextContent("台：2");
    expect(screen.queryByText("17")).not.toBeInTheDocument();
    // 无日期桶：卡片副行（数量域全角色）+ 项目表行内列
    expect(qtyCard).toHaveTextContent("无制单日期下单量 7");
    expect(qtyCard).toHaveTextContent("单数 1");
    expect(qtyCard).toHaveTextContent("未计入期间");
    expect(screen.getByRole("columnheader", { name: /无制单日期下单量/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /无制单日期单数/ })).toBeInTheDocument();
    expect(screen.getAllByText("7").length).toBeGreaterThan(0);
  });

  it("B-F09：总览混合单位——卡片分桶、趋势/件况按单位拆系列、Top 项目分单位", async () => {
    fetchDashboardSummary.mockResolvedValueOnce(summaryMixedFixture);
    fetchDashboardOrders.mockResolvedValueOnce(ordersMixedFixture);
    renderPage();
    await waitFor(
      () => expect(screen.getByTestId("kpi-receipt")).toHaveTextContent("混合单位"),
      { timeout: 5000 },
    );
    const receipt = screen.getByTestId("kpi-receipt");
    expect(receipt).toHaveTextContent("件：2");
    expect(receipt).toHaveTextContent("台：1");
    expect(screen.getByTestId("kpi-bad")).toHaveTextContent("件：1");
    expect(screen.getByTestId("kpi-bad")).toHaveTextContent("台：1");
    expect(screen.getByTestId("kpi-issued")).toHaveTextContent("件：4");
    expect(screen.getByTestId("kpi-issued")).toHaveTextContent("台：2");
    // 趋势：按单位拆系列（绝不跨单位求和成一条柱）
    const trend = screen.getByTestId("overview-trend-chart");
    expect(trend).toHaveTextContent("返件实收（件）");
    expect(trend).toHaveTextContent("返件实收（台）");
    expect(trend).toHaveTextContent("实际领用（件）");
    expect(trend).toHaveTextContent("实际领用（台）");
    // 件况结构同样按单位拆系列
    const condition = screen.getByTestId("overview-condition-chart");
    expect(condition).toHaveTextContent("返件实收（件）");
    expect(condition).toHaveTextContent("返件实收（台）");
    // Top 项目行分单位呈现
    expect(screen.getAllByText(/件：2/).length).toBeGreaterThan(0);
    // 金额卡：估值平面 null → 单位分桶行（已估/待估/覆盖按单位）
    const valuation = screen.getByTestId("kpi-valuation");
    expect(valuation).toHaveTextContent("件：已估 3");
    expect(valuation).toHaveTextContent("台：已估 1");
    // M01 卡同一 orders 响应：无日期桶说明在总览同样可见
    expect(screen.getByTestId("kpi-orders")).toHaveTextContent("无制单日期下单量 7");
  });

  it("B-F09：项目对比混合单位（单位列/分桶单元格/率不可加总）+ B-F07 三域覆盖列", async () => {
    fetchDashboardProjects.mockResolvedValueOnce(projectsMixedFixture);
    fetchDashboardOrders.mockResolvedValueOnce(ordersMixedFixture);
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    expect(screen.getByRole("columnheader", { name: "单位" })).toBeInTheDocument();
    expect(screen.getAllByText(/混合单位/).length).toBeGreaterThan(0);
    // 数量单元格分桶（返件实收：件 3 / 台 2），数量绝不跨单位合并
    expect(screen.getAllByText(/件：3/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/台：2/).length).toBeGreaterThan(0);
    // 返还率混合 → 率不可加总提示，不是 0%
    expect(screen.getAllByText(/率不可加总/).length).toBeGreaterThan(0);
    // 三域覆盖列（返件估值口径）
    expect(screen.getByRole("columnheader", { name: /三域覆盖/ })).toBeInTheDocument();
    expect(screen.getAllByText(/覆盖 75%（返件估值口径）/).length).toBeGreaterThan(0);
  });
});

// ---- B2 独立反馈（b2fix）：双税并列 / 域分桶覆盖 / 每单位率 / 核对单位 / 无日期单位 ----

/** 行内取列单元格（AntD Table：thead 列序 = tbody cell 序）。 */
function headerCell(row: HTMLElement, header: string): HTMLElement {
  const table = row.closest("table")!;
  const headers = within(table).getAllByRole("columnheader");
  const index = headers.findIndex((h) => h.textContent?.trim() === header);
  if (index < 0) throw new Error(`missing header ${header}`);
  return within(row).getAllByRole("cell")[index];
}

describe("B2 独立反馈（B2-F01~F05）", () => {
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
    fetchDashboardSummary.mockResolvedValue(summaryFixture(true, "0.5"));
    fetchDashboardProjects.mockResolvedValue(projectsFixture);
    fetchDashboardParts.mockResolvedValue(partsQtyFixture);
    fetchDashboardValuation.mockResolvedValue(valuationFixture("0.5"));
    fetchDashboardQuantityChecks.mockResolvedValue(checksFixture);
    fetchDashboardOrders.mockResolvedValue(ordersFixture);
    fetchDashboardConsumption.mockResolvedValue(consumptionFixture(true));
    fetchDashboardConsumptionIssues.mockResolvedValue(consumptionIssuesFixture);
    fetchDashboardReceipts.mockResolvedValue(receiptsFixture);
  });

  // B2-F01：三域值双税并列——已提供两侧就都显示，不得由含税÷1.13 推算或漏一侧
  for (const [domain, ex, inc] of [
    ["purchase", "100.01", "113.01"],
    ["sales", "200.02", "226.02"],
    ["cost", "37.43", "42.29"],
  ] as const) {
    it(`B2-F01：${domain} 域双税并列（¥${inc} 含税 + ¥${ex} 不含税），未知侧不补 0`, () => {
      const domains: Record<string, DomainValuationPayload> = {
        [domain]: {
          value_ex_tax: ex, value_inc_tax: inc,
          bad_qty: "4", estimated_bad_qty: "3", pending_bad_qty: "1",
          awaiting_ratio_bad_qty: "0", coverage_pct: 75, pending_reasons: {},
          unit: "件", unit_breakdown: [], coverage_basis: "bad_receipts",
        },
      };
      render(<DomainCoverageLines domains={domains} />);
      expect(document.body.textContent).toContain(inc);
      expect(document.body.textContent).toContain(ex);
      expect(document.body.textContent).toContain("含税");
      expect(document.body.textContent).toContain("不含税");
    });
  }

  it("B2-F01：采购/销售参考双税并列（¥81.03 含税 + ¥71.71 不含税），不互相推算", () => {
    render(<ReferenceLine
      domain="purchase"
      reference={{ available: true, inc_tax: "81.03", ex_tax: "71.71", sample_count: 2, order_count: 1 }} />);
    expect(document.body.textContent).toContain("81.03");
    expect(document.body.textContent).toContain("71.71");
    render(<ReferenceLine
      domain="sales"
      reference={{ available: true, inc_tax: "226.02", ex_tax: "200.02", sample_count: 2, order_count: 1 }} />);
    expect(document.body.textContent).toContain("226.02");
    expect(document.body.textContent).toContain("200.02");
  });

  it("B2-F01：PN 表成本参考双税（42.29/37.43）+ 三域列头不再单侧「含税」", async () => {
    fetchDashboardValuation.mockResolvedValueOnce({
      ...valuationFixture("0.5"),
      rows: [{
        ...PART_ROW_QTY,
        pn: "INDEPENDENT-COST-PN",
        valuation: {
          state: "ready",
          estimated_bad_qty: "1.000", pending_bad_qty: "1.000", awaiting_ratio_bad_qty: "0.000",
          coverage_pct: 50,
          bad_estimated_value_inc_tax: "42.29", bad_estimated_value_ex_tax: "37.43",
          domains: DOMAIN_COVERAGE_FIXTURE,
        },
        references: {
          purchase: { available: false, unavailable_reason: "no_samples" },
          sales: { available: false, unavailable_reason: "no_samples" },
          cost: {
            available: true, unit_cost_inc_tax: "42.29", unit_cost_ex_tax: "37.43",
            sample_line_count: 2, coverage_pct: 100,
          },
          chosen: null, chosen_reason: null,
        },
      }],
    });
    renderPage("/maintenance/dashboard?tab=parts");
    await screen.findByText("INDEPENDENT-COST-PN");
    const row = screen.getByText("INDEPENDENT-COST-PN").closest("tr")!;
    // 成本参考：持久化双税值分别显示，不是 42.29÷1.13 重建
    expect(row.textContent).toContain("42.29");
    expect(row.textContent).toContain("37.43");
    // 列头不得单侧误述「含税」
    expect(screen.getByRole("columnheader", { name: /三域参考/ }).textContent).toContain("不含税");
    expect(screen.getByRole("columnheader", { name: /三域试算价值/ }).textContent).toContain("不含税");
  });

  it("B2-F02：域覆盖混合单位消费 unit_breakdown（件 66.7% / 台 0%），不把分桶画成全 —", () => {
    render(
      <DomainCoverageLines
        domains={{
          cost: {
            bad_qty: null, estimated_bad_qty: null, pending_bad_qty: null,
            awaiting_ratio_bad_qty: null, coverage_pct: null, pending_reasons: {},
            unit: null, coverage_basis: "bad_receipts",
            unit_breakdown: [
              { unit: "件", bad_qty: "3", estimated_bad_qty: "2", pending_bad_qty: "1", awaiting_ratio_bad_qty: "0", coverage_pct: 66.7 },
              { unit: "台", bad_qty: "4", estimated_bad_qty: "0", pending_bad_qty: "4", awaiting_ratio_bad_qty: "0", coverage_pct: 0 },
            ],
          },
        }}
      />,
    );
    const content = document.body.textContent ?? "";
    expect(content).toContain("件");
    expect(content).toContain("66.7%");
    expect(content).toContain("台");
    expect(content).toContain("0%");
    // 件 已估 2：不得误报「域内无已估金额」
    expect(content).not.toContain("域内无已估金额");
  });

  it("B2-F03：项目混合单位返还率按单位呈现分子/分母/率（件 50%（1 ÷ 2）/ 台 33.3%（1 ÷ 3））", async () => {
    fetchDashboardProjects.mockResolvedValueOnce({
      rows: [{
        ...PROJECT_ROW,
        unit: null,
        unit_breakdown: [
          { unit: "件", receipt_qty: "1", bad_receipt_qty: "1", issued_qty: "2", return_rate_pct: 50, rate_numerator: "1", rate_denominator: "2" },
          { unit: "台", receipt_qty: "1", bad_receipt_qty: "1", issued_qty: "3", return_rate_pct: 33.3, rate_numerator: "1", rate_denominator: "3" },
        ],
        receipt_qty: null, bad_receipt_qty: null, scrap_receipt_qty: null, issued_qty: null,
        exempt_issued_qty: null, pending_requirement_qty: null, unlinked_receipt_qty: null,
        return_rate_pct: null, rate_numerator: null, rate_denominator: null,
      }],
      pagination: { page: 1, page_size: 20, total: 1 },
    });
    renderPage("/maintenance/dashboard?tab=projects");
    await screen.findByText("项目一");
    const row = screen.getByRole("link", { name: "项目一" }).closest("tr")!;
    const cell = headerCell(row, "同期返还率");
    expect(cell.textContent).toContain("件");
    expect(cell.textContent).toContain("50%");
    expect(cell.textContent).toContain("1 ÷ 2");
    expect(cell.textContent).toContain("台");
    expect(cell.textContent).toContain("33.3%");
    expect(cell.textContent).toContain("1 ÷ 3");
    // 率仍不可跨单位加总：提示保留
    expect(cell.textContent).toContain("率不可加总");
  });

  it("B2-F04：需求单核对消费单位分桶（件 N7/R1/差6、台 N4/R2/差2），不再整域 —", async () => {
    const buckets = [
      { unit: "件", demand_qty: "7", received_qty: "1", difference_qty: "6", over_received_qty: "0" },
      { unit: "台", demand_qty: "4", received_qty: "2", difference_qty: "2", over_received_qty: "0" },
    ];
    fetchDashboardQuantityChecks.mockResolvedValueOnce({
      cards: {
        checked_demand_qty: null, received_qty: null, known_difference_qty: null,
        over_received_qty: null, pending_check_order_count: 0, inactive_order_count: 0,
        unassigned_receipt_qty: "0", note: "独立单位核对", unit_breakdown: [
          { unit: "件", checked_demand_qty: "7", received_qty: "1", known_difference_qty: "6", over_received_qty: "0" },
          { unit: "台", checked_demand_qty: "4", received_qty: "2", known_difference_qty: "2", over_received_qty: "0" },
        ],
      },
      rows: [{
        ...checksFixture.rows[0],
        order_no: "混单位需求单",
        demand_qty: null, received_qty: null, difference_qty: null, over_received_qty: null,
        unit: null, unit_breakdown: buckets,
      }],
      unassigned_receipts: [],
      pagination: { page: 1, page_size: 20, total: 1 },
    });
    renderPage("/maintenance/dashboard?tab=checks");
    await screen.findByText("混单位需求单");
    const row = screen.getByText("混单位需求单").closest("tr")!;
    expect(row.textContent).toContain("件");
    expect(row.textContent).toContain("台");
    expect(headerCell(row, "需求量 N").textContent).toContain("7");
    expect(headerCell(row, "需求量 N").textContent).toContain("4");
    expect(headerCell(row, "实收 R").textContent).toContain("1");
    expect(headerCell(row, "实收 R").textContent).toContain("2");
    // 卡片合计同样分桶呈现
    expect(screen.getByTestId("checks-cards").textContent).toContain("件：7");
    expect(screen.getByTestId("checks-cards").textContent).toContain("台：4");
    const differenceCard = screen.getByText("差额合计").closest(".ant-card")!;
    expect(differenceCard).toHaveTextContent("件：6");
    expect(differenceCard).toHaveTextContent("台：2");
  });

  it("B2-F05：无日期下单混合单位按单位呈现（件：7/台：4），不再只剩 —", async () => {
    fetchDashboardOrders.mockResolvedValueOnce({
      cards: {
        order_qty: "0", order_count: 0,
        undated_orders: {
          order_qty: null, order_count: 2, note: "不混单位",
          unit_breakdown: [{ unit: "件", order_qty: "7" }, { unit: "台", order_qty: "4" }],
        },
      },
      rows: [{
        ...ordersFixture.rows[0],
        order_qty: "0", order_count: 0,
        undated_order_qty: null, undated_order_count: 2,
        undated_unit_breakdown: [{ unit: "件", order_qty: "7" }, { unit: "台", order_qty: "4" }],
      }],
    });
    renderPage("/maintenance/dashboard?tab=consumption");
    const kpi = await screen.findByTestId("kpi-order-qty");
    await within(kpi).findByText(/无制单日期下单量/);
    expect(kpi.textContent).toContain("件：7");
    expect(kpi.textContent).toContain("台：4");
    expect(kpi.textContent).toContain("未计入期间");
    // 行内无日期列同样分桶（表内 UnitBreakdown 行）
    expect(screen.getAllByText(/件：7/).length).toBeGreaterThan(1);
  });

  // ---- v3 契约同步（lane-b-backend-coverage-contract version 3）----

  it("v3 §5.1：需求单无业务日实收按单位分桶在 unit_breakdown 条目内（件：3/台：2），不再只剩 —", async () => {
    // v3 定稿拼写：行内 undated 量是 unit_breakdown 条目的 undated_receipt_qty
    // （backend 未采纳独立 undated_receipt_unit_breakdown 键，见 v3-sync 结果）
    const buckets = [
      { unit: "件", demand_qty: "7", received_qty: "1", difference_qty: "6", over_received_qty: "0", undated_receipt_qty: "3" },
      { unit: "台", demand_qty: "4", received_qty: "2", difference_qty: "2", over_received_qty: "0", undated_receipt_qty: "2" },
    ];
    fetchDashboardQuantityChecks.mockResolvedValueOnce({
      cards: { ...checksFixture.cards },
      rows: [{
        ...checksFixture.rows[0],
        order_no: "无日期混合单",
        undated_receipt_qty: null,
        unit: null, unit_breakdown: buckets,
      }],
      unassigned_receipts: [],
      pagination: { page: 1, page_size: 20, total: 1 },
    });
    renderPage("/maintenance/dashboard?tab=checks");
    await screen.findByText("无日期混合单");
    const row = screen.getByText("无日期混合单").closest("tr")!;
    const cell = headerCell(row, "无业务日实收");
    expect(cell.textContent).toContain("件：3");
    expect(cell.textContent).toContain("台：2");
  });

  it("v3 §3：未关联需求单实收混合单位平面 null + 每单位桶（卡 件：7/台：2；行内同桶），不并成 9", async () => {
    fetchDashboardQuantityChecks.mockResolvedValueOnce({
      cards: {
        ...checksFixture.cards,
        unassigned_receipt_qty: null,
        unassigned_receipt_unit_breakdown: [
          { unit: "件", received_qty: "7" },
          { unit: "台", received_qty: "2" },
        ],
      },
      rows: [checksFixture.rows[0]],
      unassigned_receipts: [{
        project_id: "p2", project_code: "MDD-P2", display_name: "项目二",
        received_qty: null,
        unit_breakdown: [{ unit: "件", received_qty: "7" }, { unit: "台", received_qty: "2" }],
      }],
      pagination: { page: 1, page_size: 20, total: 1 },
    });
    renderPage("/maintenance/dashboard?tab=checks");
    await screen.findByText("项目二（MDD-P2）");
    // 卡：平面 null → 混合单位 + 分桶小字；说明文案保留
    const card = screen.getByText("未关联需求单实收").closest(".ant-card")!;
    expect(card.textContent).toContain("混合单位");
    expect(card.textContent).toContain("件：7");
    expect(card.textContent).toContain("台：2");
    expect(card.textContent).toContain("不摊入需求单行");
    // 行内：received_qty 平面 null → 分桶行
    const row = screen.getByText("项目二（MDD-P2）").closest("tr")!;
    expect(row.textContent).toContain("件：7");
    expect(row.textContent).toContain("台：2");
  });

  it("v3 §5.2：总览返还率混合单位消费 return_rate_breakdown（件 50%（1 ÷ 2）/ 台 33.3%（1 ÷ 3））", async () => {
    fetchDashboardSummary.mockResolvedValueOnce({
      ...summaryMixedFixture,
      cards: {
        ...summaryMixedFixture.cards,
        return_rate_breakdown: [
          { unit: "件", rate_numerator: "1.000", rate_denominator: "2.000", return_rate_pct: 50 },
          { unit: "台", rate_numerator: "1.000", rate_denominator: "3.000", return_rate_pct: 33.3 },
        ],
      },
    });
    fetchDashboardOrders.mockResolvedValueOnce(ordersMixedFixture);
    renderPage();
    await waitFor(
      () => expect(screen.getByTestId("kpi-rate")).toHaveTextContent("件：50%"),
      { timeout: 5000 },
    );
    const rate = screen.getByTestId("kpi-rate");
    expect(rate).toHaveTextContent("件：50%（1 ÷ 2）");
    expect(rate).toHaveTextContent("台：33.3%（1 ÷ 3）");
    // 率不可跨单位加总提示保留
    expect(rate).toHaveTextContent("混合单位：分子分母不可跨单位加总");
  });
});
