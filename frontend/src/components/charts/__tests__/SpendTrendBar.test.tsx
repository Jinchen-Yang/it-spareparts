/** SpendTrendBar：堆叠系列/null 语义/粒度轴标签/最近 40 期截断/tooltip 拆分。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import dayjs from "dayjs";

vi.mock("../echartsCore", async () => (await import("./echartsCoreMock")).mockModule);

import { resetEchartsMock } from "./echartsCoreMock";
import {
  SPEND_CATEGORY_CODES,
  SPEND_CATEGORY_LABELS,
  SPEND_TREND_MAX_BUCKETS,
  SpendTrendBar,
  buildSpendTrendBarOption,
  formatSpendBucket,
  formatSpendTrendTooltip,
  spendValue,
  visibleSpendBuckets,
} from "../SpendTrendBar";
import { CHART_COLORS } from "../chartTheme";
import type {
  SpendBusinessTypeCode,
  SpendTrendBucket,
} from "../../../api/maintenanceAnalytics";

afterEach(() => {
  cleanup();
  resetEchartsMock();
});

const ready = (value: string | number) => ({ state: "ready", value: String(value) });
const restricted = { state: "restricted" };
const notImported = { state: "not_imported" };

function makeBucket(
  bucket: string,
  values: Partial<Record<SpendBusinessTypeCode, unknown>> = {},
): SpendTrendBucket {
  const defaults = Object.fromEntries(
    SPEND_CATEGORY_CODES.map((code) => [code, ready("100")]),
  );
  return {
    bucket,
    order_count: 1,
    qty: "1",
    effective_qty: "1",
    missing_lines: 0,
    cost_inc: ready("700"),
    cost_ex: ready("619.47"),
    by_business_type: { ...defaults, ...values },
  } as SpendTrendBucket;
}

describe("buildSpendTrendBarOption", () => {
  it("七个堆叠系列：名称=分类标签、同一 stack、颜色互不相同且只用 CHART_COLORS token", () => {
    const opt = buildSpendTrendBarOption([makeBucket("2026-09-01")], "month") as Record<string, any>;
    const names = SPEND_CATEGORY_CODES.map((code) => SPEND_CATEGORY_LABELS[code]);
    expect(opt.series).toHaveLength(7);
    expect(opt.series.map((s: Record<string, any>) => s.name)).toEqual(names);
    expect(new Set(opt.series.map((s: Record<string, any>) => s.stack))).toEqual(new Set(["spend"]));
    expect(opt.legend.data).toEqual(names);
    const colors = opt.series.map((s: Record<string, any>) => s.itemStyle.color);
    expect(new Set(colors).size).toBe(7);
    const palette = Object.values(CHART_COLORS);
    colors.slice(0, 6).forEach((color: string) => expect(palette).toContain(color));
    // 未归属：crosshair 灰 token 的降透明度派生（不新增色相）
    expect(colors[6]).toMatch(/^rgba\(120, 114, 100,/);
  });

  it("null ≠ 0：restricted/not_imported/空值 → null，ready 0 保留 0", () => {
    const bucket = makeBucket("2026-09-01", {
      overall: restricted,
      spare: notImported,
      computing: { state: "ready", value: null },
      refit: ready("0"),
      other: { state: "ready", value: "" },
      unlabeled: ready("12.5"),
      unassigned: undefined,
    });
    const opt = buildSpendTrendBarOption([bucket], "month") as Record<string, any>;
    expect(opt.series.map((s: Record<string, any>) => s.data[0]))
      .toEqual([null, null, null, 0, null, 12.5, null]);
  });

  it("超过 40 期只画最近 40 期（完整数据留给明细表）", () => {
    const buckets = Array.from({ length: 45 }, (_, i) =>
      makeBucket(dayjs("2026-01-01").add(i, "day").format("YYYY-MM-DD")));
    const { visible, hiddenCount } = visibleSpendBuckets(buckets);
    expect(hiddenCount).toBe(5);
    expect(visible).toHaveLength(SPEND_TREND_MAX_BUCKETS);
    expect(visible[0].bucket).toBe(buckets[5].bucket);
    const opt = buildSpendTrendBarOption(buckets, "day") as Record<string, any>;
    expect(opt.xAxis.data).toHaveLength(SPEND_TREND_MAX_BUCKETS);
    expect(opt.xAxis.data[0]).toBe(formatSpendBucket(buckets[5].bucket, "day"));
  });

  it("x 轴标签按粒度格式化：日 MM-DD / 周 MM-DD 周 / 月 YYYY-MM / 年 YYYY", () => {
    expect(formatSpendBucket("2026-09-17", "day")).toBe("09-17");
    expect(formatSpendBucket("2026-09-14", "week")).toBe("09-14 周");
    expect(formatSpendBucket("2026-09-01", "month")).toBe("2026-09");
    expect(formatSpendBucket("2026-01-01", "year")).toBe("2026");
    const opt = buildSpendTrendBarOption([makeBucket("2026-09-01")], "month") as Record<string, any>;
    expect(opt.xAxis.data).toEqual(["2026-09"]);
  });

  it("tooltip 含期标签、七分类精确金额（受限显式标注）与金额合计（含税）", () => {
    const bucket = makeBucket("2026-09-01", {
      overall: ready("600"),
      spare: restricted,
      computing: notImported,
      refit: { state: "ready", value: "" },
      other: ready("0"),
      unlabeled: ready("88.5"),
      unassigned: undefined,
    });
    const html = formatSpendTrendTooltip(bucket, "month");
    expect(html).toContain("<b>2026-09</b>");
    expect(html).toContain("整体维保：¥600");
    expect(html).toContain("备件维保：🔒 无权限");
    expect(html).toContain("算力运维：尚未导入");
    expect(html).toContain("拆改配服务：—");
    expect(html).toContain("非维保：¥0");
    expect(html).toContain("未标注：¥88.5");
    expect(html).toContain("未归属：—");
    expect(html).toContain("金额合计（含税）：¥700");
    const opt = buildSpendTrendBarOption([bucket], "month") as Record<string, any>;
    expect(opt.tooltip.confine).toBe(true);
    expect(opt.tooltip.formatter([{ dataIndex: 0 }])).toBe(html);
  });

  it("spendValue：仅 ready 且可解析为数值才入图", () => {
    expect(spendValue(ready("1.5"))).toBe(1.5);
    expect(spendValue(ready("0"))).toBe(0);
    expect(spendValue(restricted)).toBeNull();
    expect(spendValue(notImported)).toBeNull();
    expect(spendValue({ state: "ready", value: null })).toBeNull();
    expect(spendValue({ state: "ready", value: "abc" })).toBeNull();
    expect(spendValue(undefined)).toBeNull();
  });
});

describe("组件接线", () => {
  it("超过 40 期时注明仅展示最近 40 期与隐藏期数", () => {
    const buckets = Array.from({ length: 41 }, (_, i) =>
      makeBucket(dayjs("2026-01-01").add(i, "day").format("YYYY-MM-DD")));
    render(<SpendTrendBar buckets={buckets} granularity="day" />);
    expect(screen.getByText(/仅展示最近 40 期/)).toBeTruthy();
    expect(screen.getByText(/更早 1 期/)).toBeTruthy();
  });

  it("全受限（无可绘值）显示空态而非全 0 柱", () => {
    const allRestricted = Object.fromEntries(
      SPEND_CATEGORY_CODES.map((code) => [code, restricted]),
    );
    render(<SpendTrendBar buckets={[makeBucket("2026-09-01", allRestricted)]} granularity="month" />);
    expect(screen.getByTestId("chart-empty")).toBeTruthy();
  });
});
