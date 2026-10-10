import { describe, expect, it } from "vitest";
import { maintenanceAnalyticsReturnPath, readProjectPanelTab } from "../analysisNavigation";

describe("分析返回地址", () => {
  it("保留完整筛选、特殊 PN、分页与布局，且不二次解码", () => {
    const query = new URLSearchParams({
      layout: "matrix", focus: "pn:PN+100%/ A", customer: "甲方 / 运维",
      project: "p1,p2", range: "custom", from: "2026-01-01", to: "2026-10-01", page: "3",
    });
    const path = `/maintenance/analytics?${query}`;
    expect(maintenanceAnalyticsReturnPath(path)).toBe(path);
    expect(maintenanceAnalyticsReturnPath("/maintenance/analytics")).toBe("/maintenance/analytics");
  });

  it.each([
    null, "", "https://example.com/maintenance/analytics", "//example.com/maintenance/analytics",
    "javascript:alert(1)", "/maintenance", "/maintenance/analytics-evil?x=1",
    "/maintenance/analytics/../projects/p1", "/maintenance/analytics/?q=PN",
    "\\example.com\\maintenance\\analytics", "/maintenance/analytics?x=1\n",
    "/maintenance/analytics?q=x#//example.com", " /maintenance/analytics",
  ])("拒绝外链、其他路由和地址混淆：%s", (value) => {
    expect(maintenanceAnalyticsReturnPath(value)).toBeNull();
  });
});

describe("项目页 URL 页签", () => {
  it("支持分析定位与原有页签；非法页签回到概览", () => {
    expect(readProjectPanelTab("analytics")).toBe("analytics");
    expect(readProjectPanelTab("parts-orders")).toBe("parts-orders");
    expect(readProjectPanelTab("site")).toBe("site");
    expect(readProjectPanelTab("__proto__")).toBe("overview");
    expect(readProjectPanelTab(null)).toBe("overview");
  });
});
