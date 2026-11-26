import { expect, it } from "vitest";
import { NAV_GROUPS, isNavItemAllowed } from "../nav";

it("从型号查询旁进入机型备件池，具有目录查看权限的员工可直接打开", () => {
  const group = NAV_GROUPS.find(g => g.items.some(item => item.key === "parts"))!;
  const pool = group.items.find(item => item.path === "/model-parts");
  expect(pool).toBeDefined();
  expect(pool?.label).toBe("机型备件池");
  expect(isNavItemAllowed(pool!, "readonly", { page_parts: true }, {})).toBe(true);
  expect(isNavItemAllowed(pool!, "readonly", { page_pool_analysis: true }, {})).toBe(false);
});
