import { describe, expect, it } from "vitest";
import { isNavItemAllowed, matchNavItem } from "../../nav";

describe("库存管理分析入口", () => {
  const item = matchNavItem("/inventory/analytics")!;
  it("与库存查询使用独立的实名页面键", () => {
    expect(item).toMatchObject({
      key: "inventory-analytics",
      label: "库存管理分析",
      perm: "page_inventory_analytics",
      accountScoped: true,
    });
    expect(matchNavItem("/inventory")?.perm).toBe("page_inventory");
  });
  it.each(["admin", "boss", "sales", "purchaser", "readonly", "maintenance_manager"])(
    "%s 的入口跟随服务端确认的库存权限，不受采购、销售或客户范围干扰",
    (role) => {
      const other = {
        page_purchase_analytics: true,
        page_sales_analysis: true,
        own_customers_only: true,
      };
      expect(isNavItemAllowed(item, role, other, {})).toBe(false);
      expect(isNavItemAllowed(item, role, { ...other, page_inventory_analytics: false }, {})).toBe(
        false,
      );
      expect(isNavItemAllowed(item, role, { ...other, page_inventory_analytics: true }, {})).toBe(true);
    },
  );
});
