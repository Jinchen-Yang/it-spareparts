// 销售看板（D-44）纯函数：URL 状态往返、金额拖动条取整与极值、下钻补丁、粒度推荐、
// 趋势桶下钻区间、环比、图表 option（null 剔除 / 负值换色 / 上月同期对齐）。
import { describe, expect, it } from "vitest";
import {
  parseAmount,
  parseSalesAnalyticsUrl,
  serializeSalesAnalyticsState,
  toSalesApiFilters,
} from "../urlState";
import { rangeFromSlider, sliderCeiling, snapAmount } from "../AmountRangeFilter";
import { drillPatch } from "../GroupsTab";
import { bucketRange, recommendGranularity } from "../TrendTab";
import { changeRatio } from "../SummaryView";
import {
  buildGroupBarOption,
  buildTrendOption,
  buildWeekdayOption,
} from "../../../../components/charts/SalesDashboardCharts";
import { toSalesAnalyticsParams } from "../../../../api/salesAnalytics";
import { businessToday } from "../../../../utils/date";

const sp = (q: string) => new URLSearchParams(q);

describe("URL 状态（D-44 新键）", () => {
  it("零参数默认：订单明细页签、降序、对比开、金额不限、两开关开", () => {
    const s = parseSalesAnalyticsUrl(sp(""));
    expect(s).toMatchObject({
      tab: "orders", order: "desc", compare: true, amountMin: null, amountMax: null,
      includeNegative: true, includeEmpty: true, amountBasis: "amount_ex_tax",
      groupBy: "customer", groupTop: 20, granularity: null,
      customers: [], customerTypes: [], salespeople: [], categories: [], brands: [],
    });
  });

  it("多选与金额区间往返；默认值不落 URL", () => {
    const patch = serializeSalesAnalyticsState({
      customers: ["A公司", "B,公司"], categories: ["内存"], amountMin: 1000, amountMax: 5000,
      includeNegative: false, order: "asc", tab: "groups", compare: false,
      amountBasis: "amount_ex_tax", groupTop: 20,
    });
    expect(patch).toMatchObject({
      customer_name: ["A公司", "B,公司"], category: ["内存"], amin: "1000", amax: "5000",
      neg: "0", order: "asc", tab: "groups", compare: "0", url_version: "2",
    });
    expect(patch.amount_basis).toBeUndefined();
    expect(patch.gtop).toBeUndefined();
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(patch)) {
      if (Array.isArray(v)) v.forEach((x) => q.append(k, x));
      else if (v !== undefined) q.set(k, v);
    }
    const back = parseSalesAnalyticsUrl(q);
    expect(back.customers).toEqual(["A公司", "B,公司"]);   // url_version=2：逗号原样保留
    expect(back).toMatchObject({ amountMin: 1000, amountMax: 5000, includeNegative: false,
      order: "asc", tab: "groups", compare: false });
  });

  it("非法值收敛：页签 / 维度 / Top / 粒度未知回默认；金额按 1k 取整", () => {
    const s = parseSalesAnalyticsUrl(sp("tab=x&group_by=supplier&gtop=7&gran=year&amin=1499&amax=abc"));
    expect(s).toMatchObject({ tab: "orders", groupBy: "customer", groupTop: 20,
      granularity: null, amountMin: 1000, amountMax: null });
    expect(parseAmount("2500")).toBe(3000);
    expect(parseAmount("")).toBeNull();
  });

  it("状态 → API 参数：只带非默认项", () => {
    const s = parseSalesAnalyticsUrl(sp("preset=all&customer_type=%E7%9B%B4%E5%AE%A2&amin=2000&empty=0&amount_basis=revenue"));
    expect(toSalesApiFilters(s, businessToday())).toEqual({
      customer_type: ["直客"], amount_min: 2000, include_empty: false, amount_basis: "revenue",
    });
  });

  it("请求参数序列化：多选重复键、开关只在 false 时落、0 下限保留", () => {
    const p = toSalesAnalyticsParams({
      customer_name: ["甲", "乙"], amount_min: 0, include_negative: false, include_empty: true,
      amount_basis: "amount_ex_tax",
    });
    expect(p.getAll("customer_name")).toEqual(["甲", "乙"]);
    expect(p.get("amount_min")).toBe("0");
    expect(p.get("include_negative")).toBe("false");
    expect(p.has("include_empty")).toBe(false);
    expect(p.has("amount_basis")).toBe(false);
  });
});

describe("金额拖动条（D-44 §7）", () => {
  it("右端向上取整到 1k，至少 1k", () => {
    expect(sliderCeiling(8000)).toBe(8000);
    expect(sliderCeiling(8000.01)).toBe(9000);
    expect(sliderCeiling(null)).toBe(1000);
    expect(sliderCeiling(-5)).toBe(1000);
  });
  it("输入按 1k 取整；拖到两端极值视为不限", () => {
    expect(snapAmount(1499)).toBe(1000);
    expect(snapAmount(1500)).toBe(2000);
    expect(snapAmount(undefined)).toBeNull();
    expect(rangeFromSlider([0, 9000], 9000)).toEqual({ amountMin: null, amountMax: null });
    expect(rangeFromSlider([2000, 5000], 9000)).toEqual({ amountMin: 2000, amountMax: 5000 });
  });
});

describe("下钻与趋势", () => {
  const state = parseSalesAnalyticsUrl(sp("customer_name=A%E5%85%AC%E5%8F%B8"));
  it("组 → 筛选补丁：去重追加、切到订单明细；未填组与无筛选参数维度不可下钻", () => {
    expect(drillPatch(state, "customer", { key: "B公司", is_empty: false })).toEqual({
      customers: ["A公司", "B公司"], tab: "orders", page: 1,
    });
    expect(drillPatch(state, "customer", { key: "A公司", is_empty: false })?.customers).toEqual(["A公司"]);
    expect(drillPatch(state, "customer", { key: "未填", is_empty: true })).toBeNull();
    expect(drillPatch(state, "warehouse", { key: "一号仓", is_empty: false })).toBeNull();
    expect(drillPatch(state, "category", { key: "未分类", is_empty: false })).toMatchObject({
      categories: ["未分类"],
    });
  });
  it("粒度推荐：≤31 天按日、≤半年按周、否则 / 全部按月", () => {
    expect(recommendGranularity("2026-10-01", "2026-10-31")).toBe("day");
    expect(recommendGranularity("2026-05-02", "2026-10-31")).toBe("week");   // 183 天
    expect(recommendGranularity("2026-05-01", "2026-10-31")).toBe("month");  // 184 天
    expect(recommendGranularity("2025-10-21", "2026-10-21")).toBe("month");
    expect(recommendGranularity()).toBe("month");
  });
  it("桶 → 闭区间：周一至周日、月初至月末", () => {
    expect(bucketRange("2026-10-19", "week")).toEqual({ from: "2026-10-19", to: "2026-10-25" });
    expect(bucketRange("2026-02-01", "month")).toEqual({ from: "2026-02-01", to: "2026-02-28" });
    expect(bucketRange("2026-10-21", "day")).toEqual({ from: "2026-10-21", to: "2026-10-21" });
  });
  it("环比：上月为空或 0 时无意义", () => {
    expect(changeRatio(120, 100)).toBeCloseTo(0.2);
    expect(changeRatio(-50, -100)).toBeCloseTo(0.5);
    expect(changeRatio(100, 0)).toBeNull();
    expect(changeRatio(null, 100)).toBeNull();
  });
});

describe("图表 option（charts/README 约定）", () => {
  it("分组横条：null 剔除、负值换色并加 0 轴参考线", () => {
    const opt = buildGroupBarOption([
      { key: "甲", revenue: 100 }, { key: "乙", revenue: null }, { key: "丙", revenue: -20 },
    ], "revenue") as unknown as {
      yAxis: { data: string[] };
      series: { data: { value: number; itemStyle: { color: string } }[]; markLine?: unknown }[];
    };
    expect(opt.yAxis.data).toEqual(["甲", "丙"]);
    const [pos, neg] = opt.series[0].data;
    expect(pos.itemStyle.color).not.toBe(neg.itemStyle.color);
    expect(opt.series[0].markLine).toBeDefined();
  });

  it("趋势：单纵轴；上月同期按位置对齐，缺位为 null（不画 0）", () => {
    const opt = buildTrendOption(
      [{ bucket: "2026-10-01", orders: 2 }, { bucket: "2026-10-02", orders: 0 }, { bucket: "2026-10-03", orders: 1 }],
      [{ bucket: "2026-09-01", orders: 1 }, { bucket: "2026-09-02", orders: 3 }],
      "orders", "day",
    ) as unknown as { yAxis: unknown; series: { name: string; data: (number | null)[] }[] };
    expect(Array.isArray(opt.yAxis)).toBe(false);
    expect(opt.series.map((x) => x.name)).toEqual(["本期", "上月同期"]);
    expect(opt.series[0].data).toEqual([2, 0, 1]);
    expect(opt.series[1].data).toEqual([1, 3, null]);
  });

  it("趋势无对比时只有本期序列、无图例", () => {
    const opt = buildTrendOption([{ bucket: "2026-10-01", revenue: 5 }], null, "revenue", "month") as unknown as {
      legend?: unknown; series: unknown[];
    };
    expect(opt.series).toHaveLength(1);
    expect(opt.legend).toBeUndefined();
  });

  it("星期分布：固定周一到周日 7 格，缺的星期为 null", () => {
    const opt = buildWeekdayOption([{ isodow: 1, orders: 2 }, { isodow: 7, orders: 1 }], "orders") as unknown as {
      series: { data: (number | null)[] }[];
    };
    expect(opt.series[0].data).toEqual([2, null, null, null, null, null, 1]);
  });
});
