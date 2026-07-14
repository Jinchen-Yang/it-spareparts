/** BusinessTrendChart：option 纯函数断言（配置存在性/空值语义/不平滑）
 * + 像素反解点击（zr:click → 正确 period）。echarts mock 见 echartsCoreMock。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("../echartsCore", async () => (await import("./echartsCoreMock")).mockModule);

import { instances, resetEchartsMock } from "./echartsCoreMock";
import BusinessTrendChart, {
  buildTrendOption, formatTrendTooltip, resolveTrendZrClick,
  type BusinessTrendPoint, type TrendClickChart,
} from "../BusinessTrendChart";
import { CHART_COLORS } from "../chartTheme";

afterEach(() => {
  cleanup();
  resetEchartsMock();
});

const POINTS: BusinessTrendPoint[] = [
  { period: "2026-03-01", sales_ex_tax: 100, purchase_ex_tax: 80, gross_profit: 20 },
  { period: "2026-03-02", sales_ex_tax: null, purchase_ex_tax: null, gross_profit: null },
  {
    period: "2026-03-03", sales_ex_tax: 300, purchase_ex_tax: 500, gross_profit: -50,
    compare: { sales_ex_tax: { yoy: 0.123, mom: -0.045 } },
  },
];

type AnyOption = Record<string, any>;

describe("buildTrendOption", () => {
  const opt = buildTrendOption(POINTS, "day") as AnyOption;

  it("三条序列、直线连接不做平滑、断档 null 原样保留（不折算成 0）", () => {
    expect(opt.series).toHaveLength(3);
    expect(opt.series.map((s: AnyOption) => s.name)).toEqual(["销售额", "采购额", "毛利额"]);
    opt.series.forEach((s: AnyOption) => {
      expect(s.smooth).toBe(false);
      expect(s.connectNulls).toBe(false);
    });
    expect(opt.series[0].data).toEqual([100, null, 300]);
    expect(opt.series[2].data).toEqual([20, null, -50]);
    expect(opt.series[0].data[1]).not.toBe(0);
  });

  it("十字指针 + 轴触发 tooltip + 双 dataZoom（inside 滚轮缩放/拖动、slider 窗口）", () => {
    expect(opt.tooltip.trigger).toBe("axis");
    expect(opt.tooltip.axisPointer.type).toBe("cross");
    const zoomTypes = opt.dataZoom.map((z: AnyOption) => z.type);
    expect(zoomTypes).toContain("inside");
    expect(zoomTypes).toContain("slider");
    const inside = opt.dataZoom.find((z: AnyOption) => z.type === "inside");
    expect(inside.zoomOnMouseWheel).toBe(true);
    expect(inside.moveOnMouseMove).toBe(true);
  });

  it("图例存在（序列可单独开关）且启用 aria", () => {
    expect(opt.legend).toBeTruthy();
    expect(opt.aria.enabled).toBe(true);
  });

  it("负毛利分段变色（visualMap 只作用于毛利序列）+ 0 轴虚线参考线", () => {
    expect(opt.visualMap.seriesIndex).toBe(2);
    // 边界必须有限且覆盖全部数据（2×max|毛利|=100）：无界 piece 会让 echarts
    // getVisualGradient 初始渲染崩溃（组件内注释），这里锁死防回归
    expect(opt.visualMap.pieces).toEqual([
      { gte: -100, lt: 0, color: CHART_COLORS.profitNegative },
      { gte: 0, lte: 100, color: CHART_COLORS.profit },
    ]);
    expect(opt.series[2].markLine.data).toEqual([{ yAxis: 0 }]);
    expect(opt.series[0].markLine).toBeUndefined();
  });

  it("日粒度轴标签去年份；周/月标签原样", () => {
    expect(opt.xAxis.axisLabel.formatter("2026-03-01")).toBe("03-01");
    const weekOpt = buildTrendOption(POINTS, "week") as AnyOption;
    expect(weekOpt.xAxis.axisLabel.formatter("2026-W12")).toBe("2026-W12");
  });

  it("大数据量（>60 点）收常驻圆点并启用 LTTB 抽稀（非平滑，不捏造中间值）", () => {
    const many: BusinessTrendPoint[] = Array.from({ length: 90 }, (_, i) => ({
      period: `2026-04-${String((i % 28) + 1).padStart(2, "0")}`,
      sales_ex_tax: i, purchase_ex_tax: i, gross_profit: i,
    }));
    const dense = buildTrendOption(many, "day") as AnyOption;
    expect(dense.series[0].showSymbol).toBe(false);
    expect(dense.series[0].sampling).toBe("lttb");
    expect(opt.series[0].showSymbol).toBe(true);
    expect(opt.series[0].sampling).toBeUndefined();
  });
});

describe("formatTrendTooltip", () => {
  it("显示期全称 + 精确金额；同比/环比仅在调用方提供时出现；null 显示占位符", () => {
    const html = formatTrendTooltip(
      [
        { seriesName: "销售额", seriesIndex: 0, dataIndex: 2, marker: "", value: 300 },
        { seriesName: "毛利额", seriesIndex: 2, dataIndex: 2, marker: "", value: -50 },
      ],
      POINTS,
    );
    expect(html).toContain("2026-03-03");
    expect(html).toContain("¥300");
    expect(html).toContain("¥-50");
    expect(html).toContain("同比 +12.3%");
    expect(html).toContain("环比 -4.5%");
    const noCmp = formatTrendTooltip(
      [{ seriesName: "销售额", seriesIndex: 0, dataIndex: 0, marker: "", value: 100 }],
      POINTS,
    );
    expect(noCmp).not.toContain("同比");
    const nullVal = formatTrendTooltip(
      [{ seriesName: "销售额", seriesIndex: 0, dataIndex: 1, marker: "", value: null }],
      POINTS,
    );
    expect(nullVal).toContain("<b>-</b>");
  });

  it("period 中的 HTML 被转义（防注入）", () => {
    const dirty: BusinessTrendPoint[] = [
      { period: "<img src=x>", sales_ex_tax: 1, purchase_ex_tax: 1, gross_profit: 1 },
    ];
    const html = formatTrendTooltip(
      [{ seriesName: "销售额", seriesIndex: 0, dataIndex: 0, marker: "", value: 1 }],
      dirty,
    );
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});

describe("resolveTrendZrClick / 组件接线", () => {
  const stubChart = (over: Partial<TrendClickChart> = {}): TrendClickChart => ({
    containPixel: () => true,
    convertFromPixel: () => [2, 12345],
    ...over,
  });

  it("绘图区内点击反解出最近日期；网格外/参数残缺返回 null", () => {
    expect(resolveTrendZrClick(stubChart(), { offsetX: 10, offsetY: 10 }, POINTS)?.period)
      .toBe("2026-03-03");
    expect(resolveTrendZrClick(
      stubChart({ containPixel: () => false }), { offsetX: 10, offsetY: 10 }, POINTS,
    )).toBeNull();
    expect(resolveTrendZrClick(
      stubChart({ convertFromPixel: () => [99, 0] }), { offsetX: 10, offsetY: 10 }, POINTS,
    )).toBeNull(); // 越界索引
    expect(resolveTrendZrClick(stubChart(), { offsetX: 10 }, POINTS)).toBeNull();
    expect(resolveTrendZrClick(null, { offsetX: 10, offsetY: 10 }, POINTS)).toBeNull();
  });

  it("索引四舍五入到最近类目（1.6 → 第 2 个点）", () => {
    const hit = resolveTrendZrClick(
      stubChart({ convertFromPixel: () => [1.6, 0] }), { offsetX: 1, offsetY: 1 }, POINTS,
    );
    expect(hit?.period).toBe("2026-03-03");
  });

  it("画布点击（zr:click）回调 onPointClick(period, point)——断档日也可点（下钻查单合法）", () => {
    const onPointClick = vi.fn();
    render(<BusinessTrendChart data={POINTS} granularity="day" onPointClick={onPointClick} />);
    const chart = instances[0];
    chart.convertFromPixel.mockReturnValue([1, 0]);
    chart.zr.emit("click", { offsetX: 50, offsetY: 60 });
    expect(onPointClick).toHaveBeenCalledWith("2026-03-02", POINTS[1]);
    chart.containPixel.mockReturnValue(false); // 点到图例/滑块区域不触发
    chart.zr.emit("click", { offsetX: 1, offsetY: 1 });
    expect(onPointClick).toHaveBeenCalledTimes(1);
  });

  it("空数据显示空态而非空白图", () => {
    render(<BusinessTrendChart data={[]} granularity="day" />);
    expect(screen.getByTestId("chart-empty")).toBeTruthy();
  });
});
