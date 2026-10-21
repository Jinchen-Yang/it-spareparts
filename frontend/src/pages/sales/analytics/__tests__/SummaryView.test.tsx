// 汇总 KPI 卡状态（D-44 §9 / Q-A-05 / Q-A-07 / Q-A-10 / A-AC11）：直接渲染 SummaryView，
// 锁定「待配成本」「区间内无订单」「成本覆盖率 + 上月对比同显」「全部区间不拆新老客」
// 「无法判断」等状态文案——这些是空值语义（null ≠ 0）的用户可见承诺。
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "antd";
import SummaryView from "../SummaryView";
import type { SalesAnalyticsSummary, SalesKpi } from "../../../../api/salesAnalytics";

const kpi = (over: Partial<SalesKpi> = {}): SalesKpi => ({
  orders: 5,
  amount_ex_tax_total: 1000,
  amount_inc_tax_total: 1130,
  amount_inc_tax_missing: 0,
  revenue_total: 800,
  ...over,
});

const summary = (k: SalesKpi, over: Partial<SalesAnalyticsSummary> = {}): SalesAnalyticsSummary => ({
  window: { date_from: "2026-10-17", date_to: "2026-10-17" },
  filters_echo: { business_type: [], customer: null, salesperson: null },
  as_of: "2026-10-17",
  kpi: k,
  ...over,
});

const view = (s: SalesAnalyticsSummary) => render(<App><SummaryView summary={s} amounts={["ex", "inc", "revenue"]} /></App>);

afterEach(cleanup);

describe("毛利卡（Q-A-05 / A-AC11）", () => {
  it("有单但无毛利 → 「待配成本」，不是 0.00", () => {
    view(summary(kpi({ gross_profit: null, cost_coverage: 0.5 })));
    expect(screen.getByText("待配成本")).toBeInTheDocument();
    expect(screen.queryByText("0.00")).toBeNull();
  });

  it("成本覆盖率与上月对比同时见（A-AC11：不互斥）", () => {
    view(summary(
      kpi({ gross_profit: 200, cost_coverage: 0.625 }),
      { compare: { date_from: "2026-09-17", date_to: "2026-09-17", kpi: kpi({ orders: 4, gross_profit: 100 }) } },
    ));
    expect(screen.getByText("成本覆盖率 62.5%")).toBeInTheDocument();
    // 同一行内并排出现上月对比与增减
    expect(screen.getByText(/上月同期 ¥?100\.00/)).toBeInTheDocument();
    expect(screen.getByText(/↑\+100\.0%/)).toBeInTheDocument();
  });

  it("coverage=null 只在无营收时发生 → 「无有效基数」，不显示 0%", () => {
    view(summary(kpi({ orders: 3, revenue_total: null, gross_profit: null, cost_coverage: null })));
    expect(screen.getByText("无有效基数（区间内无营收）")).toBeInTheDocument();
    expect(screen.queryByText("成本覆盖率 0%")).toBeNull();
  });

  it("区间内无订单 → 不含税 / 含税 / 毛利卡副行都是「区间内无订单」（null ≠ 0）", () => {
    view(summary(kpi({
      orders: 0, amount_ex_tax_total: null, amount_inc_tax_total: null,
      revenue_total: null, gross_profit: null, cost_coverage: null,
    })));
    // 营收卡副行是自身口径提示（计营收行口径），不计入
    expect(screen.getAllByText("区间内无订单").length).toBe(3);
  });
});

describe("成交客户卡（Q-A-10）", () => {
  it("新老客拆分可见；无法判断计数 > 0 时如实列出", () => {
    view(summary(kpi({
      customers: 10, new_customers: 6, returning_customers: 3, undetermined: 1, available: true,
    })));
    expect(screen.getByText("新客 6 · 老客 3 · 无法判断 1")).toBeInTheDocument();
  });

  it("无法判断为 0 时不出现该段（不渲染「无法判断 0」）", () => {
    view(summary(kpi({ customers: 8, new_customers: 5, returning_customers: 3, undetermined: 0 })));
    expect(screen.getByText("新客 5 · 老客 3")).toBeInTheDocument();
    expect(screen.queryByText(/无法判断/)).toBeNull();
  });

  it("「全部」区间（available=false）：无起始日期不拆新老客，如实说明", () => {
    view(summary(kpi({
      customers: 8, new_customers: null, returning_customers: null, undetermined: null, available: false,
    })));
    expect(screen.getByText("所选区间无起始日期（全部），不拆新老客")).toBeInTheDocument();
    expect(screen.queryByText(/新客/)).toBeNull();
  });

  it("后端未回客户数（旧响应）→ 卡不渲染，不显示「undefined」", () => {
    view(summary(kpi({ customers: undefined })));
    expect(screen.queryByText("成交客户")).toBeNull();
  });
});

describe("上月对比（Q-A-07）", () => {
  it("上月基数为 0 或负 → 不显示增幅（无 ∞ / 无误导性的正增长）", () => {
    view(summary(
      kpi({ orders: 3, gross_profit: 200 }),
      // 上月 orders 与本期相等（增幅 0 不带箭头），孤立出毛利行的负基数行为
      { compare: { date_from: "2026-09-17", date_to: "2026-09-17", kpi: kpi({ orders: 3, gross_profit: -100 }) } },
    ));
    // 上月同期 -100 可见，但不带 ↑/↓ 增幅（负基数无增幅语义）
    expect(screen.getByText(/上月同期 ¥?-100\.00/)).toBeInTheDocument();
    expect(screen.queryByText(/↑/)).toBeNull();
    expect(screen.queryByText(/↓/)).toBeNull();
  });
});

describe("行级口径说明（Q-A-02）", () => {
  it("line_mode=true：整单入选 / 匹配行计额说明可见", () => {
    view(summary(kpi(), { line_mode: true }));
    expect(screen.getByText(/整单入选、匹配行计额/)).toBeInTheDocument();
    expect(screen.getByText("不含税金额（名义）")).toBeInTheDocument();
  });
});
