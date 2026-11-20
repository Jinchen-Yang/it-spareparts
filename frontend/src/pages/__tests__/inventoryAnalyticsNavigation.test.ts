import { describe, expect, it } from "vitest";
import { isNavItemAllowed, matchNavItem } from "../../nav";

describe("库存管理分析入口", () => {
  const item = matchNavItem("/inventory/analytics")!;
  it("和库存查询使用独立路由与权限", () => {
    expect(item).toMatchObject({ key: "inventory-analytics", label: "库存管理分析", perm: "page_inventory_analytics" });
    expect(matchNavItem("/inventory")?.perm).toBe("page_inventory");
  });
  it.each([
    ["admin", false, false, true], ["boss", true, false, true], ["boss", false, false, false],
    ["sales", true, false, false], ["readonly", true, false, false], ["boss", true, true, false],
    ["admin", true, true, false],
  ] as const)("角色 %s，页面键 %s，客户收敛 %s", (role, granted, scoped, expected) => {
    const perms = { page_inventory_analytics: granted, own_customers_only: scoped };
    localStorage.setItem("role", role);
    localStorage.setItem("permissions", JSON.stringify(perms));
    try {
      expect(isNavItemAllowed(item, role, perms, {})).toBe(expected);
    } finally {
      localStorage.clear();
    }
  });
});
