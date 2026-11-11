import { describe, expect, it } from "vitest";

import { NAV_GROUPS, isNavItemAllowed } from "../nav";

const cycle = NAV_GROUPS
  .find((group) => group.key === "grp-maintenance")
  ?.items.find((item) => item.key === "cycle-pool");

describe("待修池导航", () => {
  it("挂在维保组，只有维修、主管、采购三个页键可见，admin 不能绕过账号快照", () => {
    expect(cycle).toMatchObject({
      path: "/maintenance/cycle",
      label: "待修池",
      accountScoped: true,
      anyPerm: [
        "page_cycle_repair",
        "page_cycle_manage",
        "page_cycle_wip",
      ],
    });
    expect(cycle).toBeDefined();
    const item = cycle!;
    expect(isNavItemAllowed(item, "admin", {}, {})).toBe(false);
    expect(isNavItemAllowed(item, "admin", { page_cycle_repair: true }, {})).toBe(true);
    expect(isNavItemAllowed(item, "admin", { page_cycle_manage: true }, {})).toBe(true);
    expect(isNavItemAllowed(item, "readonly", { page_cycle_wip: true }, {})).toBe(true);
    expect(isNavItemAllowed(item, "admin", { page_cycle_recycle: true }, {})).toBe(false);
    expect(isNavItemAllowed(item, "admin", { page_cycle_test: true }, {})).toBe(false);
    expect(isNavItemAllowed(item, "readonly", {}, {})).toBe(false);
  });

  it("普通菜单仍让 admin 全通", () => {
    const home = NAV_GROUPS
      .find((group) => group.key === "grp-maintenance")
      ?.items.find((item) => item.key === "maintenance-analytics");
    expect(home).toBeDefined();
    expect(isNavItemAllowed(home!, "admin", {}, {})).toBe(true);
    expect(isNavItemAllowed(home!, "readonly", {}, {})).toBe(false);
  });
});
