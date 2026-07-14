/** HorizontalMetricBar：排序/空值语义/口径标签/双模式配色/点击 part_id/
 * 滚动 dataZoom/长 PN 截断。echarts mock 同 EChartContainer 测试。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("../echartsCore", async () => (await import("./echartsCoreMock")).mockModule);

import { instances, resetEchartsMock } from "./echartsCoreMock";
import HorizontalMetricBar, {
  buildMetricBarOption, formatMetricBarTooltip, metricLabel,
  resolveMetricBarClick, sortMetricBarItems, MODE_COLOR,
  type MetricBarItem,
} from "../HorizontalMetricBar";
import { CHART_COLORS } from "../chartTheme";

afterEach(() => {
  cleanup();
  resetEchartsMock();
});

const ITEMS: MetricBarItem[] = [
  { part_id: 1, pn: "PN-LOW", value: 100, qty: 5, order_count: 2, last_date: "2026-06-01" },
  { part_id: 2, pn: "PN-NULL", value: null },
  { part_id: 3, pn: "PN-HIGH", value: 900, description: "高价盘", pool_avg: 700, constraint_price: 800 },
  { part_id: 4, pn: "PN-MID", value: 500 },
  { part_id: 5, pn: "PN-NAN", value: Number.NaN },
];

describe("sortMetricBarItems", () => {
  it("从高到低排序；null/NaN 整项剔除并计数（绝不画成 0 长度柱）", () => {
    const { sorted, excluded } = sortMetricBarItems(ITEMS);
    expect(sorted.map((i) => i.pn)).toEqual(["PN-HIGH", "PN-MID", "PN-LOW"]);
    expect(excluded).toBe(2);
    expect(sorted.some((i) => i.value === 0)).toBe(false);
    expect(ITEMS).toHaveLength(5); // 不改入参
  });
});

describe("metricLabel（口径文案唯一出口）", () => {
  it("average=平均单价；total=采购/销售金额合计，禁止「价格合计」", () => {
    expect(metricLabel("purchase", "average")).toBe("平均单价");
    expect(metricLabel("sales", "average")).toBe("平均单价");
    expect(metricLabel("purchase", "total")).toBe("采购金额合计");
    expect(metricLabel("sales", "total")).toBe("销售金额合计");
    (["purchase", "sales"] as const).forEach((m) =>
      (["average", "total"] as const).forEach((k) =>
        expect(metricLabel(m, k)).not.toContain("价格合计")));
  });
});

describe("buildMetricBarOption", () => {
  const { sorted } = sortMetricBarItems(ITEMS);

  it("纵轴 PN（inverse 保证第一名在顶）、横轴数值、柱数据无 null", () => {
    const opt = buildMetricBarOption(sorted, "purchase", "average") as Record<string, any>;
    expect(opt.yAxis.type).toBe("category");
    expect(opt.yAxis.inverse).toBe(true);
    expect(opt.yAxis.data).toEqual(["PN-HIGH", "PN-MID", "PN-LOW"]);
    expect(opt.xAxis.type).toBe("value");
    expect(opt.series[0].data).toEqual([900, 500, 100]);
  });

  it("purchase/sales 两套稳定配色 + 柱端数值标签（色彩非唯一信息源）", () => {
    const p = buildMetricBarOption(sorted, "purchase", "average") as Record<string, any>;
    const s = buildMetricBarOption(sorted, "sales", "total") as Record<string, any>;
    expect(p.series[0].itemStyle.color).toBe(MODE_COLOR.purchase);
    expect(s.series[0].itemStyle.color).toBe(MODE_COLOR.sales);
    expect(MODE_COLOR.purchase).not.toBe(MODE_COLOR.sales);
    expect(p.series[0].label.show).toBe(true);
    expect(p.series[0].name).toContain("平均单价");
    expect(s.series[0].name).toContain("销售金额合计");
  });

  it("PN 数超过一屏才启用 dataZoom（滚轮平移 + 侧滑块）；少量时不加", () => {
    const few = buildMetricBarOption(sorted, "purchase", "average", 12) as Record<string, any>;
    expect(few.dataZoom).toBeUndefined();
    const many = buildMetricBarOption(sorted, "purchase", "average", 2) as Record<string, any>;
    const types = many.dataZoom.map((z: Record<string, any>) => z.type);
    expect(types).toContain("inside");
    expect(types).toContain("slider");
    const inside = many.dataZoom.find((z: Record<string, any>) => z.type === "inside");
    expect(inside.yAxisIndex).toBe(0);
    expect(inside.moveOnMouseWheel).toBe(true);
    const slider = many.dataZoom.find((z: Record<string, any>) => z.type === "slider");
    expect(slider.startValue).toBe(0);
    expect(slider.endValue).toBe(1);
  });

  it("长 PN 轴标签定宽截断（完整值走 tooltip），不挤占绘图区", () => {
    const opt = buildMetricBarOption(sorted, "purchase", "average") as Record<string, any>;
    expect(opt.yAxis.axisLabel.overflow).toBe("truncate");
    expect(opt.yAxis.axisLabel.width).toBeGreaterThan(0);
    expect(opt.aria.enabled).toBe(true);
  });
});

describe("formatMetricBarTooltip", () => {
  it("包含 PN/描述/数量/订单数/最近日期/当前指标/池平均值/约束价及差异", () => {
    const html = formatMetricBarTooltip(ITEMS[2], "purchase", "average");
    expect(html).toContain("PN-HIGH");
    expect(html).toContain("高价盘");
    expect(html).toContain("平均单价");
    expect(html).toContain("¥900");
    expect(html).toContain("池平均值");
    expect(html).toContain("¥700");
    expect(html).toContain("人工约束价");
    expect(html).toContain("¥800");
    // 差异 = 900 - 800 = +¥100 / +12.5%
    expect(html).toContain("+¥100");
    expect(html).toContain("+12.5%");
  });

  it("total 口径不算约束价差异（单价 vs 合计不同量纲），只显示约束价原值", () => {
    const html = formatMetricBarTooltip(ITEMS[2], "sales", "total");
    expect(html).toContain("¥800");
    expect(html).not.toContain("差异");
  });

  it("缺失字段显示占位符而非 0；描述 HTML 被转义", () => {
    const html = formatMetricBarTooltip(
      { part_id: 9, pn: "P", value: 10, description: "<b>x</b>" }, "purchase", "average",
    );
    expect(html).toContain("&lt;b&gt;x&lt;/b&gt;");
    expect(html).not.toContain("<b>x</b>");
    const bare = formatMetricBarTooltip({ part_id: 9, pn: "P", value: 10 }, "purchase", "average");
    expect(bare.match(/>-</g)!.length).toBeGreaterThanOrEqual(4); // 描述/数量/订单数/日期等占位
  });
});

describe("组件接线", () => {
  it("点击柱条回调 onPartClick(part_id, pn)——按排序后的索引取值", () => {
    const onPartClick = vi.fn();
    render(<HorizontalMetricBar items={ITEMS} mode="purchase" metric="average" onPartClick={onPartClick} />);
    const chart = instances[0];
    chart.emit("click", { componentType: "series", dataIndex: 0 });
    expect(onPartClick).toHaveBeenCalledWith(3, "PN-HIGH"); // 排序后第一名，不是入参第一项
    chart.emit("click", { componentType: "series", dataIndex: 2 });
    expect(onPartClick).toHaveBeenCalledWith(1, "PN-LOW");
  });

  it("null 项剔除提示可见；全 null 显示空态", () => {
    render(<HorizontalMetricBar items={ITEMS} mode="purchase" metric="average" />);
    expect(screen.getByTestId("metric-bar-excluded").textContent).toContain("2 项");
    cleanup();
    render(<HorizontalMetricBar
      items={[{ part_id: 1, pn: "A", value: null }]} mode="sales" metric="total" />);
    expect(screen.getByTestId("chart-empty")).toBeTruthy();
  });

  it("口径文字条与模式色块同时存在（不依赖柱色辨识业务模式）", () => {
    render(<HorizontalMetricBar items={ITEMS} mode="sales" metric="total" />);
    expect(screen.getByText(/销售 · 销售金额合计/)).toBeTruthy();
    expect(screen.getByText("从高到低")).toBeTruthy();
  });
});
