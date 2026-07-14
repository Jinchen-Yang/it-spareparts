/** fixture 契约：种子随机=跨机器逐位一致；且必须覆盖 null 断档/负毛利/超长 PN，
 * 这些是 demo 视觉验收和后续集成回归所依赖的"必现"特征。 */
import { describe, expect, it } from "vitest";
import {
  metricPurchaseAvgFixture, metricSalesTotalFixture,
  trendDailyFixture, trendMonthlyFixture, trendWeeklyFixture,
} from "../fixtures";

describe("fixtures", () => {
  it("完全确定：两次生成逐位一致", () => {
    expect(trendDailyFixture()).toEqual(trendDailyFixture());
    expect(metricPurchaseAvgFixture()).toEqual(metricPurchaseAvgFixture());
  });

  it("趋势数据覆盖 null 断档与负毛利（图表关键路径必现）", () => {
    const daily = trendDailyFixture();
    expect(daily).toHaveLength(120);
    expect(daily.some((p) => p.sales_ex_tax === null)).toBe(true);
    expect(daily.some((p) => (p.gross_profit ?? 0) < 0)).toBe(true);
    expect(daily.some((p) => p.compare?.sales_ex_tax?.yoy != null)).toBe(true);
    expect(trendWeeklyFixture().length).toBeGreaterThan(10);
    expect(trendMonthlyFixture().length).toBeGreaterThanOrEqual(4);
  });

  it("条形图数据覆盖 null 值项与超长 PN（截断路径必现）", () => {
    const purchase = metricPurchaseAvgFixture();
    expect(purchase.filter((i) => i.value === null).length).toBeGreaterThanOrEqual(3);
    expect(purchase.some((i) => i.pn.length > 15)).toBe(true);
    expect(purchase.some((i) => i.constraint_price != null)).toBe(true);
    const sales = metricSalesTotalFixture();
    expect(sales.filter((i) => i.value === null)).toHaveLength(2);
  });
});
