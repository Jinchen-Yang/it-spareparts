// 销售分析导航注册（D-41 PR-4b）：新页挂销售管理组首位、页面权限键与后端
// page_sales_analysis 精确一致（注意是 analysis 单数形式，与 page_purchases 等
// 复数键不同族），模板仅 admin/boss。
import { beforeEach, describe, expect, it } from "vitest";

import { matchNavItem, NAV_GROUPS } from "../../nav";

describe("销售分析导航（D-41）", () => {
  beforeEach(() => localStorage.clear());

  it("新页 /sales/analytics：key/标签/权限正确登记", () => {
    expect(matchNavItem("/sales/analytics")).toMatchObject({
      key: "sales-analytics",
      label: "销售分析",
      path: "/sales/analytics",
      perm: "page_sales_analysis",
    });
  });

  it("销售组内入口并存、标签互不重复且顺序精确一致", () => {
    const grp = NAV_GROUPS.find((g) => g.key === "grp-sales")!;
    const labels = grp.items.map((it) => it.label);
    expect(labels).toEqual(["销售分析", "型号查询", "利润分析"]);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("权限键逐字等于后端 page_sales_analysis（错一个字符整个页面就 404/403）", () => {
    expect(matchNavItem("/sales/analytics")?.perm).toBe("page_sales_analysis");
  });
});
