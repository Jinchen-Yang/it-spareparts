// 采购分析图表纯函数（Q-A-01 / D-39 补图表）：趋势粒度推荐、日/月桶排序
// （「(空)」恒末位）、趋势 / 排行 option（null 断点不画 0、负值换色 + 0 轴参考线）。
// 约定同 charts/README：null ≠ 0；颜色只从 CHART_COLORS 取。
import { describe, expect, it } from "vitest";
import {
  PURCHASE_EMPTY_KEY,
  buildPurchaseRankOption,
  buildPurchaseTrendOption,
  sortTrendGroups,
} from "../../../../components/charts/PurchaseAnalyticsCharts";
import { CHART_COLORS } from "../../../../components/charts/chartTheme";
import { recommendTrendGranularity } from "../urlState";
import type { PurchaseAnalyticsGroup } from "../../../../api/purchaseAnalytics";

const g = (over: Partial<PurchaseAnalyticsGroup> & { key: string }): PurchaseAnalyticsGroup => ({
  label: null, orders: 1, lines: 1,
  amount_inc: null, amount_ex: null, amount_paid: null,
  ...over,
});

describe("趋势粒度推荐（Q-A-01）", () => {
  it("区间已知且 ≤31 天按日，>31 天 / 未知 / 全部按月（只作初始值）", () => {
    expect(recommendTrendGranularity("2026-10-17", "2026-10-17")).toBe("day");   // 1 天
    expect(recommendTrendGranularity("2026-09-17", "2026-10-17")).toBe("day");   // 31 天
    expect(recommendTrendGranularity("2026-09-16", "2026-10-17")).toBe("month"); // 32 天
    expect(recommendTrendGranularity(null, "2026-10-17")).toBe("month");         // 全部（无起点）
    expect(recommendTrendGranularity("2026-10-01", null)).toBe("month");
    expect(recommendTrendGranularity()).toBe("month");
  });
});

describe("日/月桶排序（趋势图）", () => {
  it("日期键升序；「(空)」无日期桶恒排最后（ASCII 上它本会排最前）", () => {
    const sorted = sortTrendGroups([
      g({ key: "2026-10-02" }),
      g({ key: PURCHASE_EMPTY_KEY }),
      g({ key: "2026-10-01" }),
      g({ key: "2026-09-30" }),
    ]);
    expect(sorted.map((x) => x.key)).toEqual(["2026-09-30", "2026-10-01", "2026-10-02", "(空)"]);
  });
});

describe("趋势柱图 option", () => {
  it("「(空)」x 轴显示「无日期」；null 桶保持 null（断点，不画 0）", () => {
    const opt = buildPurchaseTrendOption([
      g({ key: "2026-10-01", amount_paid: 100 }),
      g({ key: "2026-10-02", amount_paid: null }),
      g({ key: PURCHASE_EMPTY_KEY, amount_paid: 50 }),
    ], "amount_paid") as unknown as {
      xAxis: { data: string[] };
      series: { data: ({ value: number } | null)[]; markLine?: unknown }[];
    };
    expect(opt.xAxis.data).toEqual(["2026-10-01", "2026-10-02", "无日期"]);
    expect(opt.series[0].data).toEqual([
      { value: 100, itemStyle: { color: CHART_COLORS.purchase, borderRadius: [4, 4, 0, 0] } },
      null,
      { value: 50, itemStyle: { color: CHART_COLORS.purchase, borderRadius: [4, 4, 0, 0] } },
    ]);
    expect(opt.series[0].markLine).toBeUndefined(); // 无负值不加 0 轴线
  });

  it("负值桶换 profitNegative 色并加 yAxis=0 参考线（退款/冲销不伪装成正数）", () => {
    const opt = buildPurchaseTrendOption([
      g({ key: "2026-10-01", amount_paid: 100 }),
      g({ key: "2026-10-02", amount_paid: -30 }),
    ], "amount_paid") as unknown as {
      series: { data: { value: number; itemStyle: { color: string } }[]; markLine: { data: { yAxis: number }[] } }[];
    };
    const [pos, neg] = opt.series[0].data;
    expect(pos.itemStyle.color).toBe(CHART_COLORS.purchase);
    expect(neg.itemStyle.color).toBe(CHART_COLORS.profitNegative);
    expect(opt.series[0].markLine.data).toEqual([{ yAxis: 0 }]);
  });

  it("单数口径与金额口径同一构造（orders 非空即可画）", () => {
    const opt = buildPurchaseTrendOption([g({ key: "2026-10-01", orders: 7 })], "orders") as unknown as {
      series: { data: { value: number }[]; name: string }[];
    };
    expect(opt.series[0].data[0].value).toBe(7);
    expect(opt.series[0].name).toBe("采购单数");
  });
});

describe("排行横条 option", () => {
  it("null 项剔除（被遮蔽 / 缺值不冒充 0）；负值换色 + xAxis=0 参考线", () => {
    const opt = buildPurchaseRankOption([
      { label: "备件采购", group: g({ key: "备件采购", amount_paid: 300 }) },
      { label: "批量采购", group: g({ key: "批量采购", amount_paid: null }) },
      { label: "退货冲销", group: g({ key: "退货冲销", amount_paid: -20 }) },
    ], "amount_paid") as unknown as {
      yAxis: { data: string[] };
      series: { data: { value: number; itemStyle: { color: string; borderRadius: number[] } }[]; markLine: { data: { xAxis: number }[] } }[];
    };
    // null 剔除：只剩两项
    expect(opt.yAxis.data).toEqual(["备件采购", "退货冲销"]);
    const [pos, neg] = opt.series[0].data;
    expect(pos.itemStyle.color).toBe(CHART_COLORS.purchase);
    expect(neg.itemStyle.color).toBe(CHART_COLORS.profitNegative);
    // 负值条圆角朝左（贴 0 轴一侧），与正值相反
    expect(neg.itemStyle.borderRadius).toEqual([4, 0, 0, 4]);
    expect(pos.itemStyle.borderRadius).toEqual([0, 4, 4, 0]);
    expect(opt.series[0].markLine.data).toEqual([{ xAxis: 0 }]);
  });
});
