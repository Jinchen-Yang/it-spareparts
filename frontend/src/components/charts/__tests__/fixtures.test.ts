/** fixture 契约：种子随机=跨机器逐位一致；且必须覆盖 null 值与超长 PN，
 * 这些是 demo 视觉验收和后续集成回归所依赖的"必现"特征。 */
import { describe, expect, it } from "vitest";
import {
  metricPurchaseAvgFixture, metricSalesTotalFixture,
} from "../fixtures";

describe("fixtures", () => {
  it("完全确定：两次生成逐位一致", () => {
    expect(metricPurchaseAvgFixture()).toEqual(metricPurchaseAvgFixture());
    expect(metricSalesTotalFixture()).toEqual(metricSalesTotalFixture());
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
