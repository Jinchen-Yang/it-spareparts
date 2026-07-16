import { describe, expect, it } from "vitest";
import { DETAIL_ROUTES, NAV_GROUPS } from "../../nav";

describe("全员互通池价格分析导航", () => {
  it("独立价格分析菜单使用 page_pool_analysis，不复用老板看板权限", () => {
    const group = NAV_GROUPS.find((item) => item.key === "grp-price-analysis");
    expect(group?.label).toBe("价格分析");
    expect(group?.items).toEqual(expect.arrayContaining([
      expect.objectContaining({
        key: "pools",
        path: "/pools",
        label: "互通池",
        perm: "page_pool_analysis",
      }),
    ]));
  });

  it("旧池详情深链改用分析权限并高亮互通池菜单", () => {
    const detail = DETAIL_ROUTES.find((item) => item.key === "pool-analysis");
    expect(detail).toMatchObject({
      path: "/pool-analysis/:groupId",
      perm: "page_pool_analysis",
      menuKey: "pools",
    });
  });
});
